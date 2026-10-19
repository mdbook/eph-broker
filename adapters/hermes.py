#!/usr/bin/env python3
"""HermesHarness: run ONE eph worker turn on the hermes-agent CLI instead of Claude Code.

Selected per ROW by `Route.adapter == "hermes"` (routing.py; default "cc" everywhere). Everything ABOVE the turn
is the CCAdapter's, unchanged: the Adapter contract (spawn / forward / liveness / teardown, turn_age /
worker_alive / set_coalescer), the worktree cut, the ticket brief, the concurrency cap, the warm
re-prompt, session rotation, envelope posting and every escalation sink. CCAdapter._one_turn hands the harness
step to `HermesHarness.one_turn`, which fills the SAME Worker turn fields (proc / in_turn / turn_started /
turn_wall_clock / turns / last_output / session_id / tokens) so liveness, D5 force-close and teardown work as-is.

LAUNCH MODEL: a per-worker, one-shot CLI process, NOT an api_server:
    hermes chat --query-file - --format stream-json -Q --ignore-rules --source tool
                --provider eph -m <model> -t <toolsets> --max-turns N --run-budget S [--resume <session>]
  * isolated: a PRIVATE HERMES_HOME per worker (config + sessions DB), never ~/.hermes or a shared server. The
    home lives across the worker's turns (so a forward or the warm re-prompt can `--resume`); _run_worker's finally
    removes it next to the worktree (weakref.finalize on the Worker is the backstop), and homes older than
    HOME_MAX_AGE_S are swept when the harness is built.
  * `chat -q`, not `-z`: the `-z` path ignores --ignore-rules, agent.system_prompt, agent.disabled_toolsets and
    --max-turns, and auto-approves (YOLO). The chat path honours all four, and non-TTY single-query approvals
    default to DENY.
  * charter-strip: --ignore-rules skips AGENTS.md / CLAUDE.md / .cursorrules / SOUL.md / memory. The
    signed broker prompt rides `agent.system_prompt` in the private config, and is authoritative ONLY over its
    own section: even with --ignore-rules hermes PREPENDS its built-in identity + tool guidance and APPENDS a
    timestamp + RUNTIME_ENVIRONMENT block (no knob replaces the whole prompt while keeping rules off). Unlike
    CC's --system-prompt REPLACE, the model therefore sees hermes-authored text around the signed prompt.
  * prompt on STDIN (--query-file -): no argv size limit, nothing shell-interpreted.
  * caps: --max-turns = the same per-capability value CC gets; the wall clock is a HARD kill of the whole
    process group (start_new_session + killpg) at the same per-capability wall_clock_s (--run-budget only
    asks the agent to wrap up early; it does not stop it).
  * result: stream-json's final {"type":"result"} record is repacked into the CC `--output-format json`
    wrapper shape ({result, session_id, usage}) so the UNCHANGED envelope.EnvelopeParser parses it.

CAPABILITY PARITY (fail closed). hermes grants tools by TOOLSET only: `file` = read_file + write_file + patch
+ search_files (no read-only file set exists), `terminal` = arbitrary shell, and its own read-deny covers `.env*`
only. Per-call policy is therefore enforced by the broker's own hermes PLUGIN, eph-gate (adapters/hermes_gate/):
a `pre_tool_call` hook that applies the SAME allow/deny rules a CC worker of that capability gets, serialised
per turn from cc.capability_tool_grant() + cc._SECRET_FILE_GLOBS (never copied), so a tightening in cc.py reaches
hermes unchanged, plus hard denies of its own (worktree confinement, git file-reading options, secret floor,
.git/ writes). Mapping + command-parsing ceiling: see gate.py.
    mr-review  -> `-t file` under eph-gate, READ-ONLY (live 2026-09-28: a tool-less `-t todo` reviewer could not
                  read the checkout and blocked every MR). Policy = cc's mr-review grant (Read/Grep/Glob; Edit/
                  Write/Bash denied; no acceptEdits) + the secret floor, read_root = the MR source-branch checkout.
                  read_file / search_files only; write_file / patch / terminal are HARD-denied by the gate for this
                  capability whatever the imported rules say (gate.READ_ONLY_CAPABILITIES, fail-closed). The
                  terminal toolset is never granted. Same sweep / probe / sentinel / env pops as every gated turn.
    write      -> `-t file,terminal` under eph-gate: reads/searches confined to the repo, edits confined to the
                  worker cwd (CC's acceptEdits cwd), terminal = the CC write allow-list (git add/commit/status/
                  diff/log) minus the deny-floor.
    code       -> `-t file,terminal` under eph-gate: write + the reviewed test-runner / mcp-ro.sh allow-list.
                  A hermes turn is never Claude, so code ALSO needs CODE_NONCLAUDE_OK=1 in the broker env (read
                  at call time; default refuse), the same gate the CC path applies to a non-Claude code turn.
                  venv runner: <worktree>/src reaches PYTHONPATH of the terminal shell only (an init file in
                  terminal.shell_init_files), never the hermes process (see TERMINAL_ENV_FILE).
    general    -> `-t file,terminal` under eph-gate + the policy judge. CC's general has no
                  allow-list (auto classifier + deny floor), and a prefix deny-floor alone is not enforceable, so
                  eph-gate runs its floors FIRST (secret floor, worktree-confined edits, no .git writes, imported
                  _GENERAL_DENIED on any segment the lexer can split) and then every terminal / write_file / patch
                  needs an allow from gate-wrapper.sh, which calls gate-hook.py (armed: no decision = BLOCK). No web toolset
                  (backend 403s); unmapped tools deny as `unmapped-tool: <name>`. A refused call is recorded and
                  reported in the result (`gate_denials`, with the hook's allow-once sha): deny-and-report, never
                  ask. Reachable only via the per-ticket `hermes` label, which ships with an EMPTY allow-set.
    read / inbox / inbox-chat / anything else -> REFUSED (inbox needs the Gmail MCP; read was refused as
                  tool-less and is not re-opened here).
INTERPRETER PATH (fail closed): every hermes process runs with cwd = the worktree (for
mr-review: the MR source-branch checkout), so ANY relative sys.path entry of the hermes interpreter resolves INTO
worker-controlled files. An EDITABLE install (`uv sync` / `pip install -e`) adds exactly such an entry
(`__editable__.<dist>.finder.__path_hook__`, placed before its own finder): a worktree dir of that name would be
imported as hermes itself. The broker's hermes MUST therefore be a non-editable install (the build report has the
reinstall command), and before EVERY turn the hermes interpreter (same env, same cwd) is checked: a relative
sys.path entry (an editable placeholder), an entry at/under the cwd (python absolutises an empty/relative
PYTHONPATH component to <cwd>[/rel]) or any `__editable__` entry/finder refuses the turn. Gated turns fold the
check into the probe (its first statement, before any import); every honoured capability, mr-review included,
is gated, so the probe carries it on every turn; the standalone check (PATH_CHECK) remains the fallback for a
turn without a probe. scrub_worker_env also drops empty/relative PYTHONPATH components.
GATE LIVENESS (fail closed): before every gated turn the adapter (1) sweeps the private home down to an allowlist
(session store + logs; everything else, such as .env, .op.env, active_profile, profiles/, .container-mode and bin/,
is removed), (2) copies the plugin into it and enables it by name, (3) writes the per-turn policy with a fresh
nonce, (4) runs a PRE-FLIGHT PROBE: the broker's own hermes interpreter under `-P` (the implicit cwd entry is
NOT on sys.path, as for the console script whose sys.path[0] is venv/bin; any other cwd-resolved entry, e.g. an
editable-install placeholder, refuses; see INTERPRETER PATH), same private home, env, cwd and argv as the run:
it imports hermes_cli.main (the real startup prelude: profile override, dotenv load, scratch env) and then calls
hermes' real pre_tool_call dispatch, and requires eph-gate's nonce answer AND a block on a floored command, a
clean prelude, no HERMES_* beyond HERMES_HOME + hermes' own HERMES_SCRATCH_DIR, the same home after the profile
override (env and hermes' resolved home), no container re-exec, no install-dir .env, an empty shell-linter table and LSP off; anything else
refuses the turn. The real run then loads the plugin DETERMINISTICALLY: plugins.load_timeout_seconds 0 (inline,
no deadline to lose it to under load), every HERMES_* but HERMES_HOME popped from the env. A discovery exception
in the real run is not cached as "discovered" (plugins.py discover_and_load resets the flag), so the pre_tool_call
dispatch re-runs discovery the same way the probe did. Residual: a transient OS failure (fd/memory exhaustion)
during that one load, and same-uid code from an EARLIER turn (a code worker's pytest) rewriting the hermes
install, or re-planting home files between the sweep and the run (accepted: the venv and home are same-uid).
The KEPT sessions/ and logs/ are not inspected: a same-uid-planted symlink inside them (e.g. logs/agent.log pointing
elsewhere) makes hermes append/clobber outside the home as the broker uid (the same same-uid residual).
TIRITH: security.tirith_enabled false (config) and TIRITH_* popped from the env, since hermes would otherwise exec
$HERMES_HOME/bin/tirith (or auto-download it unpinned) on terminal commands; eph-gate is the enforcement point.
After the run the plugin's load sentinel must carry the nonce, or the turn is a parse miss and an alert.
DETECTION, not prevention (checked after the tools ran; an ungated terminal could forge it from the same-uid
policy file).
POST-WRITE TOOLCHAINS: hermes lints after every write_file/patch outside pre_tool_call. LSP is
off in config (lsp.enabled false). The SHELL linters (.js node --check, .ts npx tsc, .go go vet, .rs rustfmt)
have no config key: eph-gate empties hermes' LINTERS table at register, and the gate denies a write to any
extension the live table would still shell-lint. In-process linters (.py/.json/.yaml/.toml) start no program.
TOOL SEARCH: `tools.tool_search.enabled: off` on every hermes turn (mr-review included); eph-gate also denies the
tool_call / tool_search / tool_describe bridge names outright.
MCP: none can load. The private config has no mcp_servers, and `-t` also filters which MCP servers start.
PLUGINS / ENV: `plugins.enabled` names only eph-gate (every honoured capability is gated), but hermes' BUNDLED
backend/platform plugins still import (their toolsets are disabled above; none registers a pre_tool_call hook
unless enabled). hermes also loads <install dir>/.env into its own process env, so the
dedicated broker install (HERMES_BIN) must keep NO .env there, since anything in it would reach every worker.
(The probe checks that it is absent on every gated turn.)

TURN CAP (turn_capped): NOT set for hermes. stream-json's result record carries no turn_exit_reason /
api_call count, and counting tool_use events is not the iteration count (one iteration can emit several tool
calls; the budget path adds a summary call), so any count would be a guess. A capped run ends completed=False
and exit_code 1, which the exit gating below turns into a parse miss, a warm re-prompt, then an escalation.

MODEL / BACKEND: hermes has no Claude Code OAuth path (and `auth.adopt_external_logins` is forced OFF so it
never borrows one), so a native-Anthropic route ("" / "claude") is REFUSED. A route backend resolves through
the SAME cc.load_backend / spawn-models.json path CC uses: its base_url + "/v1" becomes a chat_completions
custom provider, its registry model is `-m`, and its auth token reaches hermes ONLY as the process env var
AUXILIARY_VLLM_API_KEY (the name hermes scrubs from tool subprocess env; the config file names the VARIABLE,
never the value). An unresolvable backend is refused.

ORPHANS: start_new_session puts hermes in its own process group, so a SIGKILL of the BROKER alone leaves a
running turn orphaned (no wall-clock owner) until it finishes; a systemd stop of the unit still kills it (same
cgroup, KillMode=control-group). Normal teardown / hard-kill paths kill the whole group.

Verified against hermes-agent v0.21.5 (2026.9.24), upstream dd3ba4b4; see HERMES_VERIFIED_VERSION.
"""
import json, logging, os, re, secrets, shlex, shutil, signal, subprocess, tempfile, time, weakref

