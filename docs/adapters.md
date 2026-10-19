# Adding a harness

A harness is the agent CLI that runs a worker's turns. It takes the signed prompt and the brief, calls a model,
uses tools inside the worktree and prints a result. Two ship: Claude Code (`cc`, the default) and hermes-agent
(`hermes`, see [hermes.md](hermes.md)). This page explains the seam a third one plugs into, with Codex as a
worked example.

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
  `HermesHarness.one_turn` in `adapters/hermes.py`.

So `CCAdapter` covers more than Claude Code: it's the lifecycle every harness shares. A new harness belongs at
the turn layer, the way hermes does it. A new `Adapter` subclass would have to rebuild the worktree cut, the
prompt gate, the caps, the retry, publishing and every escalation path, and `app.py` has no switch for it.

## The Adapter interface

`adapters/base.py` defines four required methods and three optional ones. `key` is the subject key, such as
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

`CCAdapter` also has `turn_deadline_extra(key)`. The driver reads it if it exists, so a capability with a longer
time limit than the global one keeps the same headroom before a force-close.

Refusals happen in the worker thread, never in `spawn`. A missing prompt, an unknown capability or a missing
binary is logged, reported to the escalation handle and leaves nothing running.

## The turn seam

`CCAdapter._one_turn` reads `route.adapter`. For `cc` it builds the `claude` command line itself. For `hermes` it
builds `HermesHarness` on first use (so a broker that never routes to hermes never imports it) and calls:

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
  Teardown currently uses hermes' `_kill_group` for that.

It returns the parsed envelope, or `None` for a parse miss. A miss gets one warm re-prompt (a resumed turn asking
for a clean envelope), then an escalation.

## The envelope contract

`envelope.EnvelopeParser.try_parse(raw, run_id, route)` reads one JSON wrapper, the shape Claude Code prints with
`--output-format json`:

```json
{"result": "<the worker's final text>", "session_id": "<id>", "usage": {"input_tokens": 0, "output_tokens": 0}}
```

The last `{...}` block inside `result` is the worker's envelope. `status` is required; `summary`, `wake` (a list)
and `needs_operator` (only a literal `true` counts) are optional. The status has to be one the capability allows:
`pass` or `block` for `mr-review`, `done` or `blocked` for everything else. A capability with no allowed set is a
parse miss. The parser adds `session_id`, `tokens`, `run_id` and `harness` (the route's `adapter` value, which
also ends up in the hidden tag on MR verdict notes).

A new harness should repack its own output into that wrapper, as hermes does with its stream-json `result`
record (`parse_stream`), so `envelope.py` stays the one parser for every harness.

Only a clean run may produce an envelope. A non-zero exit, an error record or a killed process is a parse miss,
even if the text happens to contain a valid envelope. Both shipped harnesses work that way. Anything the harness
adds after parsing, like hermes' `gate_denials`, has to come from its own records, never from the worker's text.

## How a route picks its harness

- `Route.adapter` in `routing.py`, `cc` by default. The allowed names are `routing.ADAPTERS`. `Route` refuses any
  other value when the table loads, and `_one_turn` refuses it again at turn time.
