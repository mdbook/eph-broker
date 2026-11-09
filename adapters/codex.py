#!/usr/bin/env python3
"""CodexHarness: run ONE eph worker turn on the native Codex CLI (its own ChatGPT login) instead of Claude Code.

Selected per ROW by `Route.adapter == "codex"` (routing.py: a <ROW>_ADAPTER knob, default "cc", or a trusted
`codex` ticket label). Everything ABOVE the turn is CCAdapter's, unchanged (worktree cut, ticket brief, prompt
hash gate, caps, warm re-prompt, session rotation, publishing, every escalation), exactly as for hermes:
CCAdapter._one_turn hands the turn to `CodexHarness.one_turn`, which fills the same Worker fields (proc / in_turn /
turn_started / turn_wall_clock / hard_killed / turns / last_output / session_id / kill_group).

LAUNCH, per turn (headless, never the TUI or a shared app-server daemon):
    codex exec --json -s <sandbox> -m <model> -          (first turn; brief on stdin)
    codex exec --json -s <sandbox> -m <model> resume <thread id> -     (a forward / the warm re-prompt)
  with cwd = the worktree, env = cc.scrub_worker_env (every CODEX_* / OPENAI_* name popped, then CODEX_HOME set),
  in its own process group (start_new_session) so the wall clock kills the whole group.

CODEX_HOME: one PRIVATE home per worker under CODEX_HOME_ROOT (mkdtemp, 0700), never ~/.codex. It lives across the
  worker's turns (Codex keeps the thread rollouts there, which `resume` needs) and is removed with the worktree
  (cc._run_worker's finally; weakref.finalize is the backstop); homes older than HOME_MAX_AGE_S are swept when the
  harness is built. Before EVERY turn the home is swept down to what Codex itself wrote and resume needs
  (_HOME_KEEP: sessions/, archived_sessions/, log/, *.sqlite*); everything else (a planted AGENTS.md, rules/,
  skills/, config.toml, hooks.json, auth.json) is removed and the broker's own files are rewritten fresh.

AUTH: <home>/auth.json is a SYMLINK to the operator's CODEX_AUTH_FILE (absolute path; unset = refuse). The broker
  never opens, reads or copies it (a copy would fork the refresh token); only the link is ever removed.

MODEL / EFFORT: from the route backend's `codex` block in the spawn registry (SPAWN_MODELS_JSON):
      "chatgpt": {"kind": "codex", "codex": {"model": "<codex model id>", "effort": "high"}}
  If the route's backend has no such block (native Claude, a FLEET_MODE answer, a fallback rung), the entry named
  by CODEX_BACKEND is used; neither = refuse. The model always goes on the command line and in config.toml.

SIGNED PROMPT: written to <home>/eph-instructions.md and named by `model_instructions_file`, which REPLACES Codex's
  built-in instructions. project_doc_max_bytes = 0 keeps any AGENTS.md out (worktree.py strips them too), and
  the sweep keeps a $CODEX_HOME/AGENTS.md out. The workspace is never marked trusted, so a repo's .codex/ config
  and hooks do not load.

SANDBOX + APPROVALS (Codex's own OS sandbox; approval_policy = "never": nobody can answer an ask):
    mr-review          read-only
    write / code       workspace-write, writable root = the worktree, network off (CODEX_SANDBOX_NETWORK=1 turns
                       it on, e.g. for mcp-ro.sh; the gate still decides every command). code also needs
                       CODE_NONCLAUDE_OK=1, the same opt-in a non-Claude code turn needs on cc and hermes.
    general            workspace-write, same network rule; every effectful call also needs the exec gate's allow
                       (EXEC_GATE, unset = deny), as on hermes.
    anything else      REFUSED (inbox / inbox-chat need the Gmail MCP; read / site capabilities have no mapping).

PER-CALL GATE (eph-gate on Codex hooks): the private home's hooks.json has ONE PreToolUse hook, matcher "*",
  running adapters/codex_gate.py under the broker's own python (-I) with this turn's policy file. The policy is
  hermes.gate_policy() (cc.capability_tool_grant + the secret floor, never a copied list), so cc, hermes and codex
  enforce one rule set. Codex skips an untrusted or edited hook SILENTLY and fails OPEN on a hook that never
  answers, so before every turn the harness (fail-closed, CodexRefused = no launch):
    1. writes the policy (fresh nonce) and hooks.json,
    2. asks Codex itself (`codex app-server` hooks/list) which hooks it resolved, and
       records trust (hooks.state.<key>.trusted_hash) only for the hook in THIS hooks.json,
    3. asks again and requires that hook to be a preToolUse hook, enabled, trusted, matcher "*", with the exact
       command written, plus no hooks/list error and no other non-managed trusted hook (the one exception: the
       steer.py PostToolUse hook for mid-turn dashboard messages, which must resolve exactly as written too;
       it only adds context after a tool result and decides nothing, so it is not probed),
    4. runs the hook command directly with a synthetic event: the probe tool must be denied with the nonce and a
       floored `git push` must be denied.
  Tools that would never reach the hook (web_search, hosted image generation, MCP-backed apps / plugins / browser
  and computer use, sub-agents, view_image) are switched off in config.toml (DISABLED_FEATURES), and `exec
  --strict-config` refuses to start on a key this Codex does not know. The hook arms its own SIGALRM deadline
  below HOOK_TIMEOUT_S (Codex fails open on a timed-out hook).
  The config never sets bypass_hook_trust and the CLI never gets --dangerously-bypass-hook-trust; a [features]
  hooks = false anywhere would make step 3 fail. Residual (same uid, as for hermes): code from an earlier turn
  rewriting the broker install or the home between the check and the run.

CODE MODE (code_mode_host, explicitly ENABLED, not merely left off DISABLED_FEATURES): on 0.164.0 every model this
  ChatGPT login offers is code_mode_only - its tools are nested under one code-mode exec-JS tool, never exposed as
  direct tool calls, so with code_mode_host off (the pre-code-mode-gating-fix default) such a model can call NO
  tool at all (hooked_tools=[]; fail-closed but useless). codex.codex_config() now writes `code_mode_host = true`
  instead of false, so a code_mode_only model can use shell + apply_patch. ALWAYS written together with
  `unified_exec_tty = false` (the workdir-bypass fix, verified under --strict-config against an offline
  real-binary probe report): with tty on, an `exec_command(tty:true)` session's stdin
  stays open and `write_stdin` actually feeds it, un-gated (LOW-1); with it off, Codex itself rejects `tty:true`
  and every session's stdin is closed, so `write_stdin` fails "stdin is closed" whatever the gate does - the
  config enforces this, which is stronger than any heuristic in codex_gate.py. `goals` and `sleep_tool` are also
  off (DISABLED_FEATURES), narrowing the nested surface further (create_goal/get_goal/update_goal, clock.sleep).
  Enabling code_mode_host at all is safe ONLY because adapters/codex_gate.py gates every nested `tools.*` call the
  same way it gates a classic tool call:
    - the nested shell call (observed as `exec_command`, distinct from the classic `Bash`) runs through the exact
      same Read/Grep/Glob and terminal rules as `Bash` (codex_gate._decide_exec serves both tool names), and
      since the workdir-bypass fix ALSO requires the exact literal `cd <worktree> || exit 1; ` prefix on every such command (see
      codex_gate's WORKDIR BYPASS FIX) - Codex hides the real `workdir` it executes in from the hook but still
      honours it, so the command text itself must force its own cwd; a bare `workdir`/`cwd` argument is denied,
    - `apply_patch` nested under code mode is the same tool name as the classic one, so it is already covered,
    - `write_stdin` (feed a running exec session) is denied outright too: Codex never routes it through
      PreToolUse (LOW-1, eph-broker !169 review) - the real floor is `unified_exec_tty = false` above; the gate
      additionally refuses to ever START a bare stdin-reading interpreter (a shell or script interpreter given no
      script / inline code) via `Bash` or `exec_command`, in every capability, as a second belt,
    - any other nested tool name the gate does not recognise is denied (unmapped-tool, e.g. create_goal,
      list_mcp_resources, list_mcp_resource_templates, read_mcp_resource, a namespaced `collaboration*` call such
      as spawn_agent), the same fail-closed default as an unmapped classic tool.
  What this file CANNOT prove: that the code-mode JS runtime itself (the sandboxed interpreter that runs the
  model's JS and calls back into `tools.*`) has no ambient reach to the filesystem, network, processes or env
  outside that `tools.*` surface. That is Codex's own sandbox, not the gate's; it can only be checked against the
  real binary (tests/test_codex_canary.py's offline probe, and the real-login canary), never asserted here. If a
  future canary run shows the JS runtime can bypass tools.*, code_mode_host must go back to a hard refuse here,
  not stay enabled on a hope. See CODEX_VERIFIED.

CAPS: Codex has no per-turn iteration cap flag, so the per-capability wall clock is the only hard cap (process
  group SIGKILL). turn_capped is never set.

RESULT: `--json` events: thread.started (thread id), item.completed agent_message (the final text),
  turn.completed (usage, CUMULATIVE per thread, so the per-turn delta is reported), turn.failed / error.
  Stdout is drained live for dashboard progress. With BROKER_STREAM_ENABLED, a redacted transcript under
  stream.PROJECTS_DIR/eph-codex survives the private home's cleanup: event metadata plus message and thought
  text, command lines with their output tail, per-edit worktree diffs, plan items and MCP tool names (see
  _item_detail), never MCP arguments or results.
  Repacked into the cc `--output-format json` wrapper ({result, session_id, usage}) so envelope.EnvelopeParser is
  the one parser. Only a clean run yields an envelope: rc != 0, no turn.completed, or any error event is a parse
  miss (warm re-prompt, then an escalation), whatever the text says. Gate denials come from the hook's own file.

Verified against: see CODEX_VERIFIED. Bump it deliberately after re-checking the flags and keys above.
"""
import difflib, glob, json, logging, os, re, shlex, shutil, stat, subprocess, sys, tempfile, threading, time, weakref
import steer
import stream
import worktree