from . import cc as _cc
from .cc import _NO_OVERRIDE, load_backend, scrub_worker_env, SPAWN_MODELS_JSON
from .cc import _is_local_backend, LOCAL_WALL_CLOCK_MULT   # local-backend wall-clock scaling, shared with cc
from .hermes_gate import DENIALS_FILE, POLICY_FILE, SENTINEL_FILE, gate as _gate

log = logging.getLogger("eph-broker.hermes")

# the CLI surface this harness was built and reviewed against (flags, stream-json schema, config keys). A bump is
# deliberate: re-verify chat -q flags, the result record, agent.system_prompt / disabled_toolsets, approvals.
HERMES_VERIFIED_VERSION = "v0.21.5 (2026.9.24) upstream dd3ba4b4"
# the broker's DEDICATED hermes install (never a shared one, never ~/.hermes); resolved to an absolute path at init
HERMES_BIN = os.getenv("HERMES_BIN", os.path.expanduser("~/.local/share/eph-hermes/venv/bin/hermes"))
# per-worker private HERMES_HOME dirs live here (box-local scratch on the broker host, like the MR/wiki worktrees)
HERMES_HOME_ROOT = os.getenv("HERMES_HOME_ROOT", os.path.expanduser("~/.cache/eph-hermes-homes"))
# leftover homes (crash / broker kill) older than this are swept at init. ASSUMES ONE broker per HERMES_HOME_ROOT:
# a second broker sharing the root could sweep a long-idle live home of the first.
HOME_MAX_AGE_S = 24 * 3600
_SESSION_ID_RE = re.compile(r"[\w.-]+", re.ASCII)       # used with fullmatch()
# the ONLY env var carrying the backend token into hermes (named in config as key_env; hermes scrubs this name
# from terminal-tool child env; other hermes configs use the same name for the same reason).
KEY_ENV = "AUXILIARY_VLLM_API_KEY"
PROVIDER = "eph"