- Built-in rows set it in code. The MR reviewer row reads `MR_REVIEW_ADAPTER`
  ([configuration.md](configuration.md#choosing-the-reviewers-model)).
- Rows in `config/routes.json` have no harness field and run on `cc`, unless the label below moves a ticket.
- A `hermes` label moves one ticket's worker onto hermes (`routing.apply_adapter_label`). It only counts from
  creators in `MODEL_LABEL_TRUSTED_CREATORS`, never on a row with a pinned backend, and only for capabilities in
  `HERMES_LABEL_CAPABILITIES` (`general`). The ticket also needs a non-Anthropic backend, normally from a
  `model:` label. A label on the wrong capability or with an Anthropic backend refuses the spawn; it never falls
  back to `cc`.

## What the two harnesses do differently

| | Claude Code (`cc`) | hermes-agent (`hermes`) |
|---|---|---|
| Launch, per turn | `claude -p <brief>` | `hermes chat --query-file - --format stream-json ...`, brief on stdin |
| Signed prompt | `--system-prompt`, which replaces the CLI's own prompt | `agent.system_prompt` in a private config; hermes adds its own identity and tool text before it and a runtime block after it |
| Repo and user instructions | `--setting-sources ''` (the general row loads `user`) | `--ignore-rules` (skips `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `SOUL.md`, memory) |
| Tool limits | `--allowedTools`, `--disallowedTools` and `--permission-mode` from `capability_tool_grant()` | toolsets (`-t file` or `-t file,terminal`) plus the `eph-gate` plugin, which checks every call against the same lists |
| General worker | `auto` permission mode on native Claude, `--dangerously-skip-permissions` on other backends, deny list on top | eph-gate floors first, then an external policy judge; no judge configured means deny |
| Resuming | `--resume <session_id>` | `--resume=<session>` inside a private `HERMES_HOME` that lives as long as the worker |
| Result | the `--output-format json` wrapper | the stream-json `result` record, repacked into the same wrapper |
| Turn cap | `--max-turns` | `--max-turns`, plus `--run-budget` asking it to wrap up early |
| Time limit | the process is killed | the whole process group is killed |
| Backends | native Claude or a registry backend | non-Anthropic registry backends only |
| Capabilities | all | `mr-review`, `write`, `code` (only with `CODE_NONCLAUDE_OK=1`), `general` (label only); the rest are refused |
| Live transcript | yes, with `BROKER_STREAM_ENABLED` | no |
| Checks before each turn | prompt present; MCP config present for general and inbox | prompt and binary present, private home swept, gate probe passes, interpreter has no editable or relative path entries |

## Worked example: Codex

Codex is not a supported harness. This section sketches what adding one would take, to show the shape of the
work. The Codex CLI flags named here are illustrative; check each one against the version you install.

### The pieces to add

1. `adapters/codex.py` with a `CodexHarness` class and a `one_turn` method with the signature and contract
   above.
2. `"codex"` in `routing.ADAPTERS`.
3. A branch in `CCAdapter._one_turn` next to the hermes one, importing the module lazily.
4. Process-group teardown, if the harness starts its own session (today teardown reaches for hermes'
   `_kill_group`).
5. Tests with a fake `codex` binary, the way `tests/test_hermes.py` fakes hermes, and a line in
   `tools/check.sh` if they live in a new file.
6. A page like [hermes.md](hermes.md) for its install and its quirks.

### What `one_turn` has to map

**Launching non-interactively.** Run Codex's non-interactive mode (`codex exec`) once per turn with the brief
on stdin, the worktree as its working directory and the environment from `scrub_worker_env`. Give each worker a
private config home (`CODEX_HOME`), created on the first turn and removed with the worktree, so it never reads
the broker user's own config, credentials or session history. Always pass an explicit model. Resolve the
route's backend through the same registry `load_backend` reads, and refuse one that doesn't resolve.

**Passing the signed prompt.** The signed prompt has to be the instructions Codex runs under. Find the setting
that replaces Codex's base instructions, not one that appends to them. If the CLI can only add to its own
prompt, say so on the harness page, the way hermes.md does, because then the signed prompt isn't the whole
system prompt. `worktree.py` already removes `AGENTS.md` and `CLAUDE.md` from the worktree, and the private home
keeps user-level instruction files out.

**Resuming a session for forwarded events.** Read the session id from Codex's output and store it on
`worker.session_id`, even when the turn fails. When `resume_session` is set, continue that session
(`codex exec resume <id>`) instead of starting a new one. Validate the id against a strict pattern before it goes
on the command line, as hermes does. Session rotation needs nothing here: `_one_turn` passes `None` when it's
time for a fresh session.

**Tool and write limits.** Map each capability to what Codex can enforce, and refuse the rest.

- `mr-review`: a read-only sandbox, no network, approvals off (`--sandbox read-only`, and an approval policy
  that never asks, since nobody can answer).
- `code` and `write`: writes confined to the worktree, network off so a `git push` can't reach anything. The
  `code` test runner allow-list and the deny lists in `adapters/cc.py` (secret files, `.git` writes, destructive
  shell) still have to hold. If Codex can't express them, the harness needs a per-call gate like hermes'
  `eph-gate`, or it refuses that capability.
- `general` and `inbox`: refuse until there's a per-call gate. Reading the broker's secret files is the main
  risk, and a sandbox that allows reads everywhere doesn't stop it.
- Anything else, unknown values included: refuse.

Codex's sandbox is an OS-level one, which the shipped harnesses don't have. It still runs as the broker's user,
so the same-user reads in [security-residual-same-uid.md](security-residual-same-uid.md) apply unless the
sandbox blocks them. Check before you claim more.

**Caps.** Use the per-capability turn cap if Codex has one; if not, the time limit is the only hard cap, and the
harness page has to say so. Start the process in its own session and kill the whole group on the time limit.

**Parsing the result.** Read Codex's machine-readable output (`--json`), take the final agent message, the
session id and the token usage, and repack them into `{"result", "session_id", "usage"}`. Then call
`adapter.envelope_parser.try_parse`. A non-zero exit or an error event returns `None`, whatever the text says.