from . import cc as _cc
from . import codex_gate
from .cc import _NO_OVERRIDE, scrub_worker_env, SPAWN_MODELS_JSON
from .hermes import gate_policy, read_denials, _kill_group
from .hermes_gate import POLICY_FILE, DENIALS_FILE, gate as _gate

log = logging.getLogger("eph-broker.codex")

CODEX_VERIFIED = ("Codex CLI 0.154.0 (exec --json, app-server hooks/list, hooks.state trusted_hash, PreToolUse exit-2 "
                  "deny); model_instructions_file per the config docs. Config keys and DISABLED_FEATURES checked "
                  "against 0.164.0 with `codex features list` + `exec --strict-config`. Hook routing on 0.164.0 is "
                  "proven only by tests/test_codex_canary.py: run it and record the result here before any codex row. "
                  "Mid-turn steering (steer.py PostToolUse additionalContext reaching the MAIN model after a shell "
                  "call) passed test_codex_real_binary_steer_canary on 0.164.0 and 0.166.0 with gpt-6.1-sol "
                  "(2026-11-06); re-run it on any bump. "
                  "code_mode_host (the code-mode gating fix): enabled by codex_config() so code_mode_only ChatGPT "
                  "models can call a tool at all; the nested exec_command/apply_patch/write_stdin coverage is "
                  "fake-codex-unit-tested (tests/test_codex.py) only so far - run the REAL-LOGIN canary's "
                  "code-mode path (tests/test_codex_canary.py) on an actual code_mode_only model and record the "
                  "result here before any codex row serves one. "
                  "The workdir-bypass fix (offline real-binary probe against 0.164.0, mock Responses server, no "
                  "login - see the probe report): confirmed on the REAL binary that (a) "
                  "Bash/exec_command's tool_input the hook sees is `{\"command\": cmd}` ONLY - workdir/tty/shell/"
                  "login/sandbox_permissions are stripped but still honoured when the command actually runs, the "
                  "HIGH workdir-bypass this fixes (codex_gate._decide_exec's required `cd <worktree> || exit 1; ` "
                  "prefix, H1-strengthened per eph-broker !185 review to end in a statement terminator rather "
                  "than `&& ` so a later top-level `&` cannot background the cd into a forked subshell, and to "
                  "deny outright - not judge - an unparseable rest; NOT yet re-run against the real binary with "
                  "the fix in place - that is still owed before any codex row), (b) `[features] unified_exec_tty "
                  "= false` makes Codex reject "
                  "`tty:true` and close every exec_command session's stdin so write_stdin always fails \"stdin is "
                  "closed\", verified under --strict-config, (c) `goals = false` / `sleep_tool = false` remove "
                  "create_goal/get_goal/update_goal / clock.sleep from the nested tool tree, also verified under "
                  "--strict-config, (d) create_goal, clockcurr_time (no `__`, the namespace separator Codex "
                  "drops), list_mcp_resources, list_mcp_resource_templates, read_mcp_resource and a namespaced "
                  "collaborationspawn_agent are all denied as unmapped-tool by the real gate. Still owed: a "
                  "codex-binary-present offline test porting the probe (tests/test_codex.py/"
                  "test_codex_canary.py per the report's section 5), and the real-login canary + strict-config "
                  "test updated to assert the fix and the new feature keys - see tests/test_codex_canary.py's "
                  "module docstring")
# The exact `codex --version` all of the above was verified against. CodexHarness refuses to build (fail-closed,
# CodexRefused) against any other version rather than run this adapter's config keys, feature flags and gate
# assumptions against a binary that may have renamed, removed or changed the behaviour of any of them. Bump this
# deliberately - after re-checking CODEX_VERIFIED's claims against the new version and updating it - never as a
# reflex to a refusal.
CODEX_VERIFIED_VERSION = "0.164.0"
# Captures the release "X.Y.Z" separately from any prerelease/build suffix glued directly onto it
# (`-rc1`, `-beta.2`, ...). M3 residual: a bare `\d+\.\d+\.\d+` search also matched (and
# happily accepted) the release prefix of a prerelease string - "0.164.0-rc1" parsed as the verified "0.164.0"
# even though it is NOT the binary CODEX_VERIFIED was checked against. codex_version() below refuses a match
# with a non-empty suffix unless CODEX_ALLOW_PRERELEASE=1 is set.
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)(-[0-9A-Za-z.]+)?")
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
CODEX_HOME_ROOT = os.getenv("CODEX_HOME_ROOT", os.path.expanduser("~/.cache/eph-codex-homes"))
# Codex only emits `reasoning` items when the model returns a summary; without one the live transcript has no
# thoughts. Off by default like CC_SHOW_THINKING_SUMMARIES: empty omits the key (Codex's own default), "none"
# sends it off explicitly. Checked at import, since Codex rejects an unknown value and every turn would fail.
def _reasoning_summary(raw):
    v = (raw or "").strip()
    if v not in ("", "auto", "concise", "detailed", "none"):
        raise ValueError(f"CODEX_REASONING_SUMMARY={v!r}: use one of auto, concise, detailed, none (or leave it empty)")
    return v


REASONING_SUMMARY = _reasoning_summary(os.getenv("CODEX_REASONING_SUMMARY"))
# L1 preflight: every codex turn execs a `codex-linux-sandbox` helper (via plain PATH lookup) to actually sandbox
# a command, in EITHER sandbox mode. Absent, Codex fails each exec mid-turn with a confusing "No such file or
# directory" (tests/test_codex_probe.py's SANDBOX_HELPER_ERR) instead of never starting the turn at all. Codex
# locates its own copy of this helper itself (arg0 dispatch, next to its own executable) - the adapter does not
# configure or pass it; this constant only names the default this preflight check looks for on PATH.
CODEX_LINUX_SANDBOX_BIN = "codex-linux-sandbox"
HOME_MAX_AGE_S = 24 * 3600
APP_SERVER_TIMEOUT_S = 60
HOOK_TIMEOUT_S = 30          # the hook entry's Codex timeout: above the exec gate's own (EXEC_GATE_TIMEOUT, 5 s).
                             # Codex fails OPEN past it, so codex_gate.py denies itself first (its own copy, pinned
                             # equal by test_codex.py: the hook process imports hermes_gate only, never this module)
