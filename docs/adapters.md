# Adding a harness

A harness is the agent CLI that runs a worker's turns. It takes the signed prompt and the brief, calls a model,
uses tools inside the worktree and prints a result. Three ship: Claude Code (`cc`, the default), hermes-agent
(`hermes`, see [hermes.md](hermes.md)) and the Codex CLI (`codex`, see [The Codex harness](#the-codex-harness)).
This page explains the seam they plug into.

This is not the same thing as the tracker adapters in [trackers.md](trackers.md), which are about where tickets
come from.

## Two layers

The code has two layers, and it helps to keep them apart.

- The **Adapter** (`adapters/base.py`) is the interface the broker's scheduler talks to. It owns a worker's
  whole life: start it, feed it follow-ups, say whether it's busy, stop it. `CCAdapter` in `adapters/cc.py` is
  the only production implementation, and `app.py` always builds it. `StubAdapter` (`adapters/stub.py`) records
  calls and starts nothing; the tests and `coalescer.py --selftest` use it.
- The **harness** runs one turn. `CCAdapter` does everything around the turn (worktree, ticket brief, prompt
  check, concurrency cap, session rotation, the envelope re-prompt, publishing, escalations) and hands the turn
  itself to the harness the route names. For `cc` that's its own `claude -p` code. For `hermes` it calls
  `HermesHarness.one_turn` in `adapters/hermes.py`, and for `codex` `CodexHarness.one_turn` in `adapters/codex.py`.

So `CCAdapter` covers more than Claude Code: it's the lifecycle every harness shares. A new harness belongs at
the turn layer, the way hermes does it. A new `Adapter` subclass would have to rebuild the worktree cut, the
prompt gate, the caps, the retry, publishing and every escalation path, and `app.py` has no switch for it.

## The Adapter interface

`adapters/base.py` defines four required methods and three optional ones; the coalescer also calls `steerable` if an adapter has it. `key` is the subject key, such as
`vikunja:task:42` or `gitlab:group/app#7`.

| Method | Called by, and when | What it must do |
|---|---|---|
| `spawn(key, brief)` | The coalescer (`coalescer.py`) when a batching window closes, while it holds its lock. | Start the worker on its own thread and return at once. Never block. |
| `forward(key, brief)` | The liveness driver (`liveness.py`) at a turn boundary, with every buffered follow-up collapsed into one brief. | Queue one more turn on the warm worker and return without blocking. With no live worker, drop it; the coalescer starts a fresh one later. |
| `liveness(key)` | Reserved for the force-close backstop. | `True` only while the worker is inside a turn. The shipped driver decides force-closes from `turn_age` and `worker_alive` instead, so nothing in the broker calls it today. |
| `teardown(key)` | The liveness driver, on a normal close or a force-close. Runs without the coalescer lock. | Kill any running process, then wait (bounded) for the worker thread to remove its worktree, since the next worker for the same subject may reuse the path. Safe to call twice. |
| `turn_age(key)` | The liveness driver, on every sweep of a running worker. | Seconds the current turn has run, or `None` when idle or gone. Default `None`. |
| `worker_alive(key)` | The same sweep, when `turn_age` is `None`. | Whether the worker's own thread is still running. Default `False`, so a running worker with no turn age gets force-closed. |
| `set_coalescer(c)` | The coalescer's constructor. | Keep the reference, so the worker can report turn start and end through the coalescer's lock. Default does nothing. |
| `steerable(key)` | `coalescer.forward_message` when a dashboard message arrives mid-turn. | True if this worker's turns carry the `steer.py` `PostToolUse` hook, so the message can land after its next tool call. Default (missing) False: it waits for the turn boundary. |

`CCAdapter` also has `turn_deadline_extra(key)`. The driver reads it if it exists, so a capability with a longer
time limit than the global one keeps the same headroom before a force-close.

Refusals happen in the worker thread, never in `spawn`. A missing prompt, an unknown capability or a missing
binary is logged, reported to the escalation handle and leaves nothing running.

## The turn seam

`CCAdapter._one_turn` reads `route.adapter`. For `cc` it builds the `claude` command line itself. For `hermes`
and `codex` it builds `HermesHarness` / `CodexHarness` on first use (so a broker that never routes to one never
imports it) and calls:

```python
def one_turn(self, adapter, worker, route, system_prompt, prompt_text, resume_session,
             backend_override=_NO_OVERRIDE, wall_clock_override=None)
```

Any other value raises and the worker is refused. What each argument is:

- `adapter`: the `CCAdapter`. The turn reads its caps from it (`max_turns_by_capability`, `max_turns`,
  `wall_clock_by_capability`, `wall_clock_s`), alerts through `adapter._alert_ops` and parses with
  `adapter.envelope_parser`.
- `worker`: the `Worker` from `adapters/cc.py`, with `key`, `run_id`, `worktree`, `session_id` and `done`.
- `route`: the `Route`, with `capability`, `backend`, `env` and `test_runner`.
- `system_prompt`: the signed prompt, already hash-checked. Refuse to launch if it's empty.
- `prompt_text`: this turn's message. The ticket brief on the first turn, then collapsed follow-ups or the
  envelope re-prompt.
- `resume_session`: the session to continue, or `None` for a fresh one (first turn, or after rotation).
- `backend_override`: set by the usage-cap fallback chain. The default means use `route.backend`.
- `wall_clock_override`: a shorter limit for the one re-prompt allowed after a hard kill.

The turn has to keep the `Worker` fields that the liveness layer and teardown read:

- Check `worker.done` before launching, and again right after the process starts. Teardown sets it before it
  kills.
- Before the process starts, set `in_turn = True`, `turn_started`, `turn_wall_clock` and `hard_killed = False`,
  and store the process in `worker.proc`. Clear `in_turn` and `turn_started` in a `finally`.
- Set `hard_killed = True` when the time limit kills the process. That caps the re-prompt.
- Add one to `worker.turns`, and store the raw output in `worker.last_output` (escalations quote it, fenced as
  untrusted).
- Set `worker.session_id` whenever the output names one, even on a failed turn, so the re-prompt can resume it.
- If the process runs in its own process group, set `worker.kill_group = True` so teardown kills the whole group.
  Teardown uses hermes' `_kill_group` for that (Codex reuses it).

It returns the parsed envelope, or `None` for a parse miss. A miss gets one warm re-prompt (a resumed turn asking
for a clean envelope), then an escalation.

Before dispatch, `CCAdapter` re-seeds a forwarded turn that starts fresh: on rotation, a usage-cap fallback,
or a forward with no learned session id. It merges the enriched seed brief and accumulated earlier forwarded
comments with the current delta; only non-None delta values override seed fields. Plain resumed forwards stay
deltas. Envelope-only re-prompts never re-seed or rotate, even without a session id, and the adapter excludes
their harness invocation from the rotation turn count. Every fresh start clears the previous context-size
reading, so a missing reading cannot carry a prior session's size into the new session.

## The envelope contract

`envelope.EnvelopeParser.try_parse(raw, run_id, route)` reads one JSON wrapper, the shape Claude Code prints with
`--output-format json`:

```json
{"result": "<the worker's final text>", "session_id": "<id>", "usage": {"input_tokens": 0, "output_tokens": 0}}
```

The last independently decoded JSON object with a `status` key inside `result` is the worker's envelope.
Nested objects within a decoded object are never separate envelopes. Prose, fences and other JSON objects
are tolerated. A later object with an invalid status is still a parse miss, not a reason to
fall back to an earlier envelope. `status` is required; `summary`, `wake` (a list)
and `needs_operator` (only a literal `true` counts) are optional. The status has to be one the capability allows:
`pass` or `block` for `mr-review`, `done` or `blocked` for everything else. A capability with no allowed set is a
parse miss. The parser adds `session_id`, `tokens`, `run_id` and `harness` (the route's `adapter` value, which
also ends up in the hidden tag on MR verdict notes).

The broker does not record, display or aggregate the CLI wrapper's `total_cost_usd` as spend. Claude Code's
price-table estimate is not actual billing for local or unrecognized models (which may emit
`[claude-code:unrecognized_model]`). Token counts are only what the harness reports: zero or missing usage
can mean unavailable accounting, not a free run or no work. Use the backend's own billing or metering for
spend; do not infer it from that estimate or the broker's token count.

A new harness should repack its own output into that wrapper, as hermes does with its stream-json `result`
record (`parse_stream`), so `envelope.py` stays the one parser for every harness.

Only a clean run may produce an envelope. A non-zero exit, an error record or a killed process is a parse miss,
even if the text happens to contain a valid envelope. Both shipped harnesses work that way. Anything the harness
adds after parsing, like hermes' `gate_denials`, has to come from its own records, never from the worker's text.

## How a route picks its harness

- `Route.adapter` in `routing.py`, `cc` by default. The allowed names are `routing.ADAPTERS`. `Route` refuses any
  other value when the table loads, and `_one_turn` refuses it again at turn time.
- Each built-in worker row reads its own `<ROW>_ADAPTER` setting (`routing._row_adapter`, default `cc`), for
  example `CODE_CHANGE_ADAPTER` or the reviewer's `MR_REVIEW_ADAPTER`
  ([configuration.md](configuration.md#choosing-a-rows-harness)).
- Rows in `config/routes.json` have no harness field and run on `cc`, unless a label below moves a ticket.
- A `hermes` or `codex` label moves one ticket's worker onto that harness (`routing.apply_adapter_label`). It only
  counts from creators in `MODEL_LABEL_TRUSTED_CREATORS`, never on a row with a pinned backend, and only for the
  label's capabilities (`HERMES_LABEL_CAPABILITIES` / `CODEX_LABEL_CAPABILITIES`, both `general` and `code`).
  Both labels on one ticket refuse. A hermes ticket also needs a non-Anthropic backend, normally from a `model:`
  label; a Codex ticket takes its model from `CODEX_BACKEND` unless its backend has a `codex` block. A label on
  the wrong capability or with an Anthropic backend (hermes) refuses the spawn; it never falls back to `cc`.

## What the three harnesses do differently

| | Claude Code (`cc`) | hermes-agent (`hermes`) | Codex CLI (`codex`) |
|---|---|---|---|
| Launch, per turn | `claude -p <brief>` | `hermes chat --query-file - --format stream-json ...`, brief on stdin | `codex exec --json -s <sandbox> -m <model> -`, brief on stdin |
| Signed prompt | `--system-prompt`, which replaces the CLI's own prompt | `agent.system_prompt` in a private config; hermes adds its own identity and tool text before it and a runtime block after it | `model_instructions_file` in a private config, which replaces Codex's built-in instructions |
| Repo and user instructions | `--setting-sources ''` (the general row loads `user`) | `--ignore-rules` (skips `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `SOUL.md`, memory) | `project_doc_max_bytes = 0`, a swept private `CODEX_HOME`, and an untrusted project (its `.codex/` never loads) |
| Tool limits | `--allowedTools`, `--disallowedTools` and `--permission-mode` from `capability_tool_grant()` | toolsets (`-t file` or `-t file,terminal`) plus the `eph-gate` plugin, which checks every call against the same lists | Codex's OS sandbox (read-only or workspace-write) plus eph-gate as a `PreToolUse` hook, checking every call against the same lists |
| General worker | `auto` permission mode on native Claude, `--dangerously-skip-permissions` on other backends, deny list on top | eph-gate floors first, then your exec gate ([hermes.md](hermes.md#plugging-in-your-own-exec-gate-general-worker-only)); none configured means deny | same as hermes |
| Resuming | `--resume <session_id>` | `--resume=<session>` inside a private `HERMES_HOME` that lives as long as the worker | `resume <thread id>` inside a private `CODEX_HOME` that lives as long as the worker |
| Result | the `--output-format json` wrapper | the stream-json `result` record, repacked into the same wrapper | the `--json` events (final agent message, thread id, usage), repacked into the same wrapper |
| Turn cap | `--max-turns` | `--max-turns`, plus `--run-budget` asking it to wrap up early | none; the time limit is the only hard cap |
| Time limit | the process is killed | the whole process group is killed | the whole process group is killed |
| Backends | native Claude or a registry backend | non-Anthropic registry backends only | its own ChatGPT login; the model comes from a registry `codex` block |
| Capabilities | all | `mr-review`, `write`, `code` (only with `CODE_NONCLAUDE_OK=1`), `general`; the rest are refused | same as hermes |
| Live transcript | yes, with `BROKER_STREAM_ENABLED` | no | yes, with `BROKER_STREAM_ENABLED`: a broker-written, redacted record of messages, reasoning summaries, command lines with the last 3000 characters of their output, per-edit diffs of worktree files, plan items and tool names, never MCP arguments or results |
| Checks before each turn | prompt present; MCP config present for general and inbox | prompt and binary present, private home swept, gate probe passes, interpreter has no editable or relative path entries | prompt, binary and login file present, private home swept, Codex reports the gate hook loaded, enabled and trusted, gate probe passes |

## The Codex harness

`adapters/codex.py` (`CodexHarness`) runs a turn on the Codex CLI with its own ChatGPT login.
`adapters/codex_gate.py` is the per-call gate it installs. Settings are in
[configuration.md](configuration.md#the-codex-harness); `tests/test_codex.py` fakes the `codex` binary. The flags
and config keys were checked against the CLI version named in `CODEX_VERIFIED`; re-check them when you upgrade.
`CodexHarness` refuses to build at all (`CodexRefused`, fail-closed) unless `codex --version` matches
`CODEX_VERIFIED_VERSION` exactly (a prerelease suffix such as `-rc1` is never an exact match, whatever its
release numbers say, unless `CODEX_ALLOW_PRERELEASE=1`), and unless the `codex-linux-sandbox` helper every turn
execs to actually sandbox a command resolves on `PATH` - bump `CODEX_VERIFIED_VERSION` deliberately, after
re-checking `CODEX_VERIFIED`'s claims, never as a reflex to a refusal. The version is re-checked before every
spawn too (cheaply: only a `stat()` unless the binary file itself changed since the last check), so an in-place
`codex` upgrade under a running broker is caught on the next turn rather than only at the next broker restart.
Codex finds its sandbox helper next to its own executable (arg0); the adapter does not configure it.
`tests/test_codex_canary.py` checks the installed binary: with no login it loads the broker's `config.toml` under
`--strict-config` and confirms every disabled feature is off, `code_mode_host` is on and `unified_exec_tty` is
off; with `CODEX_CANARY=1` (plus `CODEX_AUTH_FILE` and `CODEX_CANARY_MODEL`) it runs one real turn behind a
deny-all hook and requires `apply_patch`, web search and a shell command to be denied or absent (and, for a
code_mode_only model, a nested call's `tool_use_id` to start `exec-`). It prints a report: the tool names and
shell argument keys the hook saw, error events, and whether a login refresh rewrote the auth file through the
symlink. Run it before you set any row to codex and after every Codex upgrade.
`tests/test_codex_probe.py` (the workdir-bypass fix) also needs the real binary, but no login at all: it drives it
against a throwaway localhost mock of the OpenAI Responses API, proving the offline probe's findings
stay true - every nested call hooked and isolated, the
nested `tools` surface within `codex_gate.EXPECTED_NESTED_TOOLS`, `tty`/`write_stdin` inert, the workdir bypass
fixed, a namespaced `spawn_agent` denied as unmapped-tool. Both are skipped (and pass) with no `codex` binary on
`PATH` / `CODEX_BIN`.

**Launching non-interactively.** One headless `codex exec --json --strict-config -s <sandbox> -m <model> -` per
turn (`--strict-config`: a config key this Codex doesn't know stops the run rather than being ignored), brief on
stdin, cwd the worktree, environment from `scrub_worker_env` with every `CODEX_*` and `OPENAI_*` name dropped
(no API-key billing, no second home, no sandbox override) and `CODEX_HOME` set. Each worker gets a private
`CODEX_HOME` under `CODEX_HOME_ROOT`, created on the first turn and removed with the worktree. Before every turn
it is swept down to what Codex wrote and resume needs (`sessions/`, `archived_sessions/`, `log/`, `*.sqlite*`),
so a planted `AGENTS.md`, `rules/`, `skills/` or config never survives into a turn. The model and effort always go
in explicitly, from the backend's `codex` registry block (or `CODEX_BACKEND`'s).

**Auth.** `<home>/auth.json` is a symlink to `CODEX_AUTH_FILE`. The broker only checks that the file exists; it
never opens, reads or copies it (a copy would fork the refresh token). Removing the home removes only the link.
The config pins `cli_auth_credentials_store = "file"` and `forced_login_method = "chatgpt"`, so Codex uses that
file and never a keyring copy. Concurrent codex workers share one login, so a refresh in one rewrites the file for
all of them; the canary reports whether a refresh wrote through the link.

**Passing the signed prompt.** The signed prompt is written to `<home>/eph-instructions.md` and named by
`model_instructions_file`, which replaces Codex's built-in instructions. `project_doc_max_bytes = 0` keeps any
`AGENTS.md` out (`worktree.py` removes them too), and the project is never marked trusted, so a repo's `.codex/`
config and hooks don't load.

**Resuming a session for forwarded events.** The thread id from `thread.started` is stored on
`worker.session_id`, even when the turn fails. A resumed turn runs `codex exec ... resume <thread id> -`; the id
must be a UUID before it reaches the command line. Session rotation needs nothing here.

**Sandbox and approvals.** `approval_policy = "never"` always, since nobody can answer an ask.

- `mr-review`: `read-only` sandbox.
- `write`, `code`, `general`: `workspace-write`, writable roots the worktree plus Codex's defaults, `/tmp` and
  `$TMPDIR` (left writable so test runners can use temp dirs; the gate still decides every command and edit, and
  `apply_patch` is confined to the worktree). Network off unless `CODEX_SANDBOX_NETWORK=1`. `code` also needs
  `CODE_NONCLAUDE_OK=1`.
- Anything else, unknown values included: refused.

Commands Codex runs don't inherit `CODEX_HOME` (`shell_environment_policy.exclude`).

**Tools the hook never sees are switched off.** Hosted tools (web search, image generation) run on OpenAI's side,
and Codex skips `PreToolUse` for MCP tools, so the gate can't stop either. The config sets `web_search =
"disabled"` and turns off every feature in `DISABLED_FEATURES` (`apps`, `plugins`, `multi_agent`,
`image_generation`, `view_image`, browser and computer use, and others). The worker keeps the shell,
`apply_patch` and `update_plan`.

**code_mode_host is explicitly ON** (not merely left off `DISABLED_FEATURES`), because every model the ChatGPT
login offers on 0.164.0 is code_mode_only: without it, such a model can call no tool at all (`hooked_tools=[]`;
safe but useless). ALWAYS written together with **`unified_exec_tty = false`** (the workdir-bypass fix): without it, an
`exec_command(tty: true)` session keeps stdin open and `write_stdin` actually feeds it - and `write_stdin` never
reaches `PreToolUse` at all, so the gate could never stop that. With `unified_exec_tty = false`, Codex itself
rejects `tty: true` and closes every session's stdin, so `write_stdin` fails "stdin is closed" regardless of the
gate - stronger than any gate-side heuristic. `goals` and `sleep_tool` are off too, narrowing the nested surface
further (`create_goal`/`get_goal`/`update_goal`, `clock.sleep`); the gate would default-deny them anyway.
Enabled, code mode's nested `tools.*` calls reach `PreToolUse` with the SAME event shape as a classic tool call,
so the hook maps the shell one (`exec_command`) through the identical rules as `Bash` - including the workdir
fix below - and `apply_patch` under code mode is already the same tool name. `write_stdin` is also denied
outright by the gate as a second belt (the real floor is `unified_exec_tty = false` above); `Bash` / `exec_command`
additionally refuse to ever start a bare stdin-reading interpreter (a shell or script interpreter given no script
file and no inline-code flag, e.g. plain `bash` or `python3`), in every capability, as a third belt. What this
cannot prove is whether the code-mode JS runtime itself has any reach to the filesystem / network / processes /
env outside `tools.*` - only the real binary can show that (`tests/test_codex_canary.py`,
`tests/test_codex_probe.py`); see `CODEX_VERIFIED`.

**The workdir bypass fix (HIGH, pre-existing on `main` whatever code_mode_host is).** An offline real-binary
probe (no login, a local mock of the OpenAI Responses API) found that Codex 0.164.0 strips `workdir` (also `tty`,
`shell`, `login`, `sandbox_permissions`) from the `tool_input` this hook sees on every `Bash` / `exec_command`
call - the event's `tool_input` is just `{"command": cmd}` - but still runs the command in that workdir; the
event's own `cwd` is always the session cwd, never the real one. `exec_command({cmd:"cat hostname",
workdir:"/etc"})` passed the gate and printed `/etc/hostname`: a relative path was checked against the worktree
while the command actually ran somewhere else, so the check proved nothing. The fix does not try to recover
`workdir` (a call carrying one is denied outright, whatever it names); instead it confines the command in its
own TEXT: every `Bash` / `exec_command` command must start with the EXACT literal `cd <abs worktree> || exit 1; `
and contain no OTHER `cd`. Whatever hidden directory Codex actually starts the shell in, an absolute `cd` into
the worktree moves it there before anything else runs, and with no second `cd` available to leave again, every
relative path really is relative to the worktree from then on. The prefix ends in `|| exit 1; ` (a statement
terminator), not `&& ` (H1, eph-broker !185 review): `cd WT && A & B` backgrounds `(cd WT && A)` into a forked
subshell, so the `cd` never reaches the parent shell and `B` runs in whatever hidden workdir Codex actually
started the shell in instead; `cd WT || exit 1; ...` always runs the `cd` to completion in the parent shell
first, whatever follows. When the part after the prefix does not even parse (e.g. a bare `&`), the gate denies
outright rather than skipping the second-cd / bare-interpreter checks. The worker's signed prompt is told the
exact required prefix (`codex_gate.worker_system_prompt`, appended the same way `hermes.worker_system_prompt`
appends `gate.PATCH_EDIT_RULE`) for EVERY capability, `mr-review` included (M1, eph-broker !185 review): it
still reads through the shell and needs the prefix on its reads too, even though it is otherwise read-only.

Codex's sandbox runs as the broker's user, so the same-user reads in
[security-residual-same-uid.md](security-residual-same-uid.md) are covered by the gate below, not by the sandbox.

**The per-call gate (eph-gate on Codex hooks).** The private home's `hooks.json` has one `PreToolUse` hook,
matcher `*`, that runs `codex_gate.py` under the broker's own Python (`-I`, so nothing in the worktree can be
imported) with this turn's policy file. The policy is `hermes.gate_policy()`, built from the same
`capability_tool_grant()` lists and secret floor a `cc` worker gets, so all three harnesses enforce one rule set.
The hook maps Codex's tools onto eph-gate:

- `Bash` or `exec_command` (the code_mode_only nested shell call, the code-mode gating fix - same rules either
  way): the command must start with the exact literal `cd <abs worktree> || exit 1; ` and carry no `workdir` /
  `cwd` argument and no other `cd` (the workdir-bypass fix, see above) - denied outright otherwise, as is a rest
  that does not even parse (H1, see above). Past that prefix,
  where every segment is a plain read
  (`cat`, `head`, `tail`, `wc`, `nl`, `ls`, `grep`, `rg`, by bare name): checked as Claude Code's `Read` / `Grep`
  / `Glob` (the capability must grant them, every path stays in the repo and off the secret floor, `rg`'s
  program-running options are denied, a content search walks its tree for secret files). Codex reads through the
  shell, so this is what keeps `mr-review` and `code` workers able to read.
- Any other `Bash` / `exec_command` (past the required prefix): eph-gate's terminal check (the `Bash(...)` allow
  and deny lists, the command lexer, the general worker's exec gate). `mr-review` gets no other shell. Either
  name is denied outright, in every capability, when the command would start a bare stdin-reading interpreter (no
  script file, no inline code) - see code_mode_host above.
- `apply_patch`: every file it adds, updates, deletes or moves to must pass the edit check (edits granted, inside
  the worker cwd, no `.git`, no secret).
- `update_plan` is allowed; `write_stdin` is denied outright; every other tool - MCP tools, `create_goal` /
  `get_goal` / `update_goal`, `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource`,
  `clockcurr_time`, a namespaced `collaboration*` call such as `spawn_agent` (REAL even with `multi_agent =
  false` - the gate's default-deny is the only thing stopping an un-hooked sub-agent, not the feature flag)
  included - is denied as unmapped-tool.

A deny is exit 2 with the reason on stderr; an allow is silence. Denials are reported in the result, as on hermes.
Codex lets a call through when a hook runs past its timeout (`HOOK_TIMEOUT_S`, 30 s), so the hook sets its own
alarm 5 s before that and denies the call if the alarm fires first.
Codex silently skips an untrusted hook and lets a call through when a hook never answers, so before every turn
the harness writes the policy and `hooks.json`, records trust for exactly that hook (`hooks.state.<key>`), asks
`codex app-server` (`hooks/list`) to confirm Codex resolved it as an enabled, trusted `preToolUse` hook with
matcher `*` and the exact command (and no other non-managed trusted hook; stdin stays open until the answer comes
back, because Codex 0.164 exits on end of input without answering a request still in flight), then runs the hook once to see the
probe answer with the turn's nonce and a `git push` denied. Any failure refuses the turn before `codex exec` runs.
The config never sets `bypass_hook_trust`, and a `[features] hooks = false` anywhere would fail the check.

**Caps.** Codex has no per-turn tool-call cap, so the capability's time limit is the only hard cap. The process
runs in its own session and the whole group is killed on the time limit or teardown.

**Parsing the result.** From the `--json` events: `thread.started` (thread id), the last `agent_message`,
`turn.completed` (usage, which Codex reports cumulatively per thread, so the per-turn difference is only a usage stat;
rotation reads the context size from the thread's rollout `token_count` event instead), `turn.failed` / `error`. They are repacked into `{"result", "session_id", "usage"}` for
`adapter.envelope_parser.try_parse`. A non-zero exit, an error event or a missing `turn.completed` returns `None`,
whatever the text says. The one exception is an `error` event starting `Reconnecting`, which Codex emits for each
stream retry before the turn continues. A turn that really fails still ends with `turn.failed` or without
`turn.completed`. Failed-turn captures, and `last_output` when no events parsed, keep only thread, turn and error
records, never tool output.

**Not covered.** A Codex reviewer's verdict never auto-merges under CD (only `cc`, and hermes with
`CD_ACCEPT_HERMES`). A Codex row never takes the Anthropic usage-cap fallback chain. The inbox capabilities and
site capabilities stay on `cc`.