# capability -> hermes toolsets. ABSENT == refused. Capabilities in GATED_CAPABILITIES run ONLY under the
# eph-gate plugin. Every honoured capability is gated now (mr-review too: `file` = read_file + write_file + patch
# + search_files, so the READ-only posture is the gate's; the writers are hard-denied for mr-review). A
# tool-less row (never an empty -t, which enables ALL toolsets) would name `todo`.
HERMES_GRANTS = {"mr-review": ("file",), "write": ("file", "terminal"), "code": ("file", "terminal"),
                 "general": ("file", "terminal")}
GATED_CAPABILITIES = frozenset({"mr-review", "write", "code", "general"})
# general = file + terminal under eph-gate + the policy judge (gate.judge). NO web: the
# broker hermes' web backend is keyless firecrawl and 403s (checked 2026-10-16), so web_* stay unmapped-tool
# denies and a worker fetches with curl under the judge's egress rules. The judge is the SAME entry every standing
# agent's PreToolUse hook uses (gate-wrapper.sh, deployed into the judge's bin dir), armed
# (mode=enforce + full=on in the switch dir, set at deploy time) so a hook timeout / garbage / no decision is a BLOCK.
# Ceiling: agent name and gate dir are fixed here, so there's ONE shared switch dir and ONE concurrent general-on-hermes
# worker (the hook's ask-dm-sent / clearance-pending / m4b-pin state and decisions.jsonl are per-dir). Scaling past
# the canary needs a gate dir per worker, read from config. Not built. Also known (non-blocking): the worker
# can READ its gate dir's decisions.jsonl (it can't mint: allow-once writes are hard-denied by tier0 + the
# GENERAL_TERMINAL_FLOOR belt now refuses any command naming the dir; a computed path still reads).
GENERAL_JUDGE = {
    "wrapper": os.path.expanduser("~/.eph-judge/bin/gate-wrapper.sh"),
    "hook": os.path.expanduser("~/.eph-judge/bin/gate-hook.py"),
    "python": os.path.expanduser("~/.eph-judge/venv/bin/python"),
    "gate_dir": os.path.expanduser("~/.eph-judge/eph-general"),
    # The judge's CLAUDE_PROJECT_DIR. NEUTRAL and worker-unwritable (the judge's tier0 rules hard-deny writes
    # to its own dir; eph-gate's write_root is the worktree). NEVER the worktree: gate-hook reads <proj>/AUTHORIZED.md and
    # .claude/settings*.json as the agent's own policy. judge() refuses any project_dir overlapping the write root.
    "project_dir": os.path.expanduser("~/.eph-judge/eph-general"),
    "timeout": "5.0",
}
GATE_PLUGIN = "eph-gate"
_GATE_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermes_gate")
PROBE_TIMEOUT_S = 90
# Every HERMES_* name is dropped from the worker env except HERMES_HOME, which the adapter
# sets itself (the ONLY HERMES_* it sets). scrub_worker_env is an allowlist, but route.env / backend_env layer on
# top; switches seen in hermes' source include HERMES_SAFE_MODE (no plugin discovery), HERMES_ENABLE_PROJECT_PLUGINS
# (a same-named repo plugin at <cwd>/.hermes/plugins/ shadows ours), HERMES_MANAGED_DIR (managed config/.env
# overlay), HERMES_BUNDLED_PLUGINS (bundled backend plugins auto-load from another dir), HERMES_DEV (skips the
# container-mode check), HERMES_YOLO_MODE, HERMES_DISABLE_FAST_CHAT_LAUNCH. Popping the prefix closes the class.
_HERMES_ENV_PREFIX = "HERMES_"
# TIRITH_ENABLED / TIRITH_BIN / ... override security.tirith_* in config (tools/tirith_security.py): popped too.
_POPPED_ENV_PREFIXES = (_HERMES_ENV_PREFIX, "TIRITH_")
_HERMES_ENV_SET = ("HERMES_HOME",)
# the HERMES_* names hermes' OWN startup import may leave set (hermes_constants.export_scratch_tmp_env sets
# HERMES_SCRATCH_DIR, inside the home). The probe refuses any other HERMES_* after the startup replay.
_HERMES_STARTUP_ENV = frozenset(_HERMES_ENV_SET + ("HERMES_SCRATCH_DIR",))
# The private home is swept to an ALLOWLIST before every turn: hermes reads several home
# files at startup that re-point or re-arm it: .env / .op.env (dotenv, override=True), active_profile (+
# profiles/<x>/: main._apply_profile_override re-homes to another config, because a custom HERMES_HOME's parent is
# not named `profiles`) and .container-mode (main() execvp's into a container). Other state hermes may read back
# (auth.json, skills/, memories/, hooks/, cron/, cache/) is not needed across turns either. KEPT is only state
# that hermes writes and --resume / debugging needs:
#   state.db*  the session store `--resume` reads (sqlite + -wal/-shm + lock files),
#   sessions/  per-session transcripts, logs/ agent + error logs.
# bin/ is NOT kept: hermes execs $HERMES_HOME/bin/tirith on terminal commands, so a
# same-uid-planted bin/tirith would persist across turns; tirith is off in config anyway.
# Everything the adapter owns (config.yaml, plugins/eph-gate, the policy, the terminal env file) is removed too
# and rewritten fresh after the sweep; the sentinel is deleted before the probe. A symlink is never kept.
_HOME_KEEP = frozenset({"sessions", "logs"})
_HOME_KEEP_PREFIX = "state.db"
# venv test runner: the worktree src/ goes on PYTHONPATH for the TERMINAL tool's shell ONLY
# (never on the hermes process: a worker-written src/json.py would run inside hermes before the gate). The
# local terminal backend sources terminal.shell_init_files into its login-shell env snapshot
# (tools/environments/local.py _resolve_shell_init_files / _run_bash(login=True)), which every later command
# inherits; this file is that one init file.
TERMINAL_ENV_FILE = "eph-terminal-env.sh"
# belt: subtracted LAST by hermes even if a composite re-adds them (model_tools._select_tool_names).
_DISABLED_TOOLSETS = ["terminal", "file", "code_execution", "delegation", "browser", "computer_use", "web",
                      "search", "vision", "image_gen", "video_gen", "tts", "memory", "session_search", "skills",
                      "cronjob", "kanban", "homeassistant", "clarify", "connections"]


class HermesRefused(ValueError):
    """A route this harness cannot run without widening its grants (or its model/backend). Raised before any
    process starts; CCAdapter._run_worker's except turns it into the alert (fail-closed)."""


def hermes_toolsets(capability):
    """Map an abstract Route.capability to a hermes -t list, or raise HermesRefused. mr-review (read-only), write
    and code (all under eph-gate) are honoured; everything else, unknown values included, is REFUSED (stricter than CC's
    fall-back-to-read). code additionally needs CODE_NONCLAUDE_OK=1 at call time (a hermes turn is never Claude;
    parsed by cc._env_flag, like the CC path's non-Claude code gate)."""
    grant = HERMES_GRANTS.get(capability)
    if grant is None:
        raise HermesRefused(
            f"hermes adapter refuses capability {capability!r}: honoured are {', '.join(sorted(HERMES_GRANTS))} "
            f"(inbox needs Gmail MCP)")
    if capability == "code" and not _cc._env_flag("CODE_NONCLAUDE_OK"):   # same flag parse as the CC gate
        raise HermesRefused("hermes adapter refuses capability 'code': a hermes turn is non-Claude and "
                            "CODE_NONCLAUDE_OK=1 is not set (fail-closed)")
    return list(grant)