GATE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex_gate.py")
HOOKS_FILE, CONFIG_FILE, INSTRUCTIONS_FILE, AUTH_FILE = "hooks.json", "config.toml", "eph-instructions.md", "auth.json"
CODEX_SANDBOXES = {"mr-review": "read-only", "write": "workspace-write", "code": "workspace-write",
                   "general": "workspace-write"}
_SESSION_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", re.ASCII)
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_EFFORT_RE = re.compile(r"[a-z]{1,16}", re.ASCII)
_POPPED_ENV_PREFIXES = ("CODEX_", "OPENAI_")      # API-key billing, a second home, sandbox / trust overrides
_HOME_KEEP = frozenset({"sessions", "archived_sessions", "log"})
_HOME_KEEP_RE = re.compile(r"[\w.-]+\.sqlite(?:-wal|-shm|-journal)?", re.ASCII)
_CAPTURE_KEEP_TYPES = frozenset({"thread.started", "turn.started", "turn.completed", "turn.failed", "error"})
# Every tool surface that is not the shell / apply_patch / update_plan. Hosted tools (image generation) run
# server-side and MCP-backed ones (apps connectors, plugins, browser / computer use) skip PreToolUse ("MCP hooks are
# not supported"), so the gate would never see them: they are switched OFF, not gated. Names checked against
# `codex features list` and `codex exec --strict-config` (CODEX_VERIFIED); --strict-config makes a renamed key fail.
# code_mode_host is deliberately NOT here (the code-mode gating fix): every nested tools.* call it exposes (exec_command, apply_patch,
# write_stdin, ...) still fires PreToolUse and is gated by codex_gate.py, so it is enabled explicitly below instead
# (codex_config's [features] code_mode_host = true), not merely left at whatever Codex's own default is.
# unified_exec_tty is ALSO not here, for the opposite reason: it must be explicitly FALSE (below, pinned equal to
# code_mode_host = true) rather than merely disabled like everything in this tuple.
# goals / sleep_tool (workdir-bypass fix, probe report item 2): narrow the nested surface a code_mode_only model
# gets - goals removes create_goal/get_goal/update_goal, sleep_tool removes the clock.sleep namespace. Both
# verified under --strict-config (see the probe report); the gate denies them as unmapped-tool
# regardless (default-deny), so this is defence in depth, narrowing what the model is even offered.
DISABLED_FEATURES = ("apps", "plugins", "remote_plugin", "multi_agent", "multi_agent_v2", "image_generation",
                     "view_image", "browser_use", "browser_use_external", "computer_use", "in_app_browser",
                     "tool_suggest", "skill_mcp_dependency_install", "memories", "goals", "sleep_tool",
                     "shell_snapshot")
# `codex exec --json` emits an `error` event per stream retry ("Reconnecting... 2/5"); the turn then goes on.
# Only these are not fatal: a turn that really fails still ends in turn.failed or without turn.completed.
_TRANSIENT_ERROR_RE = re.compile(r"\s*Reconnecting\b", re.ASCII)
_STREAM_CLEANUP_S = 1        # one shared grace budget for ALL post-timeout joins and parent reaping


class CodexRefused(ValueError):
    """A route this harness cannot run (capability, model, auth, gate). Raised before any worker process starts;
    CCAdapter._run_worker's except turns it into the alert (fail-closed)."""


def codex_sandbox(capability):
    """Abstract Route.capability -> Codex sandbox mode, or CodexRefused."""
    sandbox = CODEX_SANDBOXES.get(capability)
    if sandbox is None:
        raise CodexRefused(f"codex adapter refuses capability {capability!r}: honoured are "
                           f"{', '.join(sorted(CODEX_SANDBOXES))} (fail-closed)")
    if capability == "code" and not _cc._env_flag("CODE_NONCLAUDE_OK"):
        raise CodexRefused("codex adapter refuses capability 'code': a codex turn is non-Claude and "
                           "CODE_NONCLAUDE_OK=1 is not set (fail-closed)")
    return sandbox


def _codex_block(name):
    entry = _cc._backend_entry(name) if name and name != "claude" else None
    block = entry.get("codex") if isinstance(entry, dict) else None
    if block is None:
        return None
    if (not isinstance(block, dict) or set(block) - {"model", "effort"}
            or not isinstance(block.get("model"), str) or not _MODEL_RE.fullmatch(block["model"])
            or ("effort" in block and not (isinstance(block["effort"], str) and _EFFORT_RE.fullmatch(block["effort"])))):
        raise CodexRefused(f"codex adapter: backend {name!r} has a malformed `codex` block (keys model, effort; "
                           f"plain ids) in {SPAWN_MODELS_JSON} - refusing (fail-closed)")
    return block


def codex_model(backend):
    """Route backend -> (model, effort or None, registry name used). The backend's own `codex` block wins, else
    the CODEX_BACKEND entry's (read at call time); neither = CodexRefused."""
    for name in (backend, os.getenv("CODEX_BACKEND", "").strip()):
        block = _codex_block(name)
        if block is not None:
            return block["model"], block.get("effort"), name
    raise CodexRefused(f"codex adapter: neither the route backend {backend or 'native'!r} nor CODEX_BACKEND names a "
                       f"spawn-registry entry with a `codex` block in {SPAWN_MODELS_JSON} - refusing (fail-closed)")


def toml_str(s):
    """A TOML basic string (JSON escapes are valid TOML escapes)."""
    return json.dumps(str(s), ensure_ascii=False)


def git_writable_roots(cwd):
    """The absolute git dirs a `git add` / `git commit` in worktree `cwd` must write: its own git dir
    (<repo>/.git/worktrees/<name>/ for a linked worktree: index.lock, HEAD) and the common dir (<repo>/.git: objects,
    refs), resolved by git itself, never hardcoded. [] when `cwd` is no git work tree or git fails (fail-closed:
    no extra root). GIT_* env is dropped so an inherited GIT_DIR cannot redirect the lookup.
    Protected `.git`: Codex's workspace-write policy marks `<writable root>/.git` read-only, i.e. a `.git` entry
    BELOW a root. These roots ARE the git dirs (the root is `.../.git` itself, whose own children are not named
    `.git`), so the carve-out does not hit them; the worktree's `.git` pointer file stays read-only, which git
    never writes. tests/test_codex_probe.py proves a real add + commit against the pinned binary."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--absolute-git-dir", "--git-common-dir"],
                           capture_output=True, text=True, timeout=20, env=env)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    out = r.stdout.splitlines()
    if len(out) != 2:
        return []
    roots = []
    for p in out:
        p = os.path.realpath(p if os.path.isabs(p) else os.path.join(cwd, p))
        if os.path.isdir(p) and p not in roots:
            roots.append(p)
    return roots


def codex_config(model, effort, sandbox, instructions_path, network=False, writable_roots=(), shell_path=None,
                 companion_env=None, git_identity=None):
    """The private config.toml for one turn (no secrets). Hook trust is appended by the gate install.
    `writable_roots` (workspace-write only): extra absolute dirs, in practice the worktree's git dirs."""
    lines = ["# written by eph-broker adapters/codex.py before every turn; edits are lost",
             f"model = {toml_str(model)}"]
    if effort:
        lines.append(f"model_reasoning_effort = {toml_str(effort)}")
    if REASONING_SUMMARY:
        lines.append(f"model_reasoning_summary = {toml_str(REASONING_SUMMARY)}")
    lines += ['approval_policy = "never"',
              'allow_login_shell = false',
              f"sandbox_mode = {toml_str(sandbox)}",
              "project_doc_max_bytes = 0",
              f"model_instructions_file = {toml_str(instructions_path)}",
              'web_search = "disabled"',                 # hosted tool: never reaches a PreToolUse hook
              'cli_auth_credentials_store = "file"',     # auth.json (the symlink), never a keyring copy
              'forced_login_method = "chatgpt"',
              "", "[features]"]
    lines += [f"{name} = false" for name in DISABLED_FEATURES]
    # enabled, not merely left off: a code_mode_only model (every ChatGPT model on 0.164.0) exposes NO tool at all
    # unless this is on (the code-mode gating fix). codex_gate.py gates every nested tools.* call it then offers
    # (exec_command, apply_patch, write_stdin, ...) the same way it gates a classic tool call - see the module
    # docstring.
    lines.append("code_mode_host = true")
    # ALWAYS together with code_mode_host = true (the workdir-bypass fix, probe report item 2): with
    # unified_exec_tty on (Codex's own default), write_stdin is advertised AND live (a tty:true exec_command
    # session has open stdin), and the gate never even sees write_stdin (LOW-1, code-mode gating fix review) -
    # so a code-mode model could feed one un-gated.
    # unified_exec_tty = false makes Codex itself reject tty:true ("TTY execution is disabled by config") and
    # close every session's stdin, so write_stdin fails "stdin is closed" whatever the gate does: config-enforced
    # by Codex, stronger than codex_gate.py's own bare-interpreter heuristic (kept only as a second belt).
    lines.append("unified_exec_tty = false")
    if sandbox == "workspace-write":
        lines += ["", "[sandbox_workspace_write]", f"network_access = {'true' if network else 'false'}",
                  "writable_roots = [%s]" % ", ".join(toml_str(p) for p in writable_roots)]
    lines += ["", "[shell_environment_policy]", 'exclude = ["CODEX_HOME"]']
    # Pin the shared launcher's PATH in tool shells, not just the codex parent. Login profiles and
    # shell snapshots must not substitute /usr/bin/ssh for the managed SSH launcher (ticket 2104).
    if shell_path is not None or companion_env:
        lines += ["", "[shell_environment_policy.set]"]
        if shell_path is not None:
            lines.append(f"PATH = {toml_str(shell_path)}")
            lines += [f"{k} = {toml_str(v)}" for k, v in (git_identity or {}).items()]
        lines += [f"{k} = {toml_str(v)}" for k, v in (companion_env or {}).items()]
    return "\n".join(lines) + "\n"


def hook_command(policy_path, python=None):
    """The ONE PreToolUse hook command: the broker's own interpreter, isolated (-I: no PYTHON* env, no user site,
    no script-dir or cwd entry on sys.path), running codex_gate.py with this turn's policy path."""
    return " ".join(shlex.quote(p) for p in (python or sys.executable, "-I", GATE_SCRIPT, policy_path))


def hooks_config(command, steer_command=None):
    cfg = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}]}]}}
    if steer_command:   # mid-turn dashboard messages (steer.py): a PostToolUse hook that only ever adds context
        cfg["hooks"]["PostToolUse"] = [{"matcher": "*", "hooks": [
            {"type": "command", "command": steer_command, "timeout": HOOK_TIMEOUT_S}]}]
    return cfg


def build_argv(codex_bin, model, sandbox, resume_session=None):
    argv = [codex_bin, "exec", "--json", "--strict-config", "-s", sandbox, "-m", model]
    if resume_session:
        if not _SESSION_ID_RE.fullmatch(str(resume_session)):   # fullmatch: no flag, no trailing newline
            raise CodexRefused(f"codex adapter: refusing to resume a malformed thread id {resume_session!r}")
        argv += ["resume", str(resume_session)]
    return argv + ["-"]


def _events(out):
    for line in (out or "").splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield s, obj


def parse_events(out):
    """`codex exec --json` stdout -> the cc wrapper shape, or None when no thread / message / turn record at all.
    usage is the thread's CUMULATIVE total (`cumulative`); one_turn turns it into this turn's delta."""
    thread = text = error = usage = None
    completed = seen = False
    for _s, ev in _events(out):
        typ = ev.get("type")
        if typ == "thread.started":
            thread, seen = ev.get("thread_id") or thread, True
        elif typ == "item.completed" and isinstance(ev.get("item"), dict) \
                and ev["item"].get("type") == "agent_message":
            text, seen = ev["item"].get("text") or "", True
        elif typ == "turn.completed":
            completed, seen = True, True
            usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
        elif typ == "turn.failed":
            err = ev.get("error")
            error, seen = str(err.get("message") if isinstance(err, dict) else err or "turn failed"), True
        elif typ == "error":
            msg = str(ev.get("message") or "error event")
            seen = True
            if not _TRANSIENT_ERROR_RE.match(msg):
                error = msg
    if not seen:
        return None
    usage = usage or {}
    wrapper = {"result": text or "", "session_id": thread or None, "harness": "codex", "completed": completed,
               "cumulative": {"input_tokens": int(usage.get("input_tokens") or 0),
                              "output_tokens": int(usage.get("output_tokens") or 0)}}
    wrapper["usage"] = dict(wrapper["cumulative"])
    if error:
        wrapper["error"] = error
    return wrapper


def capture_stdout(out):
    """Postmortem-safe view of the event stream: thread/turn/error records only. item.* records (command output,
    file text) and non-JSON lines are dropped, counted only (cc.capture_failed_turn redacts and caps the rest)."""
    kept, dropped = [], 0
    for line in (out or "").splitlines():
        s = line.strip()
        try:
            obj = json.loads(s) if s.startswith("{") else None
        except ValueError:
            obj = None
        if isinstance(obj, dict) and obj.get("type") in _CAPTURE_KEEP_TYPES:
            kept.append(s)
        elif s:
            dropped += 1
    if dropped:
        kept.append(f"[{dropped} other event line(s) dropped: tool output / non-JSON never captured]")
    return "\n".join(kept)


def _stream_process(proc, prompt, timeout, on_line):
    """Drain both pipes while feeding stdin, with one deadline covering ALL I/O and process exit.

    A separate writer matters: a large prompt must not block the deadline (or a child writing stderr).
    Waiting for the drains also covers a CLI that exits but leaves descendants holding its pipes open.
    """
    out, err = [], []
    deadline = time.monotonic() + timeout

    def stdout():
        with proc.stdout:
            for line in proc.stdout:
                out.append(line)
                try:
                    on_line(line)
                except Exception as e:
                    log.warning("codex live progress update failed (%s)", type(e).__name__)

    def stderr():
        with proc.stderr:
            err.append(proc.stderr.read())

    def stdin():
        try:
            with proc.stdin:
                proc.stdin.write(prompt)
        except BrokenPipeError:                 # same contract as communicate(input)
            pass

    threads = [threading.Thread(target=f, daemon=True) for f in (stdout, stderr, stdin)]
    for t in threads:
        t.start()
    killed = False
    try:
        for t in threads:
            t.join(max(0, deadline - time.monotonic()))
            if t.is_alive():
                raise subprocess.TimeoutExpired(proc.args, timeout)
        proc.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        killed = True
        # teardown / live_pid_count may already have reaped the CLI. Its descendants still belong
        # to this turn's group and must be killed regardless of the parent's returncode.
        _kill_group(proc)
        cleanup_deadline = time.monotonic() + _STREAM_CLEANUP_S
        for t in threads:
            t.join(max(0, cleanup_deadline - time.monotonic()))
            if t.is_alive():
                log.warning("codex stream thread still alive after timeout cleanup -> proceeding")
        try:
            proc.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            log.warning("codex process still alive after timeout cleanup -> proceeding")
    return "".join(out), "".join(err), killed


_DIFF_FILE_MAX = 512 * 1024    # bigger files get no snapshot and no diff
_DIFF_TEXT_MAX = 20000
_SNAP_FILES_MAX = 200
_SNAP_BYTES_MAX = 16 * 1024 * 1024   # per worker, across turns; least recently edited files go first