def read_denials(home, cap=50):
    """Deny-and-report: the eph-gate denial rows of THIS turn (DENIALS_FILE in the private home, swept
    before every turn) as a list of dicts, or []. Each row: kind (floor | unmapped-tool | judge), tool, reason, and
    (for judge denies the hook would honour an allow-once for) `sha` (byte-exact from gate-hook.py: sha256 of the exact
    command / subject bytes including heredoc body and newlines = the allow-once token filename; NEVER recomputed) plus
    `subject` (the exact terminal command, <= 4000 chars) so a reviewer can check sha256(subject) == sha before minting.
    Worker-influenced text: rendered escaped, never as instructions (see cc._gate_denials_html)."""
    out = []
    try:
        with open(os.path.join(home, DENIALS_FILE), encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append({k: row[k] for k in ("kind", "tool", "reason", "sha", "subject", "path") if k in row})
                if len(out) >= cap:
                    break
    except OSError:
        pass
    return out


def hermes_provider(backend):
    """Map a route backend name to (provider_config, model, token). Refuses native Anthropic and anything load_backend
    can't resolve. The token is returned for the process env only; it is never written to disk."""
    if not backend or backend == "claude":
        raise HermesRefused("hermes adapter needs a non-Anthropic route backend (no Claude Code OAuth path in "
                            "hermes; adopt_external_logins is forced off) - set the row's backend")
    model, benv, _effort = load_backend(backend)
    base = (benv.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    token = benv.get("ANTHROPIC_AUTH_TOKEN") or benv.get("ANTHROPIC_API_KEY")
    if not model or not base or not token:
        raise HermesRefused(f"hermes adapter: backend {backend!r} not resolvable (model/base_url/auth) in "
                            f"{SPAWN_MODELS_JSON} - refusing (fail-closed)")
    transport, overrides = hermes_backend_options(backend)
    provider = {"api": base + "/v1", "key_env": KEY_ENV, "transport": transport, "default_model": model}
    ctx = benv.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
    if ctx and str(ctx).isdigit():
        provider["context_length"] = int(ctx)
    return provider, model, token, overrides


# Optional per-backend `hermes: {transport, overrides}` block in spawn-models.json. Nested so the CC
# spawners (the spawn scripts / cc.load_backend) never see hermes-only keys. ABSENT == the
# earlier behaviour exactly (chat_completions, no model_overrides). Malformed == refused (fail-closed).
# overrides: the backend's route names are not in hermes' model catalog, so hermes guesses capabilities;
# supports_reasoning=True stops it stripping thinking on a reasoning model (e.g. the litellm anthropic-* routes).
HERMES_TRANSPORTS = ("chat_completions", "anthropic_messages")
HERMES_OVERRIDE_KEYS = frozenset({"supports_reasoning", "supports_vision"})


def hermes_backend_options(backend):
    """Map a backend name to (transport, overrides dict). Validates the registry's `hermes` block."""
    entry = _cc._backend_entry(backend) or {}
    block = entry.get("hermes")
    if block is None:
        return "chat_completions", {}
    if not isinstance(block, dict) or set(block) - {"transport", "overrides"}:
        raise HermesRefused(f"hermes adapter: backend {backend!r} has a malformed `hermes` block "
                            f"(allowed keys: transport, overrides) - refusing (fail-closed)")
    transport = block.get("transport", "chat_completions")
    if transport not in HERMES_TRANSPORTS:
        raise HermesRefused(f"hermes adapter: backend {backend!r} hermes.transport {transport!r} not one of "
                            f"{HERMES_TRANSPORTS} - refusing (fail-closed)")
    overrides = block.get("overrides", {})
    if (not isinstance(overrides, dict) or set(overrides) - HERMES_OVERRIDE_KEYS
            or not all(isinstance(v, bool) for v in overrides.values())):
        raise HermesRefused(f"hermes adapter: backend {backend!r} hermes.overrides must map "
                            f"{sorted(HERMES_OVERRIDE_KEYS)} to booleans - refusing (fail-closed)")
    return transport, dict(overrides)


def hermes_config(system_prompt, provider, model, toolsets=(), gated=False, cwd=None, shell_init_files=(),
                  overrides=None):
    """The private config.yaml (as JSON, a YAML subset). Non-secret: names KEY_ENV, never a value. Only the
    GRANTED toolsets leave the disabled list; eph-gate is the only plugin, and only on a gated turn."""
    cfg = {
        "model": {"provider": PROVIDER, "default": model},
        "providers": {PROVIDER: provider},
        "agent": {"system_prompt": system_prompt,
                  "disabled_toolsets": [t for t in _DISABLED_TOOLSETS if t not in set(toolsets)]},
        "approvals": {"single_query_mode": "deny", "unattended_mode": "deny", "cron_mode": "deny"},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "mcp_servers": {},
        # load_timeout_seconds 0 = import + register() INLINE, no deadline (plugins_loader.py
        # run_with_load_deadline: `if timeout <= 0: return fn()`). The default 10 s deadline DISCARDS a slow
        # plugin with only a warning; under box load that is a turn with file+terminal and no gate.
        "plugins": {"enabled": [GATE_PLUGIN] if gated else [], "load_timeout_seconds": 0},
        # LSP off: lsp.enabled defaults True, spawns language servers (reading repo config) after every
        # write inside a git worktree and, with install_strategy auto, npm/go/pip-installs them, all outside
        # pre_tool_call. enabled False makes LSPService.is_active() False and get_service() None, so no server.
        "lsp": {"enabled": False, "install_strategy": "manual"},
        "tools": {"tool_search": {"enabled": "off"}},     # no tool_call bridge; eph-gate denies it too
        "auth": {"adopt_external_logins": False},   # never borrow the user's Claude Code / Codex OAuth
        "updates": {"check": False},
        # no tirith: hermes would exec $HERMES_HOME/bin/tirith (or download "latest", unpinned) on every terminal
        # command (tools/tirith_security.py _load_security_config / ensure_installed); eph-gate enforces.
        # allow_lazy_installs False: tools/lazy_deps.py's _allow_lazy_installs() reads this key and,
        # if false, blocks BOTH ensure()/install_specs() before any uv/pip subprocess runs. Otherwise a
        # non-editable install still has a lazy-install uv cwd unpinned (root/pyproject.toml is gone), so uv
        # would inherit the worker's cwd and read a worker-planted uv.toml (index-url/find-links): a gate bypass
        # via an installed wheel imported into the shared hermes process.
        "security": {"tirith_enabled": False, "allow_lazy_installs": False},
        "telemetry": {"shared_metrics": {"enabled": False, "send": False}},
    }
    if overrides:                                      # only when the backend's `hermes` block sets them
        cfg["model_overrides"] = {model: dict(overrides)}
    if gated:
        # local shell in the worker cwd; don't auto-source ~/.profile/~/.bashrc into the command snapshot. The
        # only init file is the adapter's own (venv runner PYTHONPATH), when there is one.
        cfg["terminal"] = {"backend": "local", "cwd": cwd, "auto_source_bashrc": False,
                           "shell_init_files": list(shell_init_files)}
    return cfg


# CC rule names a READ-only capability's grant must never carry (checked by gate_policy, fail-closed)
_READ_ONLY_FORBIDDEN_RULES = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"})


def gate_policy(capability, cwd, test_runner=None, test_repo=None, nonce=None):
    """The per-turn eph-gate policy, derived from the CC grant for the SAME capability (never a copied list)."""
    grant = _cc.capability_tool_grant(capability, test_runner=test_runner, test_repo=test_repo)
    if capability in _gate.READ_ONLY_CAPABILITIES and ("--permission-mode" in grant or any(
            r.split("(", 1)[0] in _READ_ONLY_FORBIDDEN_RULES for r in _gate.split_rules(
                grant[grant.index("--allowedTools") + 1] if "--allowedTools" in grant else ""))):
        # belt under the gate's own hard deny: a read-only capability whose CC grant ever gains edits/Bash is a
        # policy regression; refuse rather than hand hermes a widened grant (fail-closed)
        raise HermesRefused(f"hermes adapter: {capability} must be a pure-read grant, cc.capability_tool_grant "
                            f"returned a writer/Bash rule or a permission mode - refusing (fail-closed)")

    def _val(flag):
        return grant[grant.index(flag) + 1] if flag in grant else ""
    general = capability == "general"
    cwd = os.path.realpath(cwd)
    try:
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        top = ""
    read_root = os.path.realpath(top) if top and (cwd + "/").startswith(os.path.realpath(top).rstrip("/") + "/") \
        else cwd
    pol = {"version": 1, "nonce": nonce or secrets.token_hex(16), "capability": capability,
           "cwd": cwd, "write_root": cwd, "read_root": read_root,
           "edits": _val("--permission-mode") == "acceptEdits",
           "allow": _gate.split_rules(_val("--allowedTools")),
           "deny": _gate.split_rules(_val("--disallowedTools")),
           "secret_globs": list(_cc._SECRET_FILE_GLOBS)}
    if general:
        # CC's general grant is `--permission-mode auto` plus a deny list and NO allow list (the
        # classifier decides). Here: reads/searches are the gate's own (repo-confined + secret floor + the imported
        # Read denies), edits stay inside the worktree, and every terminal / write_file / patch ALSO needs a
        # judge allow (policy.judge). read_root stays the repo: CC general reads outside it (14/29 reads in
        # the 10-16 inventory) are a known narrowing, widened only on evidence.
        pol.update(edits=True, allow=["Read", "Grep", "Glob"], judge=dict(GENERAL_JUDGE))
    return pol


# The interpreter-path tripwire. MUST be the first statement of anything run with cwd = a worktree: `sys`
# is built in and `os` already imported by site, so nothing OF OURS is looked up on sys.path before the check:
# it runs before any hermes import. (Site-time work, meaning .pth exec lines, sitecustomize/usercustomize, a
# PYTHONWARNINGS import, already ran before -c even starts and searches the site-time sys.path too; that is
# safe today because scrub_worker_env drops empty/relative PYTHONPATH components and the broker sets no
# PYTHONPATH, not because the guard runs first.) Refused:
# a relative entry (resolves against the cwd = the worktree: an editable install's `__editable__.*.__path_hook__`
# placeholder, or '' without -P), an entry AT/UNDER the cwd (python turns an empty/relative PYTHONPATH component
# into "<cwd>[/rel]" before we see it), and an `__editable__` finder (meta path / path hook / module). On refusal
# it prints PATH_REFUSE_TAG and the reprs, then exits 3; hermes is never imported.
PATH_REFUSE_TAG = "EPH_PATH_REFUSE"
PATH_OK_TAG = "EPH_PATH_OK"
PATH_CHECK_ARG = "eph-path-check"
_PATH_GUARD = r"""
import sys, os
def _eph_mod(o):
    return str(getattr(o, "__module__", None) or getattr(type(o), "__module__", None) or "")
_eph_cwd = os.path.realpath(os.getcwd())
def _eph_under_cwd(p):      # python ABSOLUTISES a relative PYTHONPATH entry against the cwd ("rel" -> "<cwd>/rel")
    r = os.path.realpath(p)
    return r == _eph_cwd or r.startswith(_eph_cwd.rstrip("/") + "/")
_eph_bad = [p for p in sys.path if not isinstance(p, str) or not os.path.isabs(p) or _eph_under_cwd(p)]
_eph_bad += [p for p in sys.path if isinstance(p, str) and "__editable__" in p]
_eph_bad += [k for k in list(sys.modules) if "__editable__" in k]
_eph_bad += [repr(o) for o in list(sys.meta_path) + list(sys.path_hooks) if "__editable__" in _eph_mod(o)]
if _eph_bad:
    print("%s %r" % ("EPH_PATH_REFUSE", _eph_bad[:20]))
    sys.stdout.flush()
    os._exit(3)
del _eph_bad, _eph_mod, _eph_cwd, _eph_under_cwd
"""
# the standalone check (a turn without a probe; none today, every honoured capability is gated): the guard,
# then the OK tag. argv[1] = PATH_CHECK_ARG.
PATH_CHECK = _PATH_GUARD + 'print("EPH_PATH_OK")\n'

_PROBE_BODY = r"""
import json
# argv[1] = the REAL run's argv (JSON), argv[2] = the probe tool name. Run as `python -P -c`: the implicit cwd
# entry is not on sys.path, as for the hermes console script (sys.path[0] = venv/bin there); the guard above
# refused any other relative entry.
real_argv, probe_tool = json.loads(sys.argv[1]), sys.argv[2]
sys.argv = real_argv            # the startup pre-parse (profile flag scan, gateway checks) sees the run's argv
out = {}
try:    # the REAL startup prelude, as the console script runs it before main(): bootstrap, profile override
    import hermes_cli.main      # (-p / <root>/active_profile re-home), scratch env, <HERMES_HOME>/.env + .op.env
except BaseException as e:      # + managed + install-dir dotenv load (override=True). SystemExit included.
    out["startup"] = repr(e)
out["home"] = os.environ.get("HERMES_HOME")
out["env"] = {k: v for k, v in os.environ.items() if k.startswith("HERMES_")}
try:
    from hermes_constants import get_hermes_home
    out["home_resolved"] = str(get_hermes_home())
except Exception as e:
    out["home_resolved"] = repr(e)
try:    # main() execvp's into a container when <HERMES_HOME>/.container-mode exists
    from hermes_cli.config import get_container_exec_info
    out["container"] = get_container_exec_info() is not None
except Exception as e:
    out["container"] = repr(e)
try:    # hermes loads <install dir>/.env into its own env at startup: the broker install must keep none
    from hermes_cli import _startup_fast
    out["install_env"] = os.path.lexists(os.path.join(_startup_fast.project_root_str(), ".env"))
except Exception as e:
    out["install_env"] = repr(e)
from hermes_cli.plugins import get_pre_tool_call_directive
for key, tool, args in (("probe", probe_tool, {}), ("floor", "terminal", {"command": "git push https://example.invalid HEAD"})):
    try:
        out[key] = list(get_pre_tool_call_directive(tool, args))
    except Exception as e:
        out[key] = ["error", repr(e)]
try:    # B1: the plugin emptied the shell-linter table; LSP is off
    from tools.file_operations_lint import LINTERS
    out["linters"] = sorted(LINTERS)
except Exception as e:
    out["linters"] = ["error", repr(e)]
try:
    from agent.lsp import get_service
    out["lsp"] = get_service() is not None
except Exception as e:
    out["lsp"] = repr(e)
print("EPH_GATE_PROBE " + json.dumps(out))
"""
_PROBE = _PATH_GUARD + _PROBE_BODY


def probe_argv(python, run_argv):
    """The pre-flight probe command. `-P` (PYTHONSAFEPATH; hermes needs Python >= 3.11, where it exists) keeps
    the cwd off sys.path: with plain `-c`, '' (= the worktree) is sys.path[0], so a worker-written json.py /
    utils.py / hermes_cli/ would run inside the probe, unconfined, and could forge its answer.
    `-P` drops only that implicit entry: an editable install's relative placeholder entry stays, which is
    why the probe's FIRST statement is the interpreter-path guard. NOT -I / -E: those also drop PYTHON*
    env vars (PYTHONPATH, ...), which the real run honours, and the probe must see the run's env. The venv
    already has no user site."""
    return [python, "-P", "-c", _PROBE, json.dumps(list(run_argv)), _gate.PROBE_TOOL]


def path_check_argv(python):
    """The standalone interpreter-path check for turns without a probe (fallback; mr-review is gated)."""
    return [python, "-P", "-c", PATH_CHECK, PATH_CHECK_ARG]


def _path_refusal(stdout):
    """The guard's refusal line, or None."""
    for ln in (stdout or "").splitlines():
        if ln.startswith(PATH_REFUSE_TAG + " ") or ln == PATH_REFUSE_TAG:
            return ln[len(PATH_REFUSE_TAG):].strip()
    return None


def _refuse_path(what):
    raise HermesRefused(f"hermes adapter: the hermes interpreter's sys.path has a relative entry or an editable-"
                        f"install finder ({what:.300}) - it would import from the worktree cwd; the broker's "
                        f"hermes must be a NON-editable install - refusing the turn (fail-closed)")


def _hermes_python(hermes_bin):
    """The interpreter of the hermes console script (its shebang), else the venv's python next to it."""
    try:
        with open(hermes_bin, "rb") as f:
            first = f.readline().decode("utf-8", "replace").strip()
        if first.startswith("#!") and " " not in first[2:].strip() and os.path.isabs(first[2:].strip()):
            return first[2:].strip()
    except OSError:
        pass
    return os.path.join(os.path.dirname(hermes_bin), "python")


# chars PyYAML rejects, or folds as line breaks (NEL/LS/PS), when RAW; json.dumps(ensure_ascii=False) leaves them
# raw. ensure_ascii=True is no fix: astral chars become \ud83d\ude00 pairs, which PyYAML decodes to lone surrogates.
_YAML_NONPRINTABLE = re.compile("[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]")


def yaml_safe_json(obj):
    """JSON text that PyYAML's safe_load reads back identically (JSON is a YAML subset, bar these chars)."""
    return _YAML_NONPRINTABLE.sub(lambda m: "\\u%04x" % ord(m.group()),
                                  json.dumps(obj, ensure_ascii=False, indent=1))


def build_argv(hermes_bin, model, toolsets, max_turns, run_budget_s, resume_session=None):
    argv = [hermes_bin, "chat", "--query-file", "-", "--format", "stream-json", "-Q",
            "--ignore-rules", "--source", "tool",
            "--provider", PROVIDER, "-m", model,
            "-t", ",".join(toolsets),
            "--max-turns", str(max_turns), "--run-budget", str(run_budget_s)]
    if resume_session:
        if not _SESSION_ID_RE.fullmatch(str(resume_session)):   # fullmatch: "$" alone would pass "abc\n"
            raise HermesRefused(f"hermes adapter: refusing to --resume a malformed session id {resume_session!r}")
        argv.append(f"--resume={resume_session}")      # =form: an id can never be parsed as a flag
    return argv


# stream-json record types a failed-turn capture may keep. Everything else (tool_use / tool results /
# text deltas, which can echo raw file or command output) and every non-JSON line is DROPPED, counted only.
_CAPTURE_KEEP_TYPES = frozenset({"system", "result", "error"})


def capture_stdout(out):
    """The postmortem-safe view of hermes stream-json stdout: only system/result/error records are
    kept; tool output and non-JSON lines are replaced by a drop count (cc.capture_failed_turn then redacts
    secret-shaped text and caps the size)."""
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
        kept.append(f"[{dropped} other stream line(s) dropped: tool output / non-JSON never captured]")
    return "\n".join(kept)


def parse_stream(out):
    """Convert hermes stream-json stdout to the CC `--output-format json` wrapper shape, or None if no result record.
    Only the LAST {"type":"result"} line counts; non-JSON lines are ignored."""
    res = None
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            res = obj
    if res is None:
        return None
    tok = res.get("tokens") or {}
    wrapper = {"result": res.get("text") or "", "session_id": res.get("session_id") or None,
               "usage": {"input_tokens": tok.get("input") or 0, "output_tokens": tok.get("output") or 0},
               "harness": "hermes", "exit_code": res.get("exit_code")}
    if res.get("error"):
        wrapper["error"] = str(res["error"])
    return wrapper


def _kill_group(proc):
    if proc.poll() is not None:               # already reaped: its pid (== pgid) may be reused, so never signal it
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)   # start_new_session=True makes pgid == pid, so this kills tool children too
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


class HermesHarness:
    def __init__(self, hermes_bin=None, home_root=None):
        found = shutil.which(hermes_bin or HERMES_BIN)
        self.hermes_bin = os.path.abspath(found) if found else None     # absolute once (never re-looked-up on PATH)
        self.home_root = os.path.abspath(home_root or HERMES_HOME_ROOT)
        if os.path.islink(self.home_root):
            raise HermesRefused(f"hermes adapter: home root {self.home_root} is a symlink - refusing")
        os.makedirs(self.home_root, mode=0o700, exist_ok=True)
        os.chmod(self.home_root, 0o700)                                   # pre-existing root: tighten it too
        self._sweep()

    def _sweep(self):
        """Remove leftover homes (broker crash/kill skipped the finally) older than HOME_MAX_AGE_S. The harness
        is built once per broker process, before any hermes worker of this process exists."""
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
                log.warning("hermes home sweep: %s: %s", path, e)

    def _home(self, worker):
        home = getattr(worker, "hermes_home", None)
        if home and os.path.isdir(home):
            os.utime(home)                                  # mtime = last turn (sweep age)
            return home
        home = tempfile.mkdtemp(prefix=f"{worker.run_id}-", dir=self.home_root)   # mkdtemp: 0700
        worker.hermes_home = home
        weakref.finalize(worker, shutil.rmtree, home, True)   # backstop; _run_worker's finally removes it first
        return home

    @staticmethod
    def _sweep_home(home):
        """Reduce the private home to _HOME_KEEP / state.db* before a turn (N-N1). Never follows a symlink; a
        kept name that is a symlink is removed. Raises HermesRefused if anything can't be removed."""
        for name in os.listdir(home):
            path = os.path.join(home, name)
            link = os.path.islink(path)
            if not link and (name in _HOME_KEEP or name.startswith(_HOME_KEEP_PREFIX)):
                continue
            try:
                if os.path.isdir(path) and not link:
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
            except OSError as e:
                raise HermesRefused(f"hermes adapter: cannot sweep {name!r} from the private home ({e}) - refusing")

    def _install_gate(self, home, policy, env, cwd, run_argv):
        """Copy eph-gate into the private home, write this turn's policy, and PROVE the gate is live via hermes'
        own startup prelude + pre_tool_call dispatch (same interpreter, home, env, cwd and argv as the run).
        Returns the nonce, or raises HermesRefused (no launch)."""
        dst = os.path.join(home, "plugins", GATE_PLUGIN)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(_GATE_SRC, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in (SENTINEL_FILE, POLICY_FILE, DENIALS_FILE):
            try:
                os.unlink(os.path.join(home, name))
            except FileNotFoundError:
                pass
        fd = os.open(os.path.join(home, POLICY_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(policy, f)
        nonce = policy["nonce"]
        try:
            r = subprocess.run(probe_argv(_hermes_python(self.hermes_bin), run_argv), stdin=subprocess.DEVNULL,
                               cwd=cwd, env=env, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            raise HermesRefused(f"hermes adapter: eph-gate pre-flight probe failed ({type(e).__name__}) - "
                                f"refusing the turn (fail-closed)")
        bad_path = _path_refusal(r.stdout)
        if bad_path is not None:
            _refuse_path(bad_path)
        try:
            line = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("EPH_GATE_PROBE ")][-1]
            res = json.loads(line[len("EPH_GATE_PROBE "):])
        except (IndexError, ValueError) as e:
            raise HermesRefused(f"hermes adapter: eph-gate pre-flight probe failed ({type(e).__name__}) - "
                                f"refusing the turn (fail-closed)")
        probe, floor = res.get("probe") or [], res.get("floor") or []
        if probe != ["block", f"eph-gate probe ok {nonce}"] or floor[:1] != ["block"] \
                or "eph-gate" not in str(floor[1:2]):
            raise HermesRefused(f"hermes adapter: eph-gate is not verifiably loaded (probe={probe!r:.200} "
                                f"floor={floor!r:.200}) - refusing the turn (fail-closed)")
        # the startup the real run will see: the prelude imported cleanly, no HERMES_* switch beyond the ones
        # hermes sets itself, still THIS home after the profile override (env AND hermes' resolved home), its
        # scratch dir inside it, no container re-exec, no install-dir .env, and no side-channel toolchain after
        # writes (shell linters emptied by the plugin, LSP off)
        env_after = res.get("env") if isinstance(res.get("env"), dict) else {"?": "?"}
        extra_env = sorted(k for k in env_after if k not in _HERMES_STARTUP_ENV)
        scratch = env_after.get("HERMES_SCRATCH_DIR")
        home_ok = res.get("home") == home and res.get("home_resolved") == home \
            and env_after.get("HERMES_HOME") == home \
            and (scratch is None or (os.path.realpath(scratch) + "/").startswith(os.path.realpath(home) + "/"))
        if res.get("startup") or extra_env or not home_ok or res.get("container") is not False \
                or res.get("install_env") is not False or res.get("linters") != [] or res.get("lsp") is not False:
            raise HermesRefused(f"hermes adapter: hermes startup env/tooling not as required (startup="
                                f"{res.get('startup')!r:.120} env={extra_env!r:.200} home_ok={home_ok} "
                                f"container={res.get('container')!r:.80} install_env={res.get('install_env')!r:.80} "
                                f"linters={res.get('linters')!r:.120} lsp={res.get('lsp')!r:.80}) - refusing the "
                                f"turn (fail-closed)")
        for name in (SENTINEL_FILE, DENIALS_FILE):   # the probe's own; the run writes a fresh sentinel, and the
            try:                                      # probe's floor-check deny is not a worker denial
                os.unlink(os.path.join(home, name))
            except FileNotFoundError:
                pass
        return nonce

    def _check_interpreter(self, env, cwd):
        """Interpreter-path check for a turn WITHOUT a probe (fallback; every honoured capability runs the probe):
        the hermes interpreter, same env and cwd as the run, must have no relative sys.path entry and no editable
        finder."""
        try:
            r = subprocess.run(path_check_argv(_hermes_python(self.hermes_bin)), stdin=subprocess.DEVNULL, cwd=cwd,
                               env=env, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            raise HermesRefused(f"hermes adapter: interpreter path check failed ({type(e).__name__}) - refusing "
                                f"the turn (fail-closed)")
        bad_path = _path_refusal(r.stdout)
        if bad_path is not None:
            _refuse_path(bad_path)
        if r.returncode != 0 or PATH_OK_TAG not in (r.stdout or "").splitlines():
            raise HermesRefused(f"hermes adapter: interpreter path check did not pass (rc={r.returncode}) - "
                                f"refusing the turn (fail-closed)")

    @staticmethod
    def _gate_ran(home, nonce):
        try:
            with open(os.path.join(home, SENTINEL_FILE), encoding="utf-8") as f:
                return json.load(f).get("nonce") == nonce
        except (OSError, ValueError, AttributeError):
            return False

    def one_turn(self, adapter, worker, route, system_prompt, prompt_text, resume_session,
                 backend_override=_NO_OVERRIDE, wall_clock_override=None):
        """One hermes turn. Returns the parsed envelope or None (parse miss); the CC _one_turn contract."""
        # never launch for a torn-down worker (teardown sets worker.done BEFORE its kill). CCAdapter's re-prompt path
        # also stops on done; this is the harness-side belt, plus the post-Popen re-check below.
        if getattr(worker, "done", False):
            log.info("hermes turn key=%s: worker torn down -> not launching", worker.key)
            return None
        if not system_prompt:
            raise ValueError("refusing to spawn without a system prompt (constraint #1)")
        if not self.hermes_bin:
            raise HermesRefused(f"hermes adapter: binary {HERMES_BIN!r} not found - refusing")
        capability = getattr(route, "capability", "read")
        toolsets = hermes_toolsets(capability)                                    # refuse BEFORE any launch
        if capability in _gate.READ_ONLY_CAPABILITIES and set(toolsets) - {"file"}:
            raise HermesRefused(f"hermes adapter: read-only {capability} may only get the file toolset "
                                f"(got {toolsets!r}) - refusing (fail-closed)")
        backend = getattr(route, "backend", "") if backend_override is _NO_OVERRIDE else backend_override
        provider, model, token, overrides = hermes_provider(backend)
        max_turns = adapter.max_turns_by_capability.get(capability, adapter.max_turns)
        wall_clock_s = adapter.wall_clock_by_capability.get(capability, adapter.wall_clock_s)
        # Same local-backend widening and bounded-reprompt cap as cc.CCAdapter._one_turn: a kind:"local"
        # vLLM rung is measurably slower than hosted Claude, and the ONE re-prompt after a wall-clock
        # hard-kill must stay short (never a second full, possibly scaled, allotment).
        local_turn = _is_local_backend(backend)
        if local_turn:
            wall_clock_s = wall_clock_s * LOCAL_WALL_CLOCK_MULT
        if wall_clock_override is not None:
            wall_clock_s = min(wall_clock_s, wall_clock_override)
        log.info("hermes spawn key=%s capability=%s backend=%s local=%s effective wall-clock=%ss",
                 worker.key, capability, backend or "claude", local_turn, wall_clock_s)
        gated = capability in GATED_CAPABILITIES
        if not (worker.worktree and os.path.isdir(worker.worktree)):   # every hermes turn runs IN a worktree
            raise HermesRefused(f"hermes adapter: {capability} turn has no worktree cwd - refusing (fail-closed)")
        home = self._home(worker)
        self._sweep_home(home)                             # N-N1: allowlist sweep (also covers B2's .env/.op.env)
        cwd = os.path.realpath(worker.worktree) if gated else None
        init_files = []
        if gated:
            test_runner = getattr(worker, "test_runner", None) or getattr(route, "test_runner", "") or None
            test_repo = getattr(worker, "repo", None) if test_runner == "venv" else None
            _wt_src = os.path.join(cwd, "src")             # G2 parity with the CC path (src-layout venv repos)
            if test_runner == "venv" and os.path.isdir(_wt_src):
                # N-N3: on the terminal shell only (its login snapshot), NOT the hermes process env
                init_files = [os.path.join(home, TERMINAL_ENV_FILE)]
                try:
                    fd = os.open(init_files[0], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except OSError as e:           # re-planted by a same-uid racer after the sweep (N2-4)
                    raise HermesRefused(f"hermes adapter: cannot create {TERMINAL_ENV_FILE} in the private home "
                                        f"({type(e).__name__}) - refusing (fail-closed)")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(f'export PYTHONPATH={shlex.quote(_wt_src)}"${{PYTHONPATH:+:$PYTHONPATH}}"\n')
        with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(yaml_safe_json(hermes_config(system_prompt, provider, model, toolsets, gated=gated, cwd=cwd,
                                                 shell_init_files=init_files, overrides=overrides)))
        argv = build_argv(self.hermes_bin, model, toolsets, max_turns, wall_clock_s, resume_session)
        # same allowlist scrub as a CC worker; the token rides ONLY as KEY_ENV (no ANTHROPIC_* passed through)
        env = scrub_worker_env(os.environ, route_env=route.env, backend_env={KEY_ENV: token})
        for k in [k for k in env if k.startswith(_POPPED_ENV_PREFIXES)]:  # N-N2: no HERMES_* but ours; no TIRITH_*
            del env[k]
        env["HERMES_HOME"] = home
        nonce = None
        if gated:
            env["TERMINAL_CWD"] = cwd                      # hermes' terminal/file base dir == the gate's cwd
            nonce = self._install_gate(home, gate_policy(capability, cwd, test_runner, test_repo), env, cwd, argv)
        else:                                              # no probe: the minimal interpreter-path gate
            self._check_interpreter(env, worker.worktree)
        worker.turn_wall_clock = wall_clock_s
        worker.kill_group = True                           # teardown kills the process group, not just the pid
        worker.in_turn = True
        worker.turn_started = time.time()
        worker.hard_killed = False                          # reset before this turn; set True only below
        try:                                               # Popen inside: an exec failure still clears in_turn
            proc = subprocess.Popen(argv, cwd=worker.worktree, env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
            worker.proc = proc
            if getattr(worker, "done", False):         # teardown landed between the check above and Popen
                _kill_group(proc)
            try:
                out, err = proc.communicate(prompt_text, timeout=wall_clock_s)   # wall-clock hard-kill
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                out, err = proc.communicate()
                worker.hard_killed = True                   # _turn_with_envelope caps the warm re-prompt
                log.warning("HARD-KILL key=%s wall-clock %ss exceeded (hermes)", worker.key, wall_clock_s)
                adapter._alert_ops(f"worker {worker.key} hard-killed: wall-clock {wall_clock_s}s (hermes)",
                                   subject=f"worker:{worker.key}")
        finally:
            worker.in_turn = False
            worker.turn_started = None
        worker.turns += 1
        wrapper = parse_stream(out)
        # Failed-turn postmortem capture: the stream is filtered (capture_stdout), never raw
        _capture = lambda reason: _cc.capture_failed_turn(worker, route, reason, proc.returncode,
                                                          capture_stdout(out), err)
        # escalations wrap last_output UNTRUSTED; with no result record keep the raw tail (stderr if stdout empty)
        worker.last_output = json.dumps(wrapper) if wrapper else ((out or "").strip() or (err or ""))[-4000:]
        if wrapper is None:
            log.warning("hermes turn key=%s rc=%s: no result record -> parse miss", worker.key, proc.returncode)
            _capture("no result record")
            return None
        if wrapper.get("session_id"):
            worker.session_id = wrapper["session_id"]      # kept even on failure: the warm re-prompt --resumes it
        if gated and not self._gate_ran(home, nonce):
            log.error("hermes turn key=%s: eph-gate load sentinel missing/mismatched after the run -> parse miss",
                      worker.key)
            adapter._alert_ops(f"worker {worker.key}: hermes eph-gate was not provably live in the run "
                               f"(sentinel missing) - result discarded", subject=f"worker:{worker.key}")
            _capture("eph-gate sentinel missing")
            return None
        # PASS gating: only a CLEAN run may yield an envelope. A failed / partial / capped / killed run is a parse
        # miss (then a warm re-prompt, then an escalation), even if its text happens to hold a valid envelope.
        if wrapper.get("exit_code") != 0 or wrapper.get("error") or proc.returncode != 0:
            log.warning("hermes turn key=%s failed (exit_code=%s rc=%s error=%r) -> parse miss", worker.key,
                        wrapper.get("exit_code"), proc.returncode, (wrapper.get("error") or "")[:200])
            _capture("failed run (non-zero exit / error)")
            return None
        parse = getattr(adapter.envelope_parser, "try_parse", adapter.envelope_parser)
        result = parse(worker.last_output, worker.run_id, route)
        if result is None:                                 # e.g. an out-of-set status for the capability
            _capture("parse miss")
        if result is not None:
            worker.tokens += int(result.get("tokens", 0) or 0)
            if gated:
                denials = read_denials(home)
                if denials:
                    result["gate_denials"] = denials
        return result