def _read_text(path):
    """A regular file's text, or None when it is missing, not regular (a FIFO would block the reader), too big or
    not text. O_NONBLOCK + O_NOFOLLOW + fstat close the race with a path swapped after realpath()."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _DIFF_FILE_MAX:
            return None
        with os.fdopen(fd, "rb") as f:
            fd = None
            return f.read(_DIFF_FILE_MAX + 1).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _git_read_env(root):
    """A minimal env for the broker's read of HEAD in a worker worktree: none of the broker's own env (no tokens),
    no global/system config, no repo discovery above the worktree, and no lazy fetch. The worker can write this
    repo's config and objects, and a promisor remote's planted uploadpack would otherwise run on a missing blob."""
    env = worktree._wt_env(root)
    keep = {k: env[k] for k in ("PATH", "LANG", "LC_ALL") if k in env}
    keep.update({k: env[k] for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_CEILING_DIRECTORIES")})
    keep.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", HOME=os.devnull)
    return keep


def _head_text(cwd, rel):
    """The committed text of `rel` (relative to worktree `cwd`), or None when it isn't in HEAD, is binary or too
    big, is not a blob (a gitlink), or git fails. `cat-file blob`, not `show`: show of a gitlink renders the commit
    and, with a planted log.showSignature + gpg.program, runs that program. No network, no hooks, no fsmonitor,
    none of the broker's secrets in env."""
    argv = worktree._wt_argv(cwd, "-c", "protocol.allow=never", "cat-file", "blob", "HEAD:./" + rel)
    try:
        r = subprocess.run(argv, capture_output=True, timeout=5, env=_git_read_env(cwd), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or len(r.stdout) > _DIFF_FILE_MAX:
        return None
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _remember(snaps, rel, text):
    """Keep the last-seen text of `rel`, evicting the least recently edited files past the count/byte budget."""
    snaps.pop(rel, None)
    snaps[rel] = text
    while len(snaps) > _SNAP_FILES_MAX or (len(snaps) > 1 and sum(map(len, snaps.values())) > _SNAP_BYTES_MAX):
        snaps.pop(next(iter(snaps)))


def _file_diffs(changes, cwd, snaps):
    """Per-file diffs for one finished file_change: Codex reports only path + kind, so the broker diffs the file
    against what it last saw (the previous edit, else HEAD). Only regular files inside the worktree are read.
    With no baseline (the file isn't in HEAD, e.g. untracked or ignored) the edit gets no diff and no counts,
    but its text is kept so the next edit diffs. Both sides are redacted whole before diffing, so a secret split
    across hunk context (a key block without its BEGIN line) is still caught. A shell edit between two
    file_changes lands in the next diff."""
    root = os.path.realpath(cwd) if cwd else None
    files = []
    for c in changes[:20]:
        if not isinstance(c, dict) or not isinstance(c.get("path"), str) or not c["path"]:
            continue
        kind = c.get("kind") if c.get("kind") in ("add", "delete", "update") else "update"
        f = {"path": _cc._redact_secretish(c["path"])[:300], "kind": kind}
        files.append(f)
        if not root:
            continue
        real = os.path.realpath(os.path.join(root, c["path"]))
        if not real.startswith(root + os.sep):
            continue
        rel = os.path.relpath(real, root)
        f["path"] = _cc._redact_secretish(rel)[:300]
        new = "" if kind == "delete" else _read_text(real)
        if new is None:
            snaps.pop(rel, None)
            continue
        old = snaps.get(rel)
        if old is None and kind == "add":
            old = ""
        elif old is None:
            old = _head_text(root, rel)
        _remember(snaps, rel, new)
        if old is None:
            continue
        lines = list(difflib.unified_diff(_cc._redact_secretish(old).splitlines(),
                                          _cc._redact_secretish(new).splitlines(), lineterm="", n=3))[2:]
        f["added"] = sum(1 for ln in lines if ln.startswith("+"))
        f["removed"] = sum(1 for ln in lines if ln.startswith("-"))
        text = "\n".join(lines)
        f["diff"] = text[:_DIFF_TEXT_MAX] + ("\n..." if len(text) > _DIFF_TEXT_MAX else "")
    return files


def _item_detail(item, cwd=None, snaps=None):
    """What a finished item said or did, for the live transcript: message and reasoning-summary text, the command
    line with its exit code and the tail of its output, edited files with their diffs, plan items, MCP server/tool
    names and web queries. Never MCP arguments or results. Each string is redacted whole (the same secret patterns
    as the cc adapter, which records full tool output), then capped."""
    red = lambda v, cap: _cc._redact_secretish(v)[:cap] if isinstance(v, str) and v else None   # redact, then cap
    t, out = item.get("type"), {}
    if t in ("agent_message", "reasoning"):
        out["text"] = red(item.get("text"), 4000)
    elif t == "command_execution":
        out["command"] = red(item.get("command"), 2000)
        if isinstance(item.get("exit_code"), int):
            out["exit_code"] = item["exit_code"]
        o = item.get("aggregated_output")
        if isinstance(o, str) and o.strip():
            o = _cc._redact_secretish(o.rstrip("\n"))
            out["output_lines"] = o.count("\n") + 1
            out["output"] = o if len(o) <= 3000 else "..." + o[-3000:]   # the tail is where errors and results are
    elif t == "file_change" and isinstance(item.get("changes"), list):
        out["paths"] = [p for p in (red(c.get("path"), 300) for c in item["changes"][:20] if isinstance(c, dict)) if p]
        out["files"] = _file_diffs(item["changes"], cwd, snaps if snaps is not None else {})
    elif t == "mcp_tool_call":
        out["tool"] = red(".".join(x for x in (item.get("server"), item.get("tool")) if isinstance(x, str)), 160)
    elif t == "todo_list" and isinstance(item.get("items"), list):
        out["todo"] = [{"text": red(i.get("text"), 300), "done": i.get("completed") is True}
                       for i in item["items"][:30] if isinstance(i, dict) and red(i.get("text"), 300)]
    elif t == "web_search":
        out["query"] = red(item.get("query"), 300)
    return {k: v for k, v in out.items() if v not in (None, [])}


def rollout_context_tokens(home, thread_id, tail_bytes=262144):
    """Context size after a codex turn: `last_token_usage` of the newest `token_count` event in the thread's
    rollout under the private CODEX_HOME (the last model request, input includes cached tokens). The `--json`
    `turn.completed` usage is a thread-cumulative sum over every request, not a context size. None when the
    rollout or the event is missing (token rotation is then skipped; the turn limit still applies)."""
    try:
        if not isinstance(thread_id, str) or not _SESSION_ID_RE.fullmatch(thread_id):
            return None
        hits = glob.glob(os.path.join(glob.escape(home), "sessions", "**", f"rollout-*{thread_id}.jsonl"), recursive=True)
        if not hits:
            return None
        try:
            with open(max(hits, key=os.path.getmtime), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - tail_bytes))
                lines = f.read().splitlines()
        except OSError:
            return None
        for raw in reversed(lines):
            if b"token_count" not in raw:
                continue
            try:
                pl = json.loads(raw).get("payload")
                last = pl["info"]["last_token_usage"] if pl.get("type") == "token_count" else None
                n = last["input_tokens"] + last.get("output_tokens", 0)
            except (ValueError, AttributeError, KeyError, TypeError):
                continue
            if isinstance(n, int) and n >= 0:
                return n
        return None
    except Exception as e:                               # context telemetry must never abort dispatch
        log.warning("codex context reader failed for thread %s: %s", thread_id, e)
        return None


class _LiveEvents:
    """Advisory dashboard progress; never replaces the final parse/PASS gate below."""
    def __init__(self, worker, resume_session, cwd=None):
        self.worker, self.cwd = worker, cwd
        if getattr(worker, "codex_snaps", None) is None:
            worker.codex_snaps = {}   # last-seen text per edited file, across turns, for per-edit diffs
        self.turns, self.tokens = worker.turns, worker.tokens
        self.prev = getattr(worker, "codex_usage", None)
        self.items = set()
        self.item_base = getattr(worker, "items", 0) if resume_session else 0
        worker.items = self.item_base

    def __call__(self, line):
        for _, ev in _events(line):
            w, typ = self.worker, ev.get("type")
            if not isinstance(typ, str):
                continue
            w.last_activity = time.time()
            view = {"type": typ} if typ in _CAPTURE_KEEP_TYPES else None
            if typ == "thread.started":
                sid = ev.get("thread_id")
                if isinstance(sid, str) and _SESSION_ID_RE.fullmatch(sid):
                    w.session_id = sid
                    view["thread_id"] = sid
                    if stream.enabled() and w.stream_session != sid:
                        w.stream_session = sid
                        stream.remember(w.key, sid, title=getattr(w, "title", None))
            elif typ == "turn.started":
                w.turns = self.turns + 1          # one broker turn, not one per item / retry
            elif typ in ("item.started", "item.updated", "item.completed"):
                item = ev.get("item")
                if isinstance(item, dict):
                    iid = item.get("id")
                    if isinstance(iid, str):
                        self.items.add(iid)
                        w.items = self.item_base + len(self.items)
                    view = {"type": typ, "item_type": str(item.get("type") or "unknown")[:80]}
                    if typ == "item.completed":
                        view.update(_item_detail(item, self.cwd, w.codex_snaps))
            elif typ == "turn.completed" and isinstance(ev.get("usage"), dict):
                usage = ev["usage"]
                if all(isinstance(usage.get(k, 0), int) and usage.get(k, 0) >= 0
                       for k in ("input_tokens", "output_tokens")):
                    cum = {k: usage.get(k, 0) for k in ("input_tokens", "output_tokens")}
                    prev = self.prev[1] if self.prev and self.prev[0] == w.session_id else {}
                    w.tokens = self.tokens + sum(max(0, v - prev.get(k, 0)) for k, v in cum.items())
                    view["usage"] = cum
            elif typ in ("error", "turn.failed"):
                error = ev.get("error") if typ == "turn.failed" else ev
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    view["message"] = error["message"][:4000]
            if view is not None and stream.enabled() and stream.valid_uuid(w.stream_session):
                # Reuse the transcript lookup/history/SSE path, in a broker-owned subdirectory. This survives
                # private CODEX_HOME cleanup; the existing auth + stream opt-in remain the only access path.
                try:
                    d = os.path.join(stream.PROJECTS_DIR, "eph-codex")
                    os.makedirs(d, mode=0o700, exist_ok=True)
                    fd = os.open(os.path.join(d, w.stream_session + ".jsonl"),
                                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    record = {"type": "system", "subtype": "codex", "timestamp": int(w.last_activity * 1000),
                              "content": json.dumps(_redact_leaves(view))}
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(json.dumps(record) + "\n")
                except OSError as e:
                    log.warning("codex live transcript write failed (%s)", type(e).__name__)


def _redact_leaves(v):
    """Redact every string inside a view. Redacting the serialized JSON instead can eat a \\" escape and leave
    a record the dashboard cannot parse (it then shows the raw text)."""
    if isinstance(v, str):
        return _cc._redact_secretish(v)
    if isinstance(v, dict):
        return {k: _redact_leaves(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_redact_leaves(x) for x in v]
    return v


def app_server_call(codex_bin, method, params, env, cwd, timeout=APP_SERVER_TIMEOUT_S):
    """One request to a one-shot `codex app-server` over stdio -> (result dict, None) or (None, error)."""
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "eph-broker", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}]
    # stdin stays OPEN until the answer arrives: 0.164 exits on EOF without answering a request still in flight
    try:
        p = subprocess.Popen([codex_bin, "app-server", "--listen", "stdio://"], cwd=cwd, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"app-server {method} failed ({type(e).__name__})"
    answer = []

    def read():
        for line in p.stdout:
            for _s, d in _events(line):
                if d.get("id") == 2 and "method" not in d:
                    answer.append(d)
                    return
    try:
        p.stdin.write("".join(json.dumps(m) + "\n" for m in msgs))
        p.stdin.flush()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout)
    except (OSError, ValueError) as e:
        return None, f"app-server {method} failed ({type(e).__name__})"
    finally:
        _close_app_server(p)
    if not answer:
        return None, f"app-server {method}: no answer (rc={p.returncode})"
    d = answer[0]
    if "error" in d:
        return None, f"app-server {method} error: {json.dumps(d['error'])[:300]}"
    res = d.get("result")
    return (res, None) if isinstance(res, dict) else (None, f"app-server {method}: no result")


def _close_app_server(p):
    for step in (lambda: p.stdin.close(), lambda: p.wait(2)):
        try:
            step()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if p.poll() is None:
        p.kill()
        try:
            p.wait(5)
        except subprocess.SubprocessError:
            pass
    try:
        p.stdout.close()
    except OSError:
        pass


def list_hooks(codex_bin, env, cwd):
    """-> (hooks Codex resolved for cwd, error or None). Codex's own view: trust, disable and feature flags apply."""
    res, err = app_server_call(codex_bin, "hooks/list", {"cwds": [cwd]}, env, cwd)
    if err:
        return None, err
    hooks, errs = [], []
    for entry in res.get("data") if isinstance(res.get("data"), list) else []:
        if isinstance(entry, dict):
            hooks += [h for h in entry.get("hooks") or [] if isinstance(h, dict)]
            errs += [str(e.get("message") if isinstance(e, dict) else e) for e in entry.get("errors") or []]
    return hooks, ("hooks/list reported errors: " + "; ".join(errs)[:300]) if errs else None


def run_hook(command, tool, tool_input, cwd, env):
    """Run the hook command the way Codex does (sh -c, event JSON on stdin, cwd = session cwd) -> (rc, stderr)."""
    ev = {"session_id": "eph-preflight", "transcript_path": None, "cwd": cwd, "hook_event_name": "PreToolUse",
          "model": "preflight", "turn_id": "preflight", "tool_name": tool, "tool_use_id": "preflight",
          "tool_input": tool_input, "permission_mode": "never"}
    try:
        r = subprocess.run(["/bin/sh", "-c", command], input=json.dumps(ev), capture_output=True, text=True,
                           cwd=cwd, env=env, timeout=HOOK_TIMEOUT_S + 5)
    except (OSError, subprocess.SubprocessError) as e:
        return None, type(e).__name__
    return r.returncode, r.stderr or ""


def _same(a, b):
    return bool(a) and bool(b) and os.path.realpath(str(a)) == os.path.realpath(str(b))


def codex_version(codex_bin):
    """Run `<codex_bin> --version` -> (parsed "X.Y.Z" string, None) or (None, a reason). Never raises - a CodexRefused
    decision from the result is the caller's, not this function's.

    M3 residual: a prerelease ("0.164.0-rc1", "0.164.0-beta.2", ...) is refused (None, reason)
    rather than silently accepted as its release prefix - the suffix means a build CODEX_VERIFIED was never
    checked against, whatever its release numbers say - unless CODEX_ALLOW_PRERELEASE=1 opts in deliberately."""
    try:
        r = subprocess.run([codex_bin, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"codex --version failed ({type(e).__name__})"
    text = (r.stdout or "") + (r.stderr or "")
    m = _VERSION_RE.search(text)
    if not m:
        seen = text.strip()[:200]
        return None, f"codex --version printed no parseable version ({seen!r})"
    if m.group(2) and not _cc._env_flag("CODEX_ALLOW_PRERELEASE"):
        return None, (f"codex --version reports a prerelease ({m.group(0)!r}), not a verifiable release build - "
                      f"refusing unless CODEX_ALLOW_PRERELEASE=1 (fail-closed)")
    return m.group(1), None


def _stat_sig(path):
    """A cheap (stat-only, no subprocess) signature of a file -> a tuple that changes whenever the file at
    `path` is replaced or rewritten (mtime, inode, device, size), or None if it cannot be stat'ed at all. Used
    by CodexHarness._check_version (M3 residual) to tell "same binary as last checked" from "something changed
    here, re-verify" without re-execing `--version` on every single spawn."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_ino, st.st_dev, st.st_size)


class CodexHarness:
    def __init__(self, codex_bin=None, home_root=None, sandbox_bin=None):
        found = shutil.which(codex_bin or CODEX_BIN)
        self.codex_bin = os.path.abspath(found) if found else None      # absolute once, never re-looked-up
        self.home_root = os.path.abspath(home_root or CODEX_HOME_ROOT)
        if os.path.islink(self.home_root):
            raise CodexRefused(f"codex adapter: home root {self.home_root} is a symlink - refusing")
        os.makedirs(self.home_root, mode=0o700, exist_ok=True)
        os.chmod(self.home_root, 0o700)
        self._bin_sig = None            # M3 residual: _check_version(force=True) below sets it on a pass
        if self.codex_bin:
            # M3: pin the binary to CODEX_VERIFIED_VERSION. A binary not found at all is left to one_turn's
            # existing CodexRefused (deferred to call time, like every other "can this turn even launch" check);
            # a binary that IS found but reports a different (or unparseable, or - M3 residual - prerelease)
            # version is refused right here, before any turn ever uses it - re-verifying CODEX_VERIFIED's claims
            # against a new version is a deliberate, reviewed act (bump CODEX_VERIFIED_VERSION), never a side
            # effect of an upgrade landing. one_turn calls this again (cheaply) before every spawn - see below.
            self._check_version(force=True)
            self._check_sandbox_helper(sandbox_bin)
        self._sweep()

    def _check_version(self, force=False):
        """M3 residual: the version pin used to be verified ONLY in __init__ and
        cached until the broker itself restarted, so an in-place `codex` binary upgrade under a running broker
        (same path, new content) was never caught again. Re-checked before every spawn (one_turn), but cheaply:
        a stat() signature (_stat_sig) of the binary tells "unchanged since last check" from "replaced/rewritten"
        with no subprocess at all; `--version` is only re-run when that signature actually changes (or on the
        first call, force=True from __init__). Raises CodexRefused (fail-closed) on a vanished binary or a
        mismatched/unparseable/prerelease version; updates self._bin_sig only on a passing check."""
        sig = _stat_sig(self.codex_bin)
        if sig is None:
            raise CodexRefused(f"codex adapter: {self.codex_bin} is no longer accessible - refusing (fail-closed)")
        if not force and sig == self._bin_sig:
            return
        version, err = codex_version(self.codex_bin)
        if err or version != CODEX_VERIFIED_VERSION:
            raise CodexRefused(
                f"codex adapter: {self.codex_bin} reports version {version or err!r}, not the verified "
                f"{CODEX_VERIFIED_VERSION!r} (see CODEX_VERIFIED) - refusing (fail-closed); re-check this "
                f"adapter's config keys, feature flags and gate assumptions against the new version, then "
                f"bump CODEX_VERIFIED_VERSION and CODEX_VERIFIED deliberately")
        self._bin_sig = sig

    def _check_sandbox_helper(self, sandbox_bin):
        """L1 (preflight): every turn execs this helper to actually sandbox a command, in either sandbox mode.
        Refuse up front with a clear reason rather than let every turn discover it missing mid-exec as a
        confusing "No such file or directory" (tests/test_codex_probe.py's SANDBOX_HELPER_ERR). Codex finds its
        own copy of this helper next to its own executable (arg0 dispatch); the adapter does not configure or
        pass it - this is a pure existence check, nothing is handed to the turn's env over it."""
        helper = sandbox_bin or CODEX_LINUX_SANDBOX_BIN
        if not shutil.which(helper):
            raise CodexRefused(
                f"codex adapter: the {helper!r} helper is not on PATH - every codex turn execs it to "
                f"sandbox a command - refusing (fail-closed); install it alongside the codex binary")

    def _sweep(self):
        """Remove leftover homes (broker crash / kill skipped the finally) older than HOME_MAX_AGE_S. Assumes one
        broker per CODEX_HOME_ROOT. rmtree never follows the auth.json symlink."""
        cutoff = time.time() - HOME_MAX_AGE_S
        for name in os.listdir(self.home_root):
            path = os.path.join(self.home_root, name)
            try:
                st = os.lstat(path)
                if st.st_mtime >= cutoff:
                    continue
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
            except OSError as e:
                log.warning("codex home sweep: %s: %s", path, e)

    def _home(self, worker):
        home = getattr(worker, "codex_home", None)
        if home and os.path.isdir(home):
            os.utime(home)
            return home
        home = tempfile.mkdtemp(prefix=f"{worker.run_id}-", dir=self.home_root)   # 0700
        worker.codex_home = home
        weakref.finalize(worker, shutil.rmtree, home, True)
        return home

    @staticmethod
    def _sweep_home(home):
        """Reduce the home to _HOME_KEEP / *.sqlite* (never a symlink) before a turn, or CodexRefused."""
        for name in os.listdir(home):
            path = os.path.join(home, name)
            link = os.path.islink(path)
            if not link and (name in _HOME_KEEP or _HOME_KEEP_RE.fullmatch(name)):
                continue
            try:
                if os.path.isdir(path) and not link:
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
            except OSError as e:
                raise CodexRefused(f"codex adapter: cannot sweep {name!r} from the private home ({e}) - refusing")

    @staticmethod
    def _write_new(path, text):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as e:                     # re-planted by a same-uid racer after the sweep
            raise CodexRefused(f"codex adapter: cannot create {os.path.basename(path)} in the private home "
                               f"({type(e).__name__}) - refusing (fail-closed)")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)

    @staticmethod
    def _link_auth(home):
        """<home>/auth.json -> CODEX_AUTH_FILE (read at call time). Only stat'ed, never opened."""
        src = os.getenv("CODEX_AUTH_FILE", "").strip()
        if not src or not os.path.isabs(src) or not os.path.isfile(src):
            raise CodexRefused("codex adapter: CODEX_AUTH_FILE must name the operator's existing Codex login file "
                               "(absolute path) - refusing (fail-closed)")
        os.symlink(src, os.path.join(home, AUTH_FILE))

    def _install_gate(self, home, policy, env, cwd, steer_command=None):
        """Write this turn's policy + hooks.json, trust exactly that hook, and PROVE Codex will run it and that it
        answers (see the module doc). Returns the nonce, or raises CodexRefused (no launch). With steer_command,
        hooks.json also carries the steer.py PostToolUse hook; it is trusted alongside the gate and must resolve
        exactly as written (it is the ONLY other hook allowed), but it gates nothing, so it is not probed."""
        policy_path = os.path.join(home, POLICY_FILE)
        for name in (POLICY_FILE, DENIALS_FILE):
            try:
                os.unlink(os.path.join(home, name))
            except FileNotFoundError:
                pass
        self._write_new(policy_path, json.dumps(policy))
        command = hook_command(policy_path)
        hooks_path = os.path.join(home, HOOKS_FILE)
        self._write_new(hooks_path, json.dumps(hooks_config(command, steer_command), indent=1))

        def refuse(why):
            raise CodexRefused(f"codex adapter: eph-gate hook not verifiably live ({why}) - refusing the turn "
                               f"(fail-closed)")
        hooks, err = list_hooks(self.codex_bin, env, cwd)
        if err:
            refuse(err)
        mine = [h for h in hooks if _same(h.get("sourcePath"), hooks_path)]
        if not mine or not all(isinstance(h.get("key"), str) and isinstance(h.get("currentHash"), str) for h in mine):
            refuse(f"codex loaded no hook from {HOOKS_FILE}")
        with open(os.path.join(home, CONFIG_FILE), "a", encoding="utf-8") as f:
            for h in mine:
                f.write(f"\n[hooks.state.{toml_str(h['key'])}]\ntrusted_hash = {toml_str(h['currentHash'])}\n")
        hooks, err = list_hooks(self.codex_bin, env, cwd)
        if err:
            refuse(err)
        mine = [h for h in hooks if _same(h.get("sourcePath"), hooks_path)]
        want = 2 if steer_command else 1
        if len(mine) != want:
            refuse(f"{len(mine)} hooks resolved from {HOOKS_FILE}, expected {want}")
        pre = [x for x in mine if x.get("eventName") == "preToolUse"]
        h = pre[0] if len(pre) == 1 else mine[0]
        if not (h.get("eventName") == "preToolUse" and h.get("enabled") is True and h.get("trustStatus") == "trusted"
                and h.get("matcher") in (None, "", "*") and h.get("command") == command):
            refuse("hook state " + json.dumps({k: h.get(k) for k in ("eventName", "enabled", "trustStatus",
                                                                     "matcher")}))
        ok = {id(h)}
        if steer_command:
            post = [x for x in mine if x is not h]
            sh = post[0] if post else {}
            if not (sh.get("eventName") == "postToolUse" and sh.get("enabled") is True
                    and sh.get("trustStatus") == "trusted" and sh.get("matcher") in (None, "", "*")
                    and sh.get("command") == steer_command):
                refuse("steer hook state " + json.dumps({k: sh.get(k) for k in ("eventName", "enabled",
                                                                              "trustStatus", "matcher")}))
            ok.add(id(sh))
        stray = [x for x in hooks if id(x) not in ok and x.get("enabled") and x.get("trustStatus") == "trusted"]
        if stray:
            refuse(f"{len(stray)} other trusted hook(s) resolved (e.g. {str(stray[0].get('sourcePath'))[:120]})")
        nonce = policy["nonce"]
        rc, err = run_hook(command, _gate.PROBE_TOOL, {}, cwd, env)
        if rc != 2 or f"eph-gate probe ok {nonce}" not in err:
            refuse(f"probe rc={rc} {err.strip()[:200]!r}")
        rc, err = run_hook(command, "Bash", {"command": "git push https://example.invalid HEAD"}, cwd, env)
        if rc != 2 or "eph-gate" not in err:
            refuse(f"floor probe rc={rc} {err.strip()[:200]!r}")
        try:
            os.unlink(os.path.join(home, DENIALS_FILE))        # the floor probe's deny is not a worker denial
        except FileNotFoundError:
            pass
        return nonce

    def one_turn(self, adapter, worker, route, system_prompt, prompt_text, resume_session,
                 backend_override=_NO_OVERRIDE, wall_clock_override=None):
        """One codex turn. Returns the parsed envelope or None (parse miss); the CC _one_turn contract."""
        if getattr(worker, "done", False):
            log.info("codex turn key=%s: worker torn down -> not launching", worker.key)
            return None
        if not system_prompt:
            raise ValueError("refusing to spawn without a system prompt (constraint #1)")
        if not self.codex_bin:
            raise CodexRefused(f"codex adapter: binary {CODEX_BIN!r} not found - refusing")
        self._check_version()          # M3 residual: cheap unless the binary file itself changed since __init__
        capability = getattr(route, "capability", "read")
        sandbox = codex_sandbox(capability)                                   # refuse BEFORE any launch
        backend = getattr(route, "backend", "") if backend_override is _NO_OVERRIDE else backend_override
        model, effort, model_src = codex_model(backend)
        wall_clock_s = adapter.wall_clock_by_capability.get(capability, adapter.wall_clock_s)
        if wall_clock_override is not None:
            wall_clock_s = min(wall_clock_s, wall_clock_override)
        if not (worker.worktree and os.path.isdir(worker.worktree)):
            raise CodexRefused(f"codex adapter: {capability} turn has no worktree cwd - refusing (fail-closed)")
        log.info("codex spawn key=%s capability=%s sandbox=%s model=%s (from %s) wall-clock=%ss", worker.key,
                 capability, sandbox, model, model_src, wall_clock_s)
        cwd = os.path.realpath(worker.worktree)
        home = self._home(worker)
        self._sweep_home(home)
        self._link_auth(home)
        test_runner = getattr(worker, "test_runner", None) or getattr(route, "test_runner", "") or None
        test_repo = getattr(worker, "repo", None) if test_runner == "venv" else None
        try:
            policy = gate_policy(capability, cwd, test_runner, test_repo)
        except ValueError as e:
            raise CodexRefused(f"codex adapter: {e}")
        instructions = os.path.join(home, INSTRUCTIONS_FILE)
        # workdir-bypass fix: every capability's signed prompt gets the exact `cd <worktree> || exit 1; ` prefix
        # codex_gate.py requires on every Bash / exec_command call (hermes.worker_system_prompt's PATCH_EDIT_RULE
        # pattern) - mr-review included (M1, eph-broker !185 review): it still reads through the shell, so it
        # needs the prefix too, even though write_file/patch/terminal are hard-denied for it.
        self._write_new(instructions, codex_gate.worker_system_prompt(system_prompt, capability, cwd))
        env = _cc.worker_env(os.environ, route, worker=worker)
        env.update(_cc.repo_companions.environment(worker))
        _cc.test_environment.apply_tmpdir(env, worker, capability)
        for k in [k for k in env if k.startswith(_POPPED_ENV_PREFIXES)]:
            del env[k]
        env["CODEX_HOME"] = home
        src = os.path.join(cwd, "src")
        if test_runner == "venv" and os.path.isdir(src):    # cc parity (src-layout venv repos); the hook runs -I
            env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self._write_new(os.path.join(home, CONFIG_FILE),
                        codex_config(model, effort, sandbox, instructions,
                                     network=_cc._env_flag("CODEX_SANDBOX_NETWORK"),
                                     writable_roots=git_writable_roots(cwd) if sandbox == "workspace-write" else (),
                                     shell_path=env["PATH"], companion_env=_cc.repo_companions.environment(worker),
                                     git_identity=_cc.routing.worker_git_identity(route)))
        self._install_gate(home, policy, env, cwd,         # CodexRefused unless the hook is proven live
                           steer.hook_command(worker.key) if steer.enabled() else None)
        argv = build_argv(self.codex_bin, model, sandbox, resume_session)
        worker.turn_wall_clock = wall_clock_s
        worker.kill_group = True
        _cc.record_launch(worker, model, backend, "codex")   # dash Worker view: the model this process gets
        worker.in_turn = True
        worker.turn_started = time.time()
        worker.hard_killed = False
        live = _LiveEvents(worker, resume_session, cwd)
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
            worker.proc = proc
            if getattr(worker, "done", False):          # teardown landed between the check above and Popen
                _kill_group(proc)
            out, err, killed = _stream_process(proc, prompt_text, wall_clock_s, live)
            if killed:
                worker.hard_killed = True
                log.warning("HARD-KILL key=%s wall-clock %ss exceeded (codex)", worker.key, wall_clock_s)
                adapter._alert_ops(f"worker {worker.key} hard-killed: wall-clock {wall_clock_s}s (codex)",
                                   subject=f"worker:{worker.key}")
        finally:
            worker.in_turn = False
            worker.turn_started = None
        worker.turns = live.turns + 1
        wrapper = parse_events(out)
        _capture = lambda reason: _cc.capture_failed_turn(worker, route, reason, proc.returncode,
                                                          capture_stdout(out), err)
        if wrapper is not None:
            # usage is the THREAD's running total: report this turn's share (session rotation counts it)
            prev = getattr(worker, "codex_usage", None)
            cum = wrapper.pop("cumulative")
            if prev and prev[0] == wrapper.get("session_id"):
                wrapper["usage"] = {k: max(0, cum[k] - prev[1].get(k, 0)) for k in cum}
            if wrapper.get("completed"):
                worker.codex_usage = (wrapper.get("session_id"), cum)
        # no wrapper: keep only the postmortem-safe event view (escalations quote last_output; never tool output)
        worker.last_output = json.dumps(wrapper) if wrapper else (capture_stdout(out).strip() or (err or ""))[-4000:]
        if wrapper is None:
            log.warning("codex turn key=%s rc=%s: no thread/turn events -> parse miss", worker.key, proc.returncode)
            _capture("no codex events")
            return None
        if wrapper.get("session_id") and _SESSION_ID_RE.fullmatch(str(wrapper["session_id"])):
            worker.session_id = wrapper["session_id"]     # kept even on failure: the warm re-prompt resumes it
        if killed or not wrapper.get("completed") or wrapper.get("error") or proc.returncode != 0:
            log.warning("codex turn key=%s failed (killed=%s completed=%s rc=%s error=%r) -> parse miss", worker.key,
                        killed, wrapper.get("completed"), proc.returncode, (wrapper.get("error") or "")[:200])
            _capture("failed run (timeout / non-zero exit / error / no turn.completed)")
            return None
        parse = getattr(adapter.envelope_parser, "try_parse", adapter.envelope_parser)
        result = parse(worker.last_output, worker.run_id, route)
        if result is None:
            _capture("parse miss")
            return result
        worker.tokens = live.tokens + int(result.get("tokens", 0) or 0)
        worker.context_tokens = rollout_context_tokens(home, worker.session_id)
        denials = read_denials(home)
        if denials:
            result["gate_denials"] = denials
        return result
