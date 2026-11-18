#!/usr/bin/env python3
"""dash.py: the dash for the ephemeral-worker broker.

Scope: INSIGHT. What is running and its current state; this module never writes. The page's buttons call
actions.py (POST /actions/<verb>), which owns the auth and the writes, and only appear when GET /actions
says the viewer may act.

Why a separate module: `snapshot()` takes the broker as an argument rather than being a Broker method,
so it can be exercised against a fake without standing up a real broker (which would spawn real
workers). app.py stays a wiring file; this owns the view.

SAFETY: what this deliberately does NOT expose:
  * It counts buffered events, it never dumps them. An event body is raw ticket/webhook content; the
    dash is reachable from anywhere on the broker host via loopback, so payloads stay out of it.
  * session_id is truncated to 8 chars (correlation only, not a resumable handle).
  * No env, no paths, no tokens, no worktree contents.
"""

import datetime
import json
import logging
import math
import os
import re
import stat
import subprocess
import threading
import time

from transcript_assets import transcript_basic, transcript_css, transcript_format, transcript_renderer, transcript_text

import coalescer
import site_env  # noqa: F401  config/site.env carries the FLEET_*/SUPERVISOR_STATUS_PATH values
import stream
import tickets

log = logging.getLogger("eph-broker.dash")

# ---------------------------------------------------------------- fleet
# READ-ONLY view of the fleet: fleet-status.json (a generator rewrites it periodically; either the
# deployment's own job, or FLEET_STATUS_GEN below, run BY THE BROKER) + the FLEET_MODE line of the mode
# file (FLEET_MD_PATH). Fail-soft by contract: a missing, unreadable, garbage or stale source yields
# {"error": ...} / stale=True, NEVER an exception (so /state can't 500 over it). Cached ~15s so the 5s
# /state poll doesn't re-read the files on every hit.

# FLEET_STATUS_PATH / FLEET_MD_PATH come from site.env; unset means the fleet view reports "not configured".
FLEET_STATUS_DEFAULT = ""
FLEET_MD_DEFAULT = ""
# The session-supervisor's own per-session view (resurrect attempts, give-ups, stale beats,
# stuck dialogs), rewritten every sweep (~30s). Optional: if it's missing, no flags are shown.
SUPERVISOR_STATUS_DEFAULT = ""   # SUPERVISOR_STATUS_PATH (site.env); unset means no flags
SUPERVISOR_STALE_S = 180
SILENT_S = 3 * 3600   # Tower: busy this long with no STATE.md/TASKS.md edit = "silent while busy"
FLEET_STALE_S = 300
FLEET_CACHE_S = 15
FLEET_MAX_AGENTS = 200
# FLEET_STATUS_GEN (site.env): an executable that (re)writes FLEET_STATUS_PATH. Unset = feature off (the
# fleet view just reads whatever last wrote the file, same as before this existed). When set, the broker
# runs it itself, at most once per FLEET_GEN_PERIOD_S, fire-and-forget on a daemon thread, so a slow or hung
# generator never blocks an HTTP request; no external timer/cron is needed to keep the file fresh. No
# shell: the path is exec'd directly, argv-only, no injection surface.
# Contract: the generator is invoked as `FLEET_STATUS_GEN <FLEET_STATUS_PATH>`: it must write its output to
# argv[1], NOT to some default/cwd-relative path of its own (a generator whose own default output path is
# relative resolves it against the broker's cwd, not FLEET_STATUS_PATH). If FLEET_STATUS_PATH is unset,
# the generator is never run, even when FLEET_STATUS_GEN is set (there's no path to tell it to write).
FLEET_STATUS_GEN_DEFAULT = ""
FLEET_GEN_PERIOD_S = 60
FLEET_GEN_TIMEOUT_S = 30
_MODE_RE = re.compile(r"^FLEET_MODE: (high|normal|lean|min|zero)$", re.M)
_MODEL_RE = re.compile(r"^(?:claude-)?(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?(?:-\d{8})?$")
_fleet_lock = threading.Lock()
_fleet_cache = {"key": None, "at": 0.0, "val": None}


def _container_file(mounts, path, limit):
    """Read bounded bytes and metadata from a mounted regular file, or return None.
    Anchor every open to a directory fd: container-controlled symlink swaps must never redirect a
    read outside the mount. Nonblocking opens plus fstat refuse FIFOs and other non-regular files.
    """
    matches = []
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        dest, src = mount.get("Destination", ""), mount.get("Source", "")
        if not isinstance(dest, str) or not dest or not isinstance(src, str) or not os.path.isabs(src):
            continue
        if path == dest or path.startswith(dest.rstrip("/") + "/"):
            matches.append((dest, src))
    if not matches:
        return None
    dest, src = max(matches, key=lambda m: len(m[0]))
    if path == dest:  # A single-file bind mount has no path components beneath its source.
        src, leaf = os.path.split(src)
        parts = [leaf]
    else:
        parts = os.path.relpath(path, dest).split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    directory_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    try:
        directory = os.open(src, directory_flags)
        try:
            for part in parts[:-1]:
                child = os.open(part, directory_flags, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    return None
                return os.read(fd, limit), info
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    except (OSError, ValueError):
        return None  # Mounted notes are optional, including files replaced during discovery.


def _container_notes(mounts, config_path, workspace, now):
    model, line, age = None, None, None
    content = _container_file(mounts, config_path, 65537)
    if content:
        try:
            raw, _ = content
            if len(raw) <= 65536:
                text = raw.decode("utf-8")
                try:
                    data = json.loads(text)  # JSON is also YAML; needs no optional dependency.
                except ValueError:
                    data = site_env._yaml_load(text)
                value = data.get("model") if isinstance(data, dict) else None
                value = value.get("default") if isinstance(value, dict) else None
                model = value[:200] if isinstance(value, str) else None
        except (OSError, ValueError, site_env.BrokerConfigError):
            log.warning("fleet containers: config.yaml unreadable or invalid")
    content = _container_file(mounts, workspace.rstrip("/") + "/NOW.md", 4096)
    if content:
        raw, info = content
        text = raw.decode("utf-8", errors="replace")
        line = next((s.strip() for s in text.splitlines() if s.strip() and not s.lstrip().startswith("#")), None)
        age = _round(max(0, now - info.st_mtime))
    return model, line, age


def _read_containers(source_path, now):
    """Opt-in Docker source. Project only mounts, start time, name and one configured pool label;
    never request Config.Env or run anything inside a container. A selector is mandatory.
    """
    with open(source_path, encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError("container source must be an object")
    label, prefix = config.get("label", ""), config.get("name_prefix", "")
    pool_label = config.get("pool_label", "")
    if (not all(isinstance(v, str) for v in (label, prefix, pool_label)) or not (label or prefix)
            or (pool_label and not re.fullmatch(r"[A-Za-z0-9_.-]+", pool_label))):
        raise ValueError("container source needs a label or name_prefix and a valid pool_label")
    config_path, workspace = config.get("config_path", ""), config.get("workspace", "")
    if (not isinstance(config_path, str) or not os.path.isabs(config_path)
            or os.path.basename(config_path) != "config.yaml"
            or not isinstance(workspace, str) or not os.path.isabs(workspace)
            or ".." in config_path.split("/") or ".." in workspace.split("/")):
        raise ValueError("container source needs absolute config.yaml and workspace paths")
    argv = ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}"]
    if label:
        argv += ["--filter", "label=" + label]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=3, check=True)
    ids = []
    for line in result.stdout.splitlines():
        ident, sep, name = line.partition("\t")
        if sep and name.startswith(prefix) and re.fullmatch(r"[a-f0-9]{12,64}", ident):
            ids.append(ident)
    if not ids:
        return []
    pool = '{{json (index .Config.Labels "' + pool_label + '")}}' if pool_label else '""'
    fmt = ('{"name":{{json .Name}},"started":{{json .State.StartedAt}},'
           '"running":{{json .State.Running}},"mounts":{{json .Mounts}},"pool":' + pool + '}')
    result = subprocess.run(["docker", "inspect", "--format", fmt] + ids[:FLEET_MAX_AGENTS],
                            capture_output=True, text=True, timeout=3, check=True)
    rows = []
    for line in result.stdout.splitlines():
        item = json.loads(line)
        if item.get("running") is not True:
            continue
        model, note, age = _container_notes(item.get("mounts") or [], config_path, workspace, now)
        row = _agent_row(None, {"name": item["name"].lstrip("/"), "agent_type": "tfa",
                               "runtime": "hermes", "model": model, "status": note,
                               "status_source": "now" if age is not None else None, "running": True}, now)
        # Docker emits nanoseconds, while Python 3.9's ISO parser accepts at most microseconds.
        started = item.get("started")
        if isinstance(started, str):
            started = re.sub(r"(\.\d{6})\d+", r"\1", started)
        started = _ts(started)
        row.update(runtime_kind="container", pool_bot=_scalar(item.get("pool")), ledger_age=age,
                   age=_round(max(0, now - started)) if started is not None else None,
                   flags=[], busy=None, busy_age=None, silent=False)
        rows.append(row)
    return rows


def _merge_containers(fleet, source_path, now):
    if not source_path:
        return fleet  # No docker calls and byte-for-byte legacy API shape when disabled.
    try:
        rows = _read_containers(source_path, now)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as e:
        log.warning("fleet containers: discovery failed: %s", type(e).__name__)
        fleet["container_error"] = "Container discovery failed: " + type(e).__name__
        return fleet
    existing = fleet.setdefault("agents", [])
    names = {a["name"] for a in existing}
    # Standing identities win. A discovered container replaces a legacy TFA of the same name.
    by_name = {a["name"]: a for a in rows}
    existing[:] = [by_name.pop(a["name"]) if a["agent_type"] == "tfa" and a["name"] in by_name else a
                   for a in existing]
    existing.extend(a for n, a in by_name.items() if n not in names)
    fleet["agents"] = existing[:FLEET_MAX_AGENTS]
    if rows and "error" in fleet:
        fleet["status_error"] = fleet.pop("error")
    return fleet


def fleet_mode(text):
    """The FLEET_MODE value from FLEET.md text, or None when no line matches exactly."""
    m = _MODE_RE.search(text or "")
    return m.group(1) if m else None


def _ts(v):
    """Epoch seconds from an epoch number or an ISO-8601 string; None if unparseable."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if v > 1e12 else float(v)   # tolerate epoch-ms
    if isinstance(v, str) and v.strip():
        try:
            d = datetime.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d.timestamp()
    return None


def _scalar(v):
    """Agent-controlled values ride /state as plain scalars only (the page escapes them all)."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return str(v)[:200]


def _first(a, *names):
    for n in names:
        if a.get(n) is not None:
            return a.get(n)
    return None


def model_label(m):
    """Short human label for a model id: claude-opus-5-5 -> 'opus 5.5', claude-sonnet-5 -> 'sonnet 5',
    local-vllm -> 'local', anything deepseek -> 'deepseek'. Unknown ids pass through (clipped)."""
    if not isinstance(m, str) or not m.strip():
        return None
    m = m.strip().lower()
    hit = _MODEL_RE.match(m)
    if hit:
        return hit.group(1) + " " + hit.group(2) + ("." + hit.group(3) if hit.group(3) else "")
    if m.startswith("local") or "vllm" in m:
        return "local"
    if "deepseek" in m:
        return "deepseek"
    return m[:24]


def model_state(runtime_model, runtime_at, configured, now, file_stale=False):
    """Which model reading a row may HONESTLY claim, given what evidence exists. The distinction is the
    whole point of the model display: `model` in fleet-status.json is the mode-resolved CONFIGURED model
    (fleet-model.sh's answer), never proof of what a process is running.

      "running"    observed runtime evidence (`runtime_model`), fresh enough to call current
      "stale"      runtime evidence exists but is too old to call current (or the whole source file is)
      "configured" no runtime evidence; only the configured/mode-resolved model is known
      "unknown"    neither

    Freshness: FLEET_STALE_S, the same window the context sample uses. An absent `runtime_model_at` is NOT
    read as stale: a producer that supplies only the model is trusted exactly as far as the status file
    itself is (that file's own age is `file_stale`). A timestamp >30s in the future is a clock skew, not
    evidence, and reads stale like a too-old one."""
    if runtime_model:
        if file_stale:
            return "stale"
        if runtime_at is not None:
            age = now - runtime_at
            if age > FLEET_STALE_S or age < -30:
                return "stale"
        return "running"
    return "configured" if configured else "unknown"


def context_percent(v):
    """Legacy percentage is display-only; it cannot establish an absolute token count."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = v.strip().rstrip("%").strip()
        try:
            v = float(v)
        except ValueError:
            return None
    if not isinstance(v, (int, float)) or not 0 <= v <= 100:
        return None
    return v


def context_count(v, minimum=0):
    """Token metadata must be a finite integer, not a boolean or an untrusted label."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return int(v) if v >= minimum and int(v) == v else None


def ledger_excerpt(s):
    """The STATE.md line the generator picked, minus list/quote markers so it reads as a sentence."""
    if s is None:
        return None
    s = re.sub(r"^\s*(?:[-*>]\s+)+", "", str(s)).strip()
    if len(s) > 2 and s[0] == s[-1] == "_":         # a whole-line _italic_ note
        s = s[1:-1].strip()
    return s[:200] or None


def _ledger_age(cwd, now):
    """Seconds since the agent's anchors (<cwd>/STATE.md or TASKS.md, newest wins) last changed: the real
    'last update' signal, since fleet-status.json's beat_age_secs is always null. Absolute paths only;
    neither file readable gives None."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    mt = []
    for fn in ("STATE.md", "TASKS.md"):
        try:
            mt.append(os.stat(os.path.join(cwd, fn)).st_mtime)
        except (OSError, ValueError):
            pass
    return _round(max(0.0, now - max(mt))) if mt else None


def _agent_row(name, a, now):
    """Accept additive context_tokens/window/ceiling/floor and context_sampled_at (epoch or ISO).
    Legacy context/context_pct/ctx remain display-only. No window or threshold is inferred from runtime.

    Model: `model` (fleet-status.json) is the CONFIGURED, mode-resolved model. Additive
    `runtime_model` (+ optional `runtime_model_at`, epoch or ISO) is OBSERVED runtime evidence and wins the
    headline when fresh; `model_state` says which of the two the row is showing (see model_state()).
    """
    if not isinstance(a, dict):
        a = {"status": a}
    upd = _ts(_first(a, "last_update", "updated", "updated_at", "ts", "last_seen"))
    # Real fleet-status.json rows carry `running` (bool) and `beat_age_secs` (seconds, may be null)
    beat = a.get("beat_age_secs")
    if isinstance(beat, (int, float)) and not isinstance(beat, bool):
        upd = now - max(0.0, float(beat)) if upd is None else upd
    alive = _first(a, "alive", "running")
    stood = _first(a, "stood_down", "stood-down")
    model = _scalar(_first(a, "model"))
    # Older payloads carry no runtime_model at all: `_scalar` keeps a non-scalar out of the page, and the
    # row then falls back to the configured model with state "configured" (never a fabricated "running").
    rt_model = _scalar(a.get("runtime_model"))
    rt_at = _ts(a.get("runtime_model_at"))
    if rt_at is not None and not math.isfinite(rt_at):
        rt_at = None
    ctx = _first(a, "context", "context_pct", "ctx")
    tokens = context_count(a.get("context_tokens"))
    window = context_count(a.get("context_window"), 1)
    ceiling = context_count(a.get("context_ceiling"), 1)
    floor = context_count(a.get("context_floor"), 1)
    sampled = _ts(a.get("context_sampled_at"))
    if sampled is not None and not math.isfinite(sampled):
        sampled = None
    sample_age = now - sampled if sampled is not None else None
    stale = sample_age is not None and (sample_age > FLEET_STALE_S or sample_age < -30)
    valid = tokens is not None and window is not None and sample_age is not None
    pct = round(tokens * 100 / window, 1) if tokens is not None and window else context_percent(ctx)
    row = {
        "name": _scalar(_first(a, "name", "agent", "id") or name),
        "agent_type": a.get("agent_type", "standing") if a.get("agent_type", "standing") in ("standing", "tfa") else None,
        "model": model,
        "model_label": model_label(model),
        # What the agent is RUNNING, when the producer observed it; else the configured model, labelled
        # `configured` by model_state so the page can never present a mode-resolved default as running.
        "runtime_model": rt_model,
        "runtime_model_label": model_label(rt_model),
        "runtime_model_at": rt_at,
        "runtime_model_age": _round(max(0.0, now - rt_at)) if rt_at is not None else None,
        "model_state": model_state(rt_model, rt_at, model, now),   # file-level staleness applied below
        "role": _scalar(_first(a, "role")),
        "effort": _scalar(_first(a, "effort")),
        "ctx_tokens": tokens,
        "context_tokens": tokens,
        "context_window": window,
        "context_ceiling": ceiling,
        "context_floor": floor if floor is not None and (ceiling is None or floor <= ceiling) else None,
        "context_pct": pct,
        "context_sampled_at": sampled,
        "context_age": _round(max(0, sample_age)) if sample_age is not None else None,
        "context_state": "stale" if stale else "ok" if valid else "unknown",
        "runtime": _scalar(a.get("runtime")),
        "ledger": ledger_excerpt(_first(a, "status", "state")),
        # "now" = the agent's own one-line NOW.md (written for humans); "state" = a line scraped from STATE.md
        "ledger_source": a.get("status_source") if a.get("status_source") in ("now", "state") else None,
        "ledger_age": _ledger_age(a.get("cwd"), now),
        "alive": bool(alive) if alive is not None else None,
        "stood_down": bool(stood) if stood is not None else None,
        "status": _scalar(_first(a, "status", "state")),
        "context": _scalar(ctx),
        "update_age": _round(max(0.0, now - upd)) if upd is not None else None,
    }
    # Additive source metadata: old status producers keep their existing API shape.
    for field in ("pool_bot", "age"):
        if field in a:
            row[field] = _scalar(a[field])
    if a.get("runtime_kind") in ("tmux", "container"):
        row["runtime_kind"] = a["runtime_kind"]
    return row


def _read_fleet(status_path, md_path, now):
    out = {"mode": None}
    try:
        with open(md_path, encoding="utf-8", errors="replace") as f:
            out["mode"] = fleet_mode(f.read())
        if out["mode"] is None:
            out["mode_error"] = "no FLEET_MODE line"
    except OSError as e:
        out["mode_error"] = "FLEET.md unreadable: " + (e.strerror or type(e).__name__)
    try:
        mtime = os.stat(status_path).st_mtime
        with open(status_path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        out["error"] = "fleet-status.json unreadable: " + (e.strerror or type(e).__name__)
        return out
    except ValueError:
        out["error"] = "fleet-status.json is not valid JSON"
        return out
    if not isinstance(data, dict):
        out["error"] = "fleet-status.json is not a JSON object"
        return out
    file_age = max(0.0, now - mtime)
    gen = _ts(_first(data, "generated_at", "generated", "ts", "updated"))
    agents = data.get("agents")
    if isinstance(agents, dict):
        pairs = list(agents.items())
    elif isinstance(agents, list):
        pairs = [(None, a) for a in agents]
    else:
        pairs = []
    # The producer merges standing registry rows and pooled TFA metadata. A lease
    # can repeat an owner; standing identity wins a collision regardless of order.
    rows = {}
    for n, a in pairs:
        row = _agent_row(n, a, now)
        name = row["name"]
        if name is None:
            continue
        if name not in rows or (rows[name]["agent_type"] == "tfa" and row["agent_type"] == "standing"):
            rows[name] = row
    out.update({
        "file_age": _round(file_age),
        "generated_age": _round(max(0.0, now - gen)) if gen is not None else _round(file_age),
        "stale": file_age > FLEET_STALE_S or (gen is not None and now - gen > FLEET_STALE_S),
        "ceiling": None,  # compatibility keys; thresholds belong to individual agents
        "soft": None,
        "agents": list(rows.values())[:FLEET_MAX_AGENTS],
    })
    flags, out["supervisor"], live = supervisor_flags(
        os.environ.get("SUPERVISOR_STATUS_PATH") or SUPERVISOR_STATUS_DEFAULT, now)
    for a in out["agents"]:
        if out["stale"]:
            a["context_state"] = "stale"
            # A stale fleet-status.json cannot vouch for a model either: a runtime reading from a file
            # nobody has rewritten in FLEET_STALE_S is history, not the current model.
            if a["model_state"] == "running":
                a["model_state"] = "stale"
        a["flags"] = flags.get(a["name"], [])
        busy, since = live.get(a["name"], (None, None))
        a["busy"] = busy                                   # Tower: null = no pane verdict (no tmux pane / unknown)
        a["busy_age"] = _round(max(0.0, now - since)) if busy and since is not None else None
        la = a["ledger_age"]
        a["silent"] = bool(a["busy_age"] is not None and a["busy_age"] > SILENT_S
                           and (la is None or la > SILENT_S))
        if out["stale"] and a["agent_type"] == "tfa":
            a.update(alive=None, busy=None, busy_age=None, silent=False, flags=[])
    return out


def supervisor_flags(path, now):
    """Supervisor status.json -> ({agent name: [{"t": text, "sev": "bad"|"warn"}]}, meta, live)
    where live = {agent name: (busy bool|None, busy_since epoch|None)} (Tower).
    Fail-soft like the rest of the fleet view: a missing file is normal (not deployed / not this site) and
    yields ({}, None, {}); unreadable or garbage yields ({}, {"error": ...}, {}). Flags and live are only built
    from a fresh file, so a dead supervisor can't leave old alarms or busy dots on screen (meta says stale)."""
    try:
        mtime = os.stat(path).st_mtime
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, None, {}
    except (OSError, ValueError) as e:
        return {}, {"error": "supervisor status unreadable: " + type(e).__name__}, {}
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, dict):
        return {}, {"error": "supervisor status has no sessions"}, {}
    gen = _ts(data.get("generated_at"))
    age = max(0.0, now - (gen if gen is not None else mtime))
    meta = {"age": _round(age), "stale": age > SUPERVISOR_STALE_S, "tmux_server_up": data.get("tmux_server_up")}
    if meta["stale"]:
        return {}, meta, {}
    out, live = {}, {}
    for name, st in list(sessions.items())[:FLEET_MAX_AGENTS]:
        if not isinstance(st, dict) or st.get("stood_down") is True:
            continue
        b = st.get("busy")
        if isinstance(b, bool) and st.get("alive") is True:
            live[str(name)] = (b, _ts(st.get("busy_since")) if b else None)
        flags = []
        n = st.get("attempts")
        if st.get("escalated") is True:
            flags.append({"t": "supervisor gave up resurrecting", "sev": "bad"})
        elif st.get("alive") is False and isinstance(n, int) and not isinstance(n, bool) and n > 0:
            flags.append({"t": "resurrecting, attempt %d" % n, "sev": "warn"})
        if st.get("prompt_dialog") is True:
            flags.append({"t": "stuck on a dialog", "sev": "bad"})
        if st.get("probe_failed") is True:
            flags.append({"t": "health probe unanswered", "sev": "warn"})
        since = _ts(st.get("automode_stop_since"))
        if since is not None:
            flags.append({"t": "auto-mode stopped %dm ago" % (max(0.0, now - since) // 60), "sev": "warn"})
        if st.get("beat_paged") is True:
            flags.append({"t": "heartbeat stale", "sev": "warn"})
        if flags:
            out[str(name)] = flags
    return out, meta, live


_fleet_gen_lock = threading.Lock()
# "thread" holds the last dispatched generator thread (tests join it); nothing joins it in production,
# it's fire-and-forget.
_fleet_gen_state = {"at": 0.0, "running": False, "thread": None}


def _run_fleet_gen(gen, status_path):
    """Runs on its own daemon thread, OUTSIDE any lock an HTTP request could be waiting on. Never raises:
    a missing/non-executable path, a non-zero exit and a hang (killed at FLEET_GEN_TIMEOUT_S) are all
    logged and swallowed, same fail-soft contract as the read side.

    status_path (FLEET_STATUS_PATH) is passed as argv[1]: the contract documented at FLEET_STATUS_GEN
    above, so the generator writes where the dash actually reads from, not wherever its own default is."""
    try:
        r = subprocess.run([gen, status_path], timeout=FLEET_GEN_TIMEOUT_S, capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("fleet status generator %s exited %d: %s", gen, r.returncode,
                        (r.stderr or "").strip()[:500])
    except subprocess.TimeoutExpired:
        log.warning("fleet status generator %s timed out after %ds", gen, FLEET_GEN_TIMEOUT_S)
    except OSError as e:
        log.warning("fleet status generator %s failed to start: %s", gen, e)
    except Exception as e:   # belt-and-braces: a generator-dispatch bug must never affect anything else
        log.exception("fleet status generator %s: unexpected error: %s", gen, e)
    finally:
        with _fleet_gen_lock:
            _fleet_gen_state["running"] = False


def _maybe_run_fleet_gen(now, status_path):
    """Kicks off FLEET_STATUS_GEN on a background thread, at most once per FLEET_GEN_PERIOD_S and never
    more than one in flight. Unset FLEET_STATUS_GEN = feature off (a no-op, same as before this existed).
    Also a no-op when status_path (FLEET_STATUS_PATH) is unset: there's nowhere to tell the generator to
    write, and running it against its own default would write somewhere the dash never reads.
    Returns immediately either way: the generator subprocess (with its own timeout) never runs on the
    calling (HTTP request) thread."""
    gen = os.environ.get("FLEET_STATUS_GEN") or FLEET_STATUS_GEN_DEFAULT
    if not gen or not status_path:
        return
    with _fleet_gen_lock:
        if _fleet_gen_state["running"] or now - _fleet_gen_state["at"] < FLEET_GEN_PERIOD_S:
            return
        _fleet_gen_state["running"] = True
        _fleet_gen_state["at"] = now
        t = threading.Thread(target=_run_fleet_gen, args=(gen, status_path), name="r3-fleet-gen", daemon=True)
        _fleet_gen_state["thread"] = t
    t.start()


def fleet_snapshot(now=None):
    """Cached, fail-soft fleet view for /state. Paths are env-configurable (FLEET_STATUS_PATH,
    FLEET_MD_PATH); the cache is keyed on them so a path change takes effect immediately.
    Also the trigger point for FLEET_STATUS_GEN: every call (cache hit or miss) tries to dispatch a
    background refresh (passing FLEET_STATUS_PATH as the generator's argv[1]), so the file stays warm as
    long as something is polling /state, with no new timer or cron owning it."""
    now = time.time() if now is None else now
    status_path = os.environ.get("FLEET_STATUS_PATH") or FLEET_STATUS_DEFAULT
    try:
        _maybe_run_fleet_gen(now, status_path)
    except Exception as e:   # dispatching the generator must never block/break the read side
        log.exception("fleet status generator dispatch failed: %s", e)
    key = (status_path, os.environ.get("FLEET_MD_PATH") or FLEET_MD_DEFAULT,
           os.environ.get("FLEET_CONTAINER_SOURCE", ""))
    with _fleet_lock:
        c = _fleet_cache
        if c["key"] == key and c["val"] is not None and 0 <= now - c["at"] < FLEET_CACHE_S:
            return c["val"]
    try:
        val = _read_fleet(key[0], key[1], now)
    except Exception as e:   # belt-and-braces: the fleet view must never take /state down
        val = {"mode": None, "error": "fleet read failed: " + type(e).__name__}
    val = _merge_containers(val, key[2], now)
    with _fleet_lock:
        _fleet_cache.update(key=key, at=now, val=val)
    return val


# ---------------------------------------------------------------- snapshot


HISTORY_PAGE = 25


def hist_param(path):
    """`?hist=N` from a /state request path -> how many history rows to send, clamped to
    [HISTORY_PAGE, stream.HISTORY_MAX]; absent/garbage -> HISTORY_PAGE."""
    m = re.search(r"[?&]hist=(\d{1,4})(?:&|$)", path or "")
    return min(max(int(m.group(1)), HISTORY_PAGE), stream.HISTORY_MAX) if m else HISTORY_PAGE


def snapshot(broker, cap=None, hist=HISTORY_PAGE):
    """Build the JSON-safe state view. Pure read: takes each lock briefly and NEVER holds two at once
    (coalescer.lock and adapter._lock are acquired in separate blocks; holding both would deadlock
    against any path that takes them in the other order)."""
    now = time.time()

    with broker.coalescer.lock:
        live = [_live_view(key, w, now) for key, w in broker.coalescer.live.items()]
        pending = [_pending_view(key, p, now) for key, p in broker.coalescer.pending.items()]

    workers = [_worker_view(key, wk, now) for key, wk in _worker_items(broker)]
    waiting = _waiting_items(broker)
    for row in live:
        if row["key"] in waiting:
            row["waiting"] = waiting[row["key"]]

    stamps = dict(getattr(broker, "route_stamps", None) or {})
    for row in live:
        row["route"] = _scalar(stamps.get(row["key"])) or None

    hist_all = stream.history() if stream.enabled() else []
    return {
        "ok": True,
        "now": now,
        "stream_enabled": stream.enabled(),      # page shows per-row stream links only when on
        "concurrency": {"cap": cap, "in_use": len(workers)},
        "live": sorted(live, key=lambda r: r["key"]),
        "pending": sorted(pending, key=lambda r: r["key"]),
        "workers": sorted(workers, key=lambda r: r["key"]),
        # Previous (and current) stream sessions, newest first: key, 8-char prefix and start ts only.
        # Capped server-side (newest `hist`, default 25) so the 5s poll stays light; the page asks for
        # more via /state?hist=N. history_total = how many exist.
        "history": hist_all[:max(1, int(hist))],
        "history_total": len(hist_all),
        "counters": {
            "deduped": getattr(broker.coalescer, "deduped", 0),
            "dedup_cleared": getattr(broker.coalescer, "dedup_cleared", 0),
            "echo_dropped": getattr(broker.echo_guard, "dropped", 0),
            "allowlist_rejected": getattr(broker, "rejected", 0),
        },
        "fleet": fleet_snapshot(now),   # fail-soft, cached
        "drain": _drain_view(broker),
        # Page labels from site config: the instance name beside the logo and the ticket link template
        # (TICKET_URL_TEMPLATE, `{}` = ticket id; the page links only http(s) templates).
        "instance": os.getenv("DASH_INSTANCE_NAME", "")[:60],
        "ticket_url": os.getenv("TICKET_URL_TEMPLATE", "")[:300],
        "queue": _queue_view(broker, now),
        "next_subject": getattr(getattr(broker, "semaphore", None), "front", None),
    }


QUEUE_SCAN = 500   # intake rows read per poll; the page lists distinct subjects from these, oldest first


def _drain_view(broker):
    """broker.drain_status() (the /health `drain` block), or None when the broker has none or it fails.
    It takes coalescer.lock then the journal lock, the same order as ingest, and snapshot holds neither here."""
    try:
        return broker.drain_status()
    except Exception:
        return None


def _queue_view(broker, now):
    """Durable intake waiting to dispatch, one row per subject in arrival order:
    {key, kind, events, arrived_age}. [] without a journal. Read-only and fail-soft."""
    st = getattr(broker, "state", None)
    if st is None or not hasattr(st, "queued"):
        return []
    try:
        rows = st.queued(QUEUE_SCAN)
    except Exception:
        return []
    out = {}
    for _id, kind, payload, arrived in rows:
        try:
            if kind == "rerun":
                key = tickets.vikunja_ref(payload.get("task_id")) or "vikunja:task:?"
            else:
                key = coalescer.normalize(payload.get("event") or {})
        except Exception:
            continue
        row = out.get(key)
        if row is None:
            out[key] = {"key": key, "kind": kind, "events": 1,
                        "title": _event_title([payload.get("event") or {}]),
                        "arrived_age": _round(max(0.0, now - arrived)) if isinstance(arrived, (int, float)) else None}
        else:
            row["events"] += 1
    return list(out.values())


def _event_title(events):
    """Use titles already carried by intake; never fetch ticket data during a state poll."""
    for event in reversed(events):
        if isinstance(event, dict) and isinstance(event.get("title"), str) and event["title"].strip():
            return event["title"][:300]
    return None


def _live_view(key, w, now):
    return {"key": key, "state": w.get("state"), "forwards": w.get("forwards", 0),
            "title": _event_title(w.get("events") or []),
            "arrived_age": _round(max(0.0, now - w["admitted_at"])) if w.get("admitted_at") is not None else None,
            "events": len(w.get("events") or []), "buffered": len(w.get("fwd_buffer") or []),
            "expire_in": _round(max(0.0, (w.get("expire") or now) - now))}


def _pending_view(key, p, now):
    return {"key": key, "events": len(p.get("events") or []),
            "title": _event_title(p.get("events") or []),
            "arrived_age": _round(max(0.0, now - p["armed_at"])) if p.get("armed_at") is not None else None,
            "spawn_in": _round(max(0.0, (p.get("spawn_at") or now) - now))}


def _worker_items(broker):
    adapter = getattr(broker, "adapter", None)
    lock = getattr(adapter, "_lock", None)
    if lock is None:
        return []
    with lock:
        return list(adapter._workers.items())


def _worker_view(key, wk, now):
    ts = getattr(wk, "turn_started", None)
    sid = getattr(wk, "session_id", None) or ""
    # The model THIS worker was launched with (adapters record it at the exec point, see
    # cc.record_launch), plus the backend/harness it resolved to. Absent on a worker whose harness never
    # recorded one (or a payload from before this field existed): the row then reads "unknown", never a
    # configured default dressed up as the running model. No staleness window here: a row only exists
    # while its worker is live, and a turn's model is the session's model until it is re-launched.
    model = _scalar(getattr(wk, "model", None))
    backend = _scalar(getattr(wk, "backend", None))
    harness = _scalar(getattr(wk, "harness", None))
    at = getattr(wk, "model_at", None)
    at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) else None
    return {"key": key, "run_id": getattr(wk, "run_id", None), "session": sid[:8],
            "turns": getattr(wk, "turns", 0), "tokens": getattr(wk, "tokens", 0),
            "items": getattr(wk, "items", None), "last_activity": getattr(wk, "last_activity", None),
            "forwards": getattr(wk, "forwards", 0), "in_turn": bool(getattr(wk, "in_turn", False)),
            "done": bool(getattr(wk, "done", False)), "turn_age": _round(now - ts) if ts else None,
            "model": model, "model_label": model_label(model),
            "backend": backend or None, "harness": harness or None,
            "model_at": at, "model_age": _round(max(0.0, now - at)) if at is not None else None,
            "model_state": "running" if model else "unknown",
            "title": _scalar(getattr(wk, "title", None)) or None, "route": _scalar(getattr(wk, "route", None)) or None}


def _waiting_items(broker):
    adapter = getattr(broker, "adapter", None)
    lock = getattr(adapter, "_lock", None)
    if lock is None:
        return {}
    with lock:
        return dict(getattr(adapter, "_waiting", {}))


# What GET /runs/<subject_key> answers: the coalescer's view of one subject, newest phase first.
_RUN_STATUS = {"open_running": "running", "open_idle": "idle", "closing": "closing", "closed": "closing"}
_RUN_KEY_RE = re.compile(r"[\x21-\x7e]{1,512}")   # printable ASCII, no spaces; subject keys are never longer


def run_status(broker, key):
    """(http code, body) for GET /runs/<key>. Pure read, same lock discipline as snapshot (one lock at a time).
    status: pending (collecting a burst), waiting (repo lock / turn cap), running / idle / closing (live), completed (finished cleanly
    and not re-armed since), else 404 unknown. completed_at is kept for the last MAX_COMPLETED subjects only."""
    if not isinstance(key, str) or not _RUN_KEY_RE.fullmatch(key):
        return 400, {"ok": False, "error": "bad subject key"}
    now = time.time()
    c = broker.coalescer
    with c.lock:
        w, p, done = c.live.get(key), c.pending.get(key), c.completed.get(key)
        live = _live_view(key, w, now) if w else None
        pend = _pending_view(key, p, now) if p else None
    worker = next((_worker_view(k, wk, now) for k, wk in _worker_items(broker) if k == key), None)
    if live:
        status = _RUN_STATUS.get(live["state"], live["state"])
        reason = _waiting_items(broker).get(key)
        if reason and live["state"] in ("open_running", "open_idle"):
            live["waiting"], status = reason, "waiting"
    elif pend:
        status = "pending"
    elif done:
        status = "completed"
    else:
        return 404, {"ok": False, "subject_key": key, "status": "unknown"}
    return 200, {"ok": True, "subject_key": key, "status": status, "row": (getattr(broker, "route_stamps", None) or {}).get(key),
                 "live": live, "pending": pend, "worker": worker, "completed_at": done}


def _round(v):
    return None if v is None else round(float(v), 1)


# ---------------------------------------------------------------- page

# Self-contained: inline CSS + JS, no build step, no CDN. Auto-refreshes /state every 5s.
# Limitation: a plain fetch-and-repaint loop, not a websocket. At 5s on a table this size the
# difference is invisible, and a socket is another thing to keep alive across broker restarts.
#
# Human-first layout: session list (sidebar) plus session detail; the SSE
# transcript renders as a chat/timeline. Assistant text is markdown (escape-FIRST renderer: every byte of
# transcript content goes through esc() before any tag is added, links are http(s)-only), tool calls as
# collapsed cards (name + short args summary) with their result folded in, errors highlighted. The raw
# jsonl stays one click away (Raw toggle). Light/dark/auto theme. Server side (auth, /state, /stream)
# is untouched.
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>eph-broker</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='9' fill='%232b57c9'/%3E%3Crect x='7' y='9.5' width='18' height='4.5' rx='2.2' fill='%23fff'/%3E%3Crect x='7' y='18' width='11.5' height='4.5' rx='2.2' fill='%23fff'/%3E%3C/svg%3E">
<style>
  /* Tokens: one brand colour, the rest semantic. Every text pairing clears WCAG AA (4.5:1) in both themes.
     run/wait/block are always paired with an icon shape and a word, never colour alone. */
  :root{
    color-scheme:light;
    --bg:#f4f5f7; --s1:#ffffff; --s2:#f0f2f5; --s3:#e6e9ee; --line:#dce0e6;
    --t1:#141920; --t2:#475160; --t3:#626c7b; --brand:#2b57c9; --onbrand:#ffffff;
    --run:#17734a; --wait:#8a5600; --block:#bb3328; --shadow:0 1px 2px rgba(20,25,32,.05);
  }
  :root[data-theme=dark]{
    color-scheme:dark;
    --bg:#0f1216; --s1:#161a20; --s2:#1d222a; --s3:#262c35; --line:#2b323c;
    --t1:#e8ecf1; --t2:#aab3bf; --t3:#8a94a2; --brand:#8eabff; --onbrand:#0c1120;
    --run:#4cc38a; --wait:#e3a63d; --block:#f27c71; --shadow:none;
  }
  @media (prefers-color-scheme:dark){
    :root:not([data-theme=light]){
      color-scheme:dark;
      --bg:#0f1216; --s1:#161a20; --s2:#1d222a; --s3:#262c35; --line:#2b323c;
      --t1:#e8ecf1; --t2:#aab3bf; --t3:#8a94a2; --brand:#8eabff; --onbrand:#0c1120;
      --run:#4cc38a; --wait:#e3a63d; --block:#f27c71; --shadow:none;
    }
  }
  /* The transcript styles below predate the tokens and use these names */
  :root{
    --panel:var(--s1); --panel2:var(--s2); --ink:var(--t1); --muted:var(--t2); --faint:var(--t3);
    --accent:var(--brand); --green:var(--run); --amber:var(--wait); --red:var(--block);
    --user:color-mix(in srgb,var(--brand) 7%,var(--s1)); --code:var(--s2);
    --r:12px; --btn:120px;
  }
  *{box-sizing:border-box}
  [hidden]{display:none !important}
  /* All views stay in document flow: wrapped titles, tall sidebars and short windows scroll. */
  html{overflow-y:auto}
  body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
       background:var(--bg);color:var(--t1);-webkit-font-smoothing:antialiased}
  code,pre,.mono{font-family:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace}
  button{font:inherit;color:var(--t1);appearance:none;background:var(--s1);border:1px solid var(--line);
         border-radius:8px;padding:6px 12px;cursor:pointer}
  button:hover{background:var(--s2)}
  button:active{background:var(--s3)}
  button:disabled{opacity:.5;cursor:default}
  a{color:inherit}
  :focus-visible{outline:2px solid var(--brand);outline-offset:2px}
  [tabindex="-1"]:focus-visible{outline:none}   /* focused by script for screen readers, not by the keyboard */

  /* ---- header: brand, view tabs, paused chip, theme */
  .top{position:sticky;top:0;z-index:5;background:var(--s1);border-bottom:1px solid var(--line)}
  .top-in{max-width:1440px;margin:0 auto;padding:0 40px;height:64px;display:flex;align-items:center;gap:28px}
  .brand{display:flex;align-items:center;gap:10px;font-weight:650;font-size:16px;letter-spacing:-.01em;white-space:nowrap}
  .logo{width:24px;height:24px;border-radius:7px;background:var(--brand);position:relative;flex:none}
  .logo::before,.logo::after{content:"";position:absolute;left:22%;height:14%;border-radius:2px;background:var(--onbrand)}
  .logo::before{top:30%;width:56%} .logo::after{top:56%;width:36%}
  .inst{font-weight:400;font-size:13px;color:var(--t2)}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--run)}
  .dot.bad{background:var(--block)}
  .nav{display:flex;align-self:stretch;gap:4px}
  .nav a{display:flex;align-items:center;padding:0 14px;text-decoration:none;color:var(--t2);font-size:14px;border-bottom:3px solid transparent;margin-bottom:-1px}
  .nav a:hover{color:var(--t1)}
  .nav a[aria-current=page]{color:var(--t1);font-weight:600;border-bottom-color:var(--brand)}
  .top-r{margin-left:auto;display:flex;align-items:center;gap:12px}
  .chip{text-decoration:none;display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:999px;font-size:13px;font-weight:600;white-space:nowrap}
  .chip.wait{color:var(--wait);background:color-mix(in srgb,var(--wait) 13%,var(--s1))}
  .chip.mode{color:var(--t2);background:var(--s3);font-weight:600}
  .chip.pause::before{content:"";width:8px;height:10px;border-left:3px solid currentColor;border-right:3px solid currentColor;box-sizing:border-box}

  /* ---- buttons: row actions share ONE width so a column of them lines up */
  .btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-width:var(--btn);height:36px;padding:0 16px;
       border-radius:8px;border:1px solid var(--line);background:var(--s1);color:var(--t1);font-weight:600;font-size:14px;
       text-decoration:none;cursor:pointer;white-space:nowrap}
  .btn:hover{border-color:color-mix(in srgb,var(--t1) 28%,var(--line))}
  .btn:active{background:var(--s3);transform:translateY(1px)}
  .btn.pri{background:var(--brand);border-color:var(--brand);color:var(--onbrand)}
  .btn.pri:hover{background:color-mix(in srgb,var(--brand) 85%,var(--t1))}
  .btn.pri:active{background:color-mix(in srgb,var(--brand) 70%,var(--t1))}
  .btn.ghost{background:transparent;border-color:transparent}
  .btn.ghost:hover{background:var(--s2);border-color:var(--line)}
  .btn.ghost:active{background:var(--s3)}
  .btn.sq{min-width:36px;width:36px;padding:0}
  .btn.on{border-color:var(--brand);color:var(--brand)}
  .btn.bad{color:var(--red)} .btn.bad:hover{border-color:var(--red)}
  .btn:disabled{opacity:.5;cursor:default}
  .sw{position:relative;flex:none;width:44px;height:26px;border-radius:13px;border:0;background:var(--s3);cursor:pointer;transition:background .2s}
  .sw::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:var(--shadow);
             transition:transform .25s cubic-bezier(.3,1.4,.5,1)}
  .sw[aria-checked=true]{background:var(--wait)} .sw[aria-checked=true]::after{transform:translateX(18px)}
  .msg{display:flex;gap:8px;align-items:flex-end;padding:12px 16px;border-top:1px solid var(--line)}
  .msg textarea{flex:1;min-height:36px;max-height:160px;resize:vertical;border:1px solid var(--line);border-radius:8px;background:var(--s1);
                color:var(--t1);padding:8px 12px;font:inherit;font-size:14px}
  .pick-sel{height:36px;border:1px solid var(--line);border-radius:8px;background:var(--s1);color:var(--t1);font:inherit;
            font-size:14px;padding:0 8px;max-width:100%}
  .pick-sel.num{width:84px}
  .pick-sel.set{border-color:var(--brand)}
  .control-label{display:block;font-size:12px;color:var(--t2);margin-bottom:4px}
  .rerun{flex-basis:100%;min-width:0}
  .rerun-controls{display:flex;align-items:flex-end;gap:8px}
  .rerun-controls label{flex:1 1 auto;min-width:0}
  .rerun-controls select{width:100%;min-width:0;text-overflow:ellipsis}
  .rerun-controls .btn{flex:none}
  .rerun .sub{margin-top:6px}
  .run-search{margin-left:auto}
  .msg label{flex:1;min-width:0}
  .msg textarea{width:100%}
  .toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:50;max-width:min(520px,90vw);padding:10px 16px;border-radius:10px;
         background:var(--t1);color:var(--s1);font-size:14px;font-weight:600;box-shadow:var(--shadow)}
  .toast.bad{background:var(--red);color:#fff}
  @media (prefers-reduced-motion:reduce){.sw,.sw::after{transition:none}}
  .lnk{color:var(--brand);font-weight:600;text-decoration:none;background:none;border:0;padding:0;cursor:pointer;font-size:14px}
  .lnk:hover{text-decoration:underline}

  /* ---- status icons: shape + colour, always next to a word */
  .st{width:16px;height:16px;flex:none;display:inline-grid;place-items:center;border-radius:50%;font:700 10px/1 system-ui,sans-serif}
  .st.run::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--run)}
  .st.wait::before{content:"";width:9px;height:9px;border-radius:50%;border:2px solid var(--wait)}
  .st.idle::before{content:"";width:9px;height:9px;border-radius:50%;border:1.5px solid var(--t3)}
  .st.block{border-radius:4px;background:var(--block);color:var(--s1)} .st.block::before{content:"!"}
  .st.ask{background:var(--wait);color:var(--s1)} .st.ask::before{content:"?"}
  .st.ok{background:color-mix(in srgb,var(--run) 16%,var(--s1));color:var(--run)} .st.ok::before{content:"✓"}
  .st.fail{background:color-mix(in srgb,var(--block) 16%,var(--s1));color:var(--block)} .st.fail::before{content:"✕"}
  .st.off{background:var(--s3);color:var(--t2)} .st.off::before{content:"–"}

  /* ---- page */
  .page{max-width:1440px;margin:0 auto;padding:32px 40px 64px}
  .ph{display:flex;align-items:baseline;gap:12px;margin:0 0 20px}
  .ph h1{margin:0;font-size:28px;line-height:1.2;font-weight:650;letter-spacing:-.02em;min-width:0;overflow-wrap:anywhere}
  .upd{margin-left:auto;display:inline-flex;align-items:center;gap:8px;font-size:13px;color:var(--t2);white-space:nowrap}
  .upd::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--run)}
  .upd.bad::before{background:var(--block)}
  .crumb{font-size:13px;color:var(--t2);margin-bottom:4px} .crumb a{color:var(--brand);font-weight:600;text-decoration:none}
  .card{background:var(--s1);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);min-width:0}
  .sec{margin-top:32px;min-width:0}
  .sec h2{display:flex;align-items:center;gap:8px;margin:0 0 12px;font-size:17px;font-weight:650;letter-spacing:-.01em}
  .sec h2 + .sub{margin:-8px 0 12px}
  .sub{font-size:13px;color:var(--t2)}
  .cnt{font-size:12px;font-weight:600;color:var(--t2);background:var(--s3);border-radius:999px;padding:1px 8px;font-variant-numeric:tabular-nums}
  .cols{display:grid;grid-template-columns:minmax(0,1fr) 440px;gap:0 20px;align-items:start}
  .empty{padding:18px 24px;color:var(--t2);font-size:13px}
  .err-banner{max-width:1360px;margin:16px auto 0;padding:10px 14px;border-radius:10px;font-size:13px;
              border:1px solid color-mix(in srgb,var(--block) 50%,var(--line));color:var(--block);
              background:color-mix(in srgb,var(--block) 8%,var(--s1))}

  /* ---- summary strip */
  .strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));padding:20px 0}
  .strip > div{padding:0 24px;border-left:1px solid var(--line);min-width:0}
  .strip > div:first-child{border-left:0}
  .strip .k{font-size:13px;color:var(--t2)}
  .strip .v{display:flex;align-items:baseline;gap:8px;margin-top:2px;white-space:nowrap}
  .strip .v b{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.2}
  .strip .v span{font-size:13px;color:var(--t2);overflow:hidden;text-overflow:ellipsis}
  .pips{display:inline-flex;gap:4px;margin-left:auto;align-self:center}
  .pips i{width:16px;height:6px;border-radius:3px;background:var(--s3)} .pips i.on{background:var(--run)}

  /* ---- row lists (needs you, attention, recent) */
  .rows > *{display:grid;grid-template-columns:auto minmax(0,1fr) auto auto;gap:4px 16px;align-items:center;padding:16px 20px 16px 22px;
          border-top:1px solid var(--line);text-decoration:none;color:inherit;background:none;border-left:0;border-right:0;border-bottom:0;width:100%;text-align:left}
  .rows > *:first-child{border-top:0}
  .rows .st{align-self:start;margin-top:2px}
  .rows .h{font-size:14px;overflow:hidden;text-overflow:ellipsis} .rows .h b{font-weight:600}
  .rows .s{font-size:13px;color:var(--t2);overflow-wrap:anywhere}
  .rows .when{font-size:13px;color:var(--t2);white-space:nowrap;font-variant-numeric:tabular-nums}
  .rows .acts{display:flex;gap:8px}
  .rows button,.rows a.row{cursor:pointer} .rows button:hover,.rows a.row:hover{background:var(--s2)}
  .ref{color:var(--t2);font-variant-numeric:tabular-nums;margin-right:6px;white-space:nowrap}

  /* ---- tables (become stacked rows on a phone) */
  .tbl{width:100%;border-collapse:collapse;font-size:14px}
  .tbl th{text-align:left;font-size:12px;font-weight:500;color:var(--t2);padding:14px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
  .tbl td{padding:14px 12px;border-top:1px solid var(--line);vertical-align:middle;min-width:0}
  .tbl tbody tr:first-child td{border-top:0}
  .tbl th:first-child,.tbl td:first-child{padding-left:20px} .tbl th:last-child,.tbl td:last-child{padding-right:20px}
  .tbl .num{font-variant-numeric:tabular-nums;white-space:nowrap}
  .tbl .r{text-align:right}
  .tbl .t{font-weight:600;overflow:hidden;text-overflow:ellipsis}
  .tbl .s2{font-size:13px;color:var(--t2)}
  .tbl tr.pick{cursor:pointer} .tbl tr.pick:hover td{background:var(--s2)}
  .tbl tr.sel td{background:color-mix(in srgb,var(--brand) 7%,var(--s1))}
  .tbl tr.sel td:first-child{box-shadow:inset 3px 0 0 var(--brand)}
  .runc{display:flex;gap:12px;align-items:flex-start;min-width:0} .runc .st{margin-top:3px} .runc > div{min-width:0}
  .stw{display:inline-flex;align-items:center;gap:8px;white-space:nowrap}
  .nw{white-space:nowrap}
  .tfoot{display:flex;align-items:center;gap:12px;padding:14px 20px;border-top:1px solid var(--line);font-size:13px;color:var(--t2)}
  .tfoot .lnk{margin-left:auto}
  .tag{display:inline-block;font-size:12px;font-weight:600;color:var(--t2);background:var(--s3);border-radius:999px;padding:1px 8px;white-space:nowrap}

  /* ---- queue panel + segmented control */
  .qhead{display:flex;align-items:flex-start;gap:16px;padding:20px 24px;border-bottom:1px solid var(--line)}
  .qhead b{display:block;font-weight:600}
  .qstate{margin-left:auto}
  .qrow{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;align-items:center;padding:12px 24px;font-size:14px}
  .qrow + .qrow{border-top:1px solid var(--line)}
  .qmore{padding:12px 24px 16px}
  .seg{display:inline-flex;background:var(--s3);border-radius:9px;padding:3px;gap:2px}
  .seg button{border:0;background:none;border-radius:7px;padding:5px 12px;font-size:13px;font-weight:600;color:var(--t2);cursor:pointer;white-space:nowrap}
  .seg button[aria-pressed=true]{background:var(--s1);color:var(--t1);box-shadow:var(--shadow)}
  .seg button:hover,.lnk:hover{background:var(--s2)}
  .seg button:active,.lnk:active{background:var(--s3)}
  .subtabs{display:flex;gap:20px;border-bottom:1px solid var(--line);margin-top:24px;overflow-x:auto}
  .subtabs a{padding:10px 0;border-bottom:3px solid transparent;text-decoration:none;white-space:nowrap;color:var(--t2)}
  .subtabs a:hover{color:var(--t1);background:var(--s2)}
  .subtabs a:active{background:var(--s3)}
  .subtabs a[aria-selected=true]{border-bottom-color:var(--brand);color:var(--t1);font-weight:600}
  .queue-preview{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}
  .sw:hover,.sw:active{box-shadow:0 0 0 2px var(--brand)}
  .bar{display:flex;align-items:center;gap:16px;margin-bottom:16px;flex-wrap:wrap}
  .search{margin-left:auto;width:280px;max-width:100%;height:36px;border:1px solid var(--line);border-radius:8px;background:var(--s1);
          color:var(--t1);padding:0 12px;font:inherit}
  .kv{display:grid;grid-template-columns:auto minmax(0,1fr);font-size:14px}
  .kv > *{padding:10px 0;border-top:1px solid var(--line)}
  .kv dt{color:var(--t2)} .kv dd{margin:0;text-align:right;font-weight:600;overflow-wrap:anywhere}
  .side{padding:20px 24px 24px}
  .side h3{margin:0 0 4px;font-size:18px;font-weight:650;letter-spacing:-.01em}
  .side .acts{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap}
  /* List details live in the viewport, never after a potentially long list. */
  .detail-sheet{padding:0;color:var(--t1);width:min(640px,calc(100% - 32px));max-height:calc(100dvh - 32px);
                background:var(--s1);border:1px solid var(--line);border-radius:12px;overflow:auto}
  .detail-sheet::backdrop{background:color-mix(in srgb,var(--t1) 35%,transparent)}
  .detail-head{position:sticky;top:0;z-index:1;display:flex;align-items:center;gap:16px;padding:16px 24px;
               background:var(--s1);border-bottom:1px solid var(--line)}
  .detail-head h2{margin:0;flex:1;font-size:18px;overflow-wrap:anywhere}
  body.detail-open{overflow:hidden}
  #r-side{position:sticky;top:80px;max-height:calc(100dvh - 96px);overflow:auto}
  @media (max-width:720px){
    .detail-sheet{width:calc(100% - 16px);max-height:calc(100dvh - 16px - env(safe-area-inset-bottom))}
    .detail-head{padding:12px 16px}.detail-head .btn{min-height:44px}
    .detail-sheet .side{padding:16px}
  }
  .bars > div{display:grid;grid-template-columns:110px minmax(0,1fr) 40px;gap:12px;align-items:center;padding:10px 24px;font-size:14px}
  .bars > div + div{border-top:1px solid var(--line)}
  .bars i{display:block;height:6px;border-radius:3px;background:color-mix(in srgb,var(--brand) 65%,var(--s3))}
  .bars .num{text-align:right;font-variant-numeric:tabular-nums}

  /* ---- details drawer */
  .drawer{margin-top:32px;border-top:1px solid var(--line);padding-top:16px}
  .drawer > summary{cursor:pointer;font-size:14px;list-style-position:outside}
  .drawer > summary span{color:var(--t2);margin-left:8px}
  .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin:16px 0}
  .tile{background:var(--s1);border:1px solid var(--line);border-radius:10px;padding:12px 16px;display:flex;flex-direction:column}
  .tile span{font-size:13px;color:var(--t2)} .tile b{font-size:20px;font-variant-numeric:tabular-nums}

  /* ---- worker view */
  .conv{display:flex;flex-direction:column;min-height:0;overflow:hidden}
  .conv-h{display:flex;align-items:center;gap:12px;padding:12px 16px 12px 24px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  .conv-h .sp{flex:1}
  .conn{font-size:13px;color:var(--t2);display:inline-flex;align-items:center;gap:6px}
  .conn::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--t3)}
  .conn.live::before{background:var(--run)} .conn.warn::before{background:var(--wait)} .conn.bad::before{background:var(--block)}
  .seg b{font-weight:600;color:var(--t3);margin-left:4px;font-variant-numeric:tabular-nums}
  .qhead .sub{display:block}
  .capv{display:flex;align-items:baseline;gap:8px;margin:4px 0 12px} .capv b{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}
  .side > b{font-weight:600}
  .side .ref{font-size:13px}
  .bars > .empty{display:block}
  .rows > .empty,.rows > .qmore{display:block}
  @media (max-width:720px){ .dk{display:none} }
  /* ---- badges */
  .pill{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:500;padding:1px 8px;border-radius:999px;
        white-space:nowrap;color:var(--muted);background:var(--panel2)}
  .pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
  .pill.run{color:var(--accent);background:color-mix(in srgb,var(--accent) 13%,transparent)}
  .pill.run::before{animation:pulse 1.4s ease-in-out infinite}
  .pill.idle,.pill.ok{color:var(--green);background:color-mix(in srgb,var(--green) 13%,transparent)}
  .pill.close,.pill.pend,.pill.warn{color:var(--amber);background:color-mix(in srgb,var(--amber) 13%,transparent)}
  .pill.bad{color:var(--red);background:color-mix(in srgb,var(--red) 13%,transparent)}
  @keyframes pulse{50%{opacity:.3}}

/* TRANSCRIPT_CSS */
  .led{min-width:0;font-size:13px;line-height:1.45;color:color-mix(in srgb,var(--ink) 80%,var(--muted));overflow:hidden;
       overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
  .led i{font-style:normal;color:var(--faint)}
  .led.old{color:var(--muted)}
  .lage{display:inline-block;margin-right:8px;font-size:12.5px;color:var(--faint);white-space:nowrap;font-variant-numeric:tabular-nums}
  .lage.stale{color:var(--amber)} .lage.old{color:var(--faint)}
  /* ---- Fleet: summary strip, needs-attention rows, then one row per agent. Clicking a row opens its full
     ledger line. Context bar: the tick is the clear floor; brand under it, amber past it, red past the ceiling. */
  .ag td{vertical-align:top}
  .ag .an b{font-weight:600}
  .ag .role{display:block;font-size:13px;color:var(--t2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:240px}
  .ag .mdl-c{white-space:nowrap;display:block}
  .ag .mst-l{display:block;font-size:13px;color:var(--t2)}
  .ag.open .led{display:block;-webkit-line-clamp:unset}
  .ag .meter{width:140px}
  .ag .stw.block{color:var(--block)} .ag .stw.wait{color:var(--wait)}
  .meter{display:flex;flex-direction:column;gap:4px;min-width:0}
  .meter .track{position:relative;display:block;width:100%;height:6px;border-radius:3px;background:var(--s3)}
  .meter .fill{position:absolute;left:0;top:0;bottom:0;border-radius:3px;background:var(--brand)}
  .meter .fill.warm{background:var(--amber)} .meter .fill.hot{background:var(--red)}
  .meter .tick{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--t1);opacity:.7}
  .meter .mval{font-size:12px;color:var(--t2)} .meter .mval.warm{color:var(--amber)} .meter .mval.hot{color:var(--red)}
  .meter.none{font-size:12px;color:var(--t3)}
  .legend{padding:12px 20px;border-top:1px solid var(--line);font-size:12px;color:var(--t2)}
  .mdl{font-weight:500} .mdl.none{color:var(--t3)}
  .mst{font-size:12px;color:var(--t2);margin-left:6px}
  .mst.stale{color:var(--wait)}
  .cmd{font:12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;padding:5px 8px;border:1px solid var(--line);border-radius:6px;
       background:var(--s1);cursor:copy;user-select:all;white-space:nowrap}
  .cmd.copied{color:var(--run);border-color:var(--run)}

  @media (max-width:1100px){
    .cols{grid-template-columns:minmax(0,1fr)}
    #r-side{display:none}
  }
  /* Mobile: the summary strip becomes a short list,
     tables become stacked rows (title line, then one muted detail line), row actions collapse into the row. */
  @media (max-width:720px){
    .nav,.inst{display:none}
    .top-r{gap:6px}
    .queue-preview{grid-template-columns:minmax(0,1fr)}
    .page{padding:20px 16px 48px}
    .ph h1{font-size:26px}
    .sec{margin-top:24px}
    .strip{grid-template-columns:minmax(0,1fr);padding:0}
    .strip > div{border-left:0;border-top:1px solid var(--line);padding:12px 16px;display:flex;align-items:center;gap:12px}
    .strip > div:first-child{border-top:0}
    .strip .v{margin:0 0 0 auto} .strip .v b{font-size:15px} .strip .v span,.pips{display:none}
    .strip .k{font-size:15px;color:var(--t1)}
    .rows > *{grid-template-columns:auto minmax(0,1fr);padding:14px 16px}
    .rows .when,.rows .acts{display:none}
    .rows .acts.keep{display:flex;grid-column:2;flex-wrap:wrap} .rows .acts.keep .btn{min-width:0;flex:1;padding:0 10px}
    .toast{bottom:84px}
    .tbl thead{display:none}
    .tbl,.tbl tbody{display:block}
    .tbl tr{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 12px;padding:12px 16px;border-top:1px solid var(--line)}
    .tbl tbody tr:first-child{border-top:0}
    .tbl td{display:block;padding:0 !important;border:0}
    .tbl td.m-hide{display:none}
    .tbl td.m-sub{grid-column:1;font-size:13px;color:var(--t2)}
    .tbl td.m-end{grid-column:2;grid-row:1;text-align:right;font-size:13px;color:var(--t2)}
    .tbl td .btn{display:none}
    .tbl tr.sel td{background:none;box-shadow:none}
    .set-tbl tr{grid-template-columns:1fr 1fr;gap:6px 12px}
    .set-tbl td:first-child{grid-column:1 / -1}
    .set-tbl .pick-sel:not(.num){width:100%}
    .ag .meter{width:100%}
    .ag .role{max-width:none}
    .qhead,.qrow,.qmore,.side{padding-left:16px;padding-right:16px}
    .bars > div{padding:10px 16px;grid-template-columns:90px minmax(0,1fr) 32px}
    .run-search,.bar .search{width:100%;margin-left:0}
    .conv-h{padding:10px 12px}
    .tl{padding:12px 12px 20px}
    .err-banner{margin:12px 12px 0}
  }
  /* Phone-only pieces stay out of the desktop layout entirely. */
  .m-only,.tabbar{display:none}
  /* Phone pass: the scroll depth is the problem, so every list row is compact, repeated text is said once,
     empty sections fold away and navigation is a bottom tab bar. Nothing here applies above 720px. */
  @media (max-width:720px){
    .d-only{display:none} .m-only{display:revert}
    .top{-webkit-backdrop-filter:saturate(1.8) blur(18px);backdrop-filter:saturate(1.8) blur(18px);
         background:color-mix(in srgb,var(--s1) 86%,transparent);padding-top:env(safe-area-inset-top)}
    .top-in{height:48px;padding:0 8px 0 16px;gap:8px}
    .brand{min-width:0;font-size:17px} .vt{overflow:hidden;text-overflow:ellipsis}
    #theme{display:none}
    #settings-gear{display:inline-flex;min-width:44px;height:44px;border:0;background:none;font-size:20px;color:var(--t2)}
    #paused{display:inline-flex;padding:4px 10px;font-size:12px}
    .live{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--t2);font-variant-numeric:tabular-nums;white-space:nowrap}
    .live::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--run)}
    .live.bad::before{background:var(--block)}
    .tabbar{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;position:fixed;left:0;right:0;bottom:0;z-index:6;
            background:color-mix(in srgb,var(--s1) 86%,transparent);-webkit-backdrop-filter:saturate(1.8) blur(18px);backdrop-filter:saturate(1.8) blur(18px);
            border-top:1px solid var(--line);padding:2px 4px env(safe-area-inset-bottom)}
    .tabbar a{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;min-height:50px;
              text-decoration:none;color:var(--t3);font-size:10.5px;font-weight:600}
    .tabbar svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;transition:transform .12s}
    .tabbar a:active svg{transform:scale(.9)}
    .tabbar a[aria-current=page]{color:var(--brand)}
    .tb-n{position:absolute;top:3px;left:calc(50% + 5px);min-width:17px;height:17px;padding:0 4px;border-radius:9px;display:grid;place-items:center;
          font-size:10.5px;font-weight:700;color:#fff;background:var(--t3);font-variant-numeric:tabular-nums}
    .tb-n.bad{background:var(--block)}
    body[data-detail] .tabbar{display:none}
    .page{padding:8px 12px calc(76px + env(safe-area-inset-bottom))}
    body[data-detail] .page{padding-bottom:calc(16px + env(safe-area-inset-bottom))}
    footer.page{padding:0 16px calc(72px + env(safe-area-inset-bottom))}
    body[data-detail] footer.page{display:none}
    .toast{bottom:calc(72px + env(safe-area-inset-bottom))}
    /* The view name lives in the top bar; only Fleet (mode chip) and run detail (title) keep a page head. */
    #v-now > .ph,#v-runs > .ph,#v-queue > .ph,#v-settings > .ph{display:none}
    .ph{margin:4px 4px 8px;flex-wrap:wrap;row-gap:4px} .ph .upd:not(#f-gen){display:none}
    #fleet-view .ph h1{display:none}
    .sec{margin-top:16px} .sec h2{font-size:13px;font-weight:600;color:var(--t2);margin:0 4px 6px}
    .sec h2 .cnt{background:none;padding:0}
    .card{border:0;border-radius:14px;box-shadow:none}
    .empty{padding:14px 16px}
    /* Now: four numbers in one row; each opens its list. */
    .strip{grid-template-columns:repeat(4,minmax(0,1fr));padding:10px 0}
    .strip > div{display:flex;flex-direction:column-reverse;align-items:center;border-top:0;border-left:1px solid var(--line);padding:2px 4px;gap:0;cursor:pointer}
    .strip > div:first-child{border-left:0}
    .strip .k{font-size:11.5px;color:var(--t2);text-align:center;line-height:1.25}
    .strip .v{margin:0} .strip .v b{font-size:24px} .strip .v span,.strip .v .lnk,.pips{display:none}
    #n-needs-w.zero{display:none}
    /* Sub-tabs become a sticky segmented control that always fits the width. */
    .subtabs{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);gap:2px;padding:2px;margin:12px 0 0;border:0;border-radius:10px;
             background:var(--s3);position:sticky;top:calc(48px + env(safe-area-inset-top));z-index:4;box-shadow:0 0 0 8px var(--bg);overflow:visible}
    .subtabs a{display:flex;align-items:center;justify-content:center;gap:4px;min-height:36px;padding:0 4px;border:0;border-radius:8px;
               font-size:13px;font-weight:600;color:var(--t2);overflow:hidden;text-overflow:ellipsis}
    .subtabs a:hover{background:none}
    .subtabs a[aria-selected=true]{background:var(--s1);color:var(--t1);box-shadow:0 1px 3px rgba(0,0,0,.12)}
    .subtabs .cnt{background:none;padding:0;font-size:11px;color:var(--t3)}
    .subtabs + div > .sec:first-child,.subtabs + div > .card:first-child{margin-top:12px}
    #now-running > .sec{margin-top:12px}
    /* One compact row everywhere: two-line title, one muted line, the time on the right. */
    .tbl tr{gap:2px 12px;padding:10px 14px;min-height:56px;align-content:center}
    .tbl .t,.rows .h{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;line-height:1.3}
    .tbl td.m-sub,.tbl td.m-sub .s2{font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .tbl td .s2.g{display:none}
    .runc .s2{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    #n-run .tbl td:first-child{grid-row:1 / span 2} #n-run .tbl td.m-sub{grid-column:2;grid-row:2;text-align:right;max-width:30vw;align-self:end}
    .tbl tr.grp-h{display:block;min-height:0;padding:14px 4px 6px;background:var(--bg);font-size:13px;font-weight:600;color:var(--t2);border-top:0}
    .tbl tr.grp-h + tr{border-top:0}
    .tbl tr.more{display:block;min-height:0;padding:0}
    .tbl tr.more td{text-align:center}
    .tbl tr.more .lnk,.tfoot .lnk{min-height:44px}
    .rows > *{padding:10px 14px;min-height:56px}
    .rows > .more{display:block;min-height:0;padding:0;text-align:center}
    .rows .s{font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .tfoot{padding:8px 14px;font-size:12.5px}
    .queue-preview{gap:12px}
    #queue-next .sec > h2,#q-sub,.run-search .control-label{display:none}
    #q-drain .qhead{padding:12px 16px}
    .bar{margin-bottom:8px;gap:8px} .bar .seg{width:100%;display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr)}
    .bar .seg button{min-height:32px}
    #v-runs .cols > .card{margin-top:4px}
    /* Fleet: name and status, a thin context bar, one line of what it is doing. */
    .ag .role{display:none}
    .ag td.m-sub .meter{flex-direction:row-reverse;align-items:center;gap:8px}
    .ag td.m-sub .led{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;-webkit-line-clamp:unset}
    #fleet-view .legend{padding:10px 16px 12px;font-size:12.5px}
    /* Run detail: a readable title, the transcript gets the screen. */
    #d-title{font-size:19px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer}
    #d-title.open{display:block}
    #v-run .ph{margin-bottom:10px}
    .conv-h{padding:8px 12px;gap:8px}
    #v-run .tl{max-height:calc(100dvh - 240px)}
    .msg textarea{min-height:44px}
    #s-look .qrow{padding:10px 16px}
    .btn,.lnk{min-height:44px}
    .tbl td .btn,.rows .acts .btn{min-height:40px}
  }
  @media (prefers-reduced-motion:reduce){ *{transition:none !important;animation:none !important} }
</style>
</head>
<body>
<header class="top"><div class="top-in">
  <div class="brand"><span class="logo" aria-hidden="true"></span><span class="d-only">eph-broker</span><span class="m-only vt" id="vt"></span><span class="inst" id="inst"></span><span class="dot" id="dot" title="state feed"></span></div>
  <nav class="nav" aria-label="Views">
    <a href="#now" data-view="now">Now</a><a href="#runs" data-view="runs">Runs</a>
    <a href="#queue" data-view="queue">Queue</a><a href="#fleet" data-view="fleet" id="tab-fleet">Fleet</a>
  </nav>
  <div class="top-r">
    <a class="chip wait pause" id="paused" href="#queue" hidden><span class="d-only">New runs paused</span><span class="m-only">Paused</span></a>
    <span class="m-only live" id="live" title="Read-only, refreshes every 5 seconds"></span>
    <button class="btn" id="theme" title="Theme: auto" aria-label="Theme: auto">Theme: auto</button>
    <a class="btn sq ghost" id="settings-gear" href="#settings/workers" data-view="settings" aria-label="Settings" title="Settings">⚙</a>
  </div>
</div>
</header>

<div id="err" class="err-banner" style="display:none" role="alert"></div>

<main class="page">
<section id="v-now">
  <div class="ph"><h1>Now</h1><span class="upd"></span></div>
  <div class="card strip" id="n-strip"></div>
  <div class="sec" id="n-needs-w"><h2>Needs you <span class="cnt" id="n-needs-n">0</span></h2><div class="card rows" id="n-needs"></div></div>
  <nav class="subtabs" role="tablist" aria-label="Now sections">
    <a id="tab-now-running" role="tab" href="#now/running" aria-controls="now-running">Running <span class="cnt" id="n-run-n"></span></a>
    <a id="tab-now-queue" role="tab" href="#now/queue" aria-controls="now-queue">Queue <span class="cnt" id="n-q-n"></span></a>
    <a id="tab-now-recent" role="tab" href="#now/recent" aria-controls="now-recent">Recent <span class="cnt" id="n-history"></span></a>
  </nav>
  <div id="now-running" role="tabpanel" aria-labelledby="tab-now-running"><div class="sec card" id="n-run"></div></div>
  <div id="now-queue" role="tabpanel" aria-labelledby="tab-now-queue" hidden>
    <p class="sub">Preview only. Open Queue for the full lists and controls.</p>
    <div class="queue-preview">
      <div><h2><a href="#queue/waiting">Waiting to start <span class="cnt" id="n-wait-n"></span></a></h2><div class="card rows" id="n-wait"></div></div>
      <div><h2><a href="#queue/next">Next up</a></h2><div class="card rows" id="n-q"></div></div>
    </div>
  </div>
  <div id="now-recent" role="tabpanel" aria-labelledby="tab-now-recent" hidden>
    <div class="sec" id="h-card"><h2>Recent</h2>
      <p class="sub">Worker sessions this broker recorded, newest first.</p><div class="card rows" id="h-list"></div></div>
  </div>
  <details class="drawer" id="drawer"><summary><b>Details</b><span>Broker counters</span></summary>
    <div class="tiles" id="counters"></div></details>
</section>

<section id="v-runs" hidden>
  <div class="ph"><h1>Runs</h1><span class="upd"></span></div>
  <div class="bar"><div class="seg" id="r-seg" role="group" aria-label="Filter runs"></div>
    <label class="run-search"><span class="control-label">Search runs</span>
      <input class="search" id="r-q" type="search" placeholder="Search tickets or titles" aria-label="Search runs"></label></div>
  <div class="cols">
    <div class="card"><table class="tbl" id="r-tbl"><thead><tr><th>Run</th><th>Status</th><th>Model</th><th>Started</th><th>Time</th><th class="r">Tokens</th></tr></thead>
      <tbody id="r-rows"></tbody></table><div class="tfoot" id="r-foot"></div></div>
    <aside class="card side" id="r-side"></aside>
  </div>
</section>

<section id="v-queue" hidden>
  <div class="ph"><h1>Queue</h1><span class="upd"></span></div>
  <div class="card" id="q-drain"></div>
  <nav class="subtabs" role="tablist" aria-label="Queue stages">
    <a id="tab-queue-waiting" role="tab" href="#queue/waiting" aria-controls="queue-waiting"><span class="d-only">Waiting to start</span><span class="m-only">Waiting</span> <span class="cnt" id="q-wait-n"></span></a>
    <a id="tab-queue-next" role="tab" href="#queue/next" aria-controls="queue-next">Next up <span class="cnt" id="q-n"></span></a>
    <a id="tab-queue-source" role="tab" href="#queue/source" aria-controls="queue-source"><span class="d-only">By source</span><span class="m-only">Sources</span> <span class="cnt" id="q-src-n"></span></a>
  </nav>
  <div id="queue-waiting" role="tabpanel" aria-labelledby="tab-queue-waiting">
      <div class="card side" id="q-cap"></div>
      <div class="sec"><h2>Waiting to start: admitted, no worker <span class="chip wait" id="q-wait-drain" hidden>Drain on</span></h2><div class="card rows" id="q-wait"></div></div>
    </div>
    <div id="queue-next" role="tabpanel" aria-labelledby="tab-queue-next" hidden>
      <div class="sec"><h2>Next up: arrived, not admitted</h2><p class="sub" id="q-sub"></p>
        <div class="card"><table class="tbl"><thead><tr><th>#</th><th>Ticket</th><th>Source</th><th>Events</th><th>Arrived</th><th>Action</th></tr></thead>
          <tbody id="q-rows"></tbody></table><div class="tfoot" id="q-foot" hidden></div></div></div>
    </div>
    <div id="queue-source" role="tabpanel" aria-labelledby="tab-queue-source" hidden><div class="sec"><h2>By source</h2><div class="card bars" id="q-src"></div></div></div>
</section>

<section id="fleet-view" hidden>
  <div class="ph"><h1>Fleet</h1><span class="chip mode" id="f-mode"></span><span class="upd" id="f-gen"></span></div>
  <div id="f-err" class="empty card" hidden></div>
  <div class="card strip" id="f-strip" hidden></div>
  <div class="sec" id="f-needs-w" hidden><h2>Needs attention <span class="cnt" id="f-needs-n"></span></h2><div class="card rows" id="f-needs"></div></div>
  <div class="sec"><div class="bar"><h2 style="margin:0">Agents <span class="cnt" id="f-n"></span></h2>
      <div class="seg" id="f-seg" role="group" aria-label="Filter agents" style="margin-left:auto"></div></div>
    <div class="card"><table class="tbl"><thead><tr><th>Agent</th><th>Status</th><th>Model</th><th>Context</th><th>Working on</th><th>Updated</th></tr></thead>
      <tbody id="f-rows"></tbody></table>
      <div class="legend"><span class="d-only">Context bar: the tick is the agent's clear floor. Blue under it, amber past it, red past the ceiling.</span><span class="m-only">The bar is how full each agent's memory is. It clears itself after the tick mark; red means it is over its limit.</span></div></div>
  </div>
</section>

<section id="v-settings" hidden>
  <div class="ph"><h1>Settings</h1></div>
  <nav class="subtabs" role="tablist" aria-label="Settings sections">
    <a id="tab-settings-workers" role="tab" href="#settings/workers" aria-controls="settings-workers">Workers</a>
    <a id="tab-settings-groups" role="tab" href="#settings/groups" aria-controls="settings-groups">Groups</a>
    <a id="tab-settings-modules" role="tab" href="#settings/modules" aria-controls="settings-modules">Modules</a>
  </nav>
  <div class="sec m-only" id="s-look"><h2>Appearance</h2><div class="card qrow"><span>Theme</span><span></span>
    <div class="seg" id="s-theme" role="group" aria-label="Theme"></div></div></div>
  <p class="sub" id="s-unavailable">Worker and group settings require dashboard action access.</p>
  <div id="s-w" hidden>
  <div class="sec" id="settings-workers" role="tabpanel" aria-labelledby="tab-settings-workers"><h2>Worker settings</h2>
    <p class="sub" id="s-sub">Applies to new runs. Running work keeps the model it started with. Blue outline = changed here; Default puts it back.</p>
    <div class="card"><table class="tbl set-tbl"><thead><tr><th>Row</th><th class="m-hide">Harness</th><th>Default backend</th><th>Model pin</th><th>Policy</th></tr></thead>
      <tbody id="s-rows"></tbody></table></div>
  </div>
  <div class="sec" id="settings-groups" role="tabpanel" aria-labelledby="tab-settings-groups" hidden><h2>Group limits</h2>
    <div class="card"><table class="tbl"><thead><tr><th>Group</th><th>Max at once</th></tr></thead>
      <tbody id="s-caps"></tbody></table>
      <div class="legend">A group covers its row and every row under it (code covers code/...). 0 holds new runs in the queue; running work is not stopped.</div></div>
  </div>
  </div>
  <div class="sec" id="settings-modules" role="tabpanel" aria-labelledby="tab-settings-modules" hidden>
    <h2>Modules</h2><div class="card rows" id="s-modules"><p class="empty">No viewer modules configured.</p></div>
  </div>
</section>

<section id="v-run" hidden>
  <nav class="crumb"><a href="#runs" id="sv-x">Runs</a> › <span id="d-ref"></span></nav>
  <div class="ph"><h1 id="d-title"></h1><span class="upd"></span></div>
  <div class="cols">
    <div class="card conv">
      <div class="conv-h"><span id="d-badge"></span><span class="conn" id="sv-st"></span><span class="sp"></span>
        <div class="seg" role="group" aria-label="Transcript detail"><button id="cv" aria-pressed="true">Conversation</button><button id="hr" aria-pressed="false"
          title="Also show behind-the-scenes steps and technical run details">Everything<b id="hr-n">0</b></button></div></div>
      <div class="tl" id="sv-log"></div>
      <form class="msg" id="d-msg" hidden><label><span class="control-label">Message this run</span>
        <textarea id="d-msg-t" rows="1" maxlength="4000" aria-label="Message this run"
        placeholder="Message this run. Enter to send, Shift+Enter for newline."></textarea></label><button class="btn pri">Send</button></form>
    </div>
    <aside>
      <div class="card side" id="d-stats"></div>
      <div class="sec" id="d-prev-w" hidden><h2>Sessions</h2><div class="card rows" id="d-prev"></div></div>
    </aside>
  </div>
</section>
</main>
<!-- Phone navigation (display:none above 720px): every view one tap away, no hamburger. -->
<nav class="tabbar" id="tabbar" aria-label="Views">
  <a href="#now" data-view="now"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg><span>Now</span></a>
  <a href="#runs" data-view="runs"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/></svg><span>Runs</span></a>
  <a href="#queue" data-view="queue"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 3 8l9 5 9-5-9-5ZM3 13l9 5 9-5"/></svg><b class="tb-n" id="tb-q" hidden></b><span>Queue</span></a>
  <a href="#fleet" data-view="fleet"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="7" cy="7" r="3"/><circle cx="17" cy="7" r="3"/><circle cx="7" cy="17" r="3"/><circle cx="17" cy="17" r="3"/></svg><b class="tb-n bad" id="tb-f" hidden></b><span>Fleet</span></a>
</nav>

<footer class="page" style="padding-top:0;font-size:12px;color:var(--t3)">
  <span id="foot">Read-only, refreshes every 5 seconds</span>
  <!-- Buttons appear only when GET /actions says this viewer may act; actions.py re-checks every POST
       (proxy secret + SSO group + same-origin), so hiding a button is courtesy, not the security boundary. -->
</footer>
<div class="toast" id="toast" role="status" aria-live="polite" hidden></div>

<script>
const $ = id => document.getElementById(id);
// Phone-only list behaviour (row caps, fleet grouping). Desktop rendering never reads it.
const PHONE = matchMedia('(max-width:720px)');

/* TRANSCRIPT_TEXT_HELPERS */
function fmtNum(n){ return Number(n || 0).toLocaleString(); }
function age(s){
  if(s === null || s === undefined) return 'n/a';
  return s < 60 ? s.toFixed(1) + 's' : Math.floor(s/60) + 'm' + String(Math.floor(s%60)).padStart(2,'0') + 's';
}

// ---- theme (auto follows the OS; the choice is remembered per browser)
const THEMES = ['auto', 'light', 'dark'];
let theme = 'auto';
try{ theme = localStorage.getItem('eph-theme') || 'auto'; }catch(e){}
function applyTheme(t){
  if(t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  const [ic, lb] = t === 'light' ? ['☀', 'Light'] : t === 'dark' ? ['☾', 'Dark'] : ['◐', 'Auto'];
  $('theme').textContent = ic + ' Theme: ' + lb;
  $('theme').title = 'Theme: ' + lb;
  $('theme').setAttribute('aria-label', 'Theme: ' + lb + '. Click to change.');
  $('s-theme').innerHTML = segHtml('theme', t, [['auto', 'Auto'], ['light', 'Light'], ['dark', 'Dark']]);
}
applyTheme(theme);
$('theme').onclick = () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  try{ localStorage.setItem('eph-theme', theme); }catch(e){}
  applyTheme(theme);
};

/* TRANSCRIPT_MARKDOWN */
// ---- state view
const STATES = {open_running: ['Running', 'run'], open_idle: ['Idle', 'idle'],
                closing: ['Closing', 'close'], closed: ['Closed', 'close']};
function badge(label, cls){ return '<span class="pill ' + cls + '">' + esc(label) + '</span>'; }
function stateBadge(s){
  if(s.live && ['open_running', 'open_idle'].includes(s.live.state) && s.live.waiting) return badge('Waiting', 'idle');
  if(s.live && s.live.state === 'open_running' && !s.worker) return badge('Starting', 'idle');
  if(s.live){ const st = STATES[s.live.state] || [s.live.state || 'unknown', '']; return badge(st[0], st[1]); }
  if(s.worker) return s.worker.in_turn ? badge('In turn', 'run') : badge('Idle', 'idle');
  return badge('Ended', 'done');
}
function title(key){
  const p = String(key).split(':');
  if(p.length < 2) return {src: '', name: String(key)};
  const last = p[p.length - 1];
  return /^\d+$/.test(last) && p.length >= 3
    ? {src: p[0], name: p.slice(1, -1).join(' ') + ' #' + last}
    : {src: p[0], name: p.slice(1).join(':')};
}
function sessions(d){
  const m = new Map();
  for(const l of d.live) m.set(l.key, {key: l.key, live: l, worker: null});
  for(const w of d.workers){ const s = m.get(w.key) || {key: w.key, live: null, worker: null}; s.worker = w; m.set(w.key, s); }
  return [...m.values()].sort((a, b) => a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
}
// Recorded stream sessions (newest first) minus each live key's newest one, since that one IS the live row.
function previous(d){
  const live = new Set(d.live.map(l => l.key)), seen = new Set();
  return (d.history || []).filter(h => {
    if(live.has(h.key) && !seen.has(h.key)){ seen.add(h.key); return false; }
    return true;
  });
}

let lastState = null, lastDerived = null, selected = null, selectedPast = null, booted = false;
const HIST_PAGE = 25;
let histLimit = HIST_PAGE;      // Previous sessions are paged server-side (/state?hist=N)   // selectedPast = 8-char session of a previous one
let runsFilter = 'all', runsQ = '', runsSel = null, runSheet = null, fleetFilter = 'all', qLimit = 10;
const fleetOpen = new Set();          // agents whose ledger line is expanded; survives the 5s repaint
const HARNESS = {cc: 'Claude Code', codex: 'Codex', hermes: 'Hermes'};
// Why an admitted subject has no worker yet (adapter._waiting reasons), in plain words
const WAIT_WHY = {repo_lock: 'Repository lock: another run on the same repository holds it', concurrency: 'All worker slots are busy'};
const WAIT_GROUP = {repo_lock: 'Repository lock', concurrency: 'Slots full'};

// ---- shared renderers. Every agent/ticket string goes through esc(); numbers are formatted first.
function kTok(n){ return Math.round(Number(n) / 1000) + 'k'; }
/* TRANSCRIPT_FORMAT */
function dur(s){
  if(s === null || s === undefined) return '-';
  s = Math.max(0, Math.round(s));
  return s < 60 ? s + 's' : s < 3600 ? Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's'
    : Math.floor(s / 3600) + 'h ' + Math.floor(s % 3600 / 60) + 'm';
}
function ago(s){
  if(s === null || s === undefined) return 'n/a';
  return s < 60 ? 'now' : s < 3600 ? Math.floor(s / 60) + 'm' : s < 172800 ? Math.floor(s / 3600) + 'h' : Math.floor(s / 86400) + 'd';
}
function agoT(s){ const a = ago(s); return a === 'now' ? 'just now' : a === 'n/a' ? '-' : a + ' ago'; }
function clock(ts){ const t = new Date(ts * 1000); return isNaN(t) ? 'n/a' : t.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}); }
function stamp(ts){   // "4:02 PM" today, "Nov 4, 4:02 PM" before that
  const t = new Date(ts * 1000);
  if(isNaN(t)) return 'n/a';
  return t.toDateString() === new Date().toDateString() ? clock(ts)
    : t.toLocaleDateString([], {month: 'short', day: 'numeric'}) + ', ' + clock(ts);
}
function stIcon(k){ return '<span class="st ' + k + '" aria-hidden="true"></span>'; }
function cell(k, v, s, extra, go){
  return '<div' + (go ? ' data-go="' + esc(go) + '"' : '') + '><div class="k">' + esc(k) + '</div><div class="v"><b>' + esc(v) + '</b><span>' + esc(s) + '</span>' + (extra || '') + '</div></div>';
}
function segHtml(id, cur, opts){
  return opts.map(([v, label]) => '<button data-seg="' + id + '" data-v="' + esc(v) + '" aria-pressed="' + (v === cur) + '">' + esc(label) + '</button>').join('');
}
function pips(used, cap){
  return cap ? '<span class="pips" aria-hidden="true">' + Array.from({length: Math.min(cap, 12)},
    (_, i) => '<i' + (i < used ? ' class="on"' : '') + '></i>').join('') + '</span>' : '';
}

// ---- Fleet: fleet-status.json + FLEET_MODE + STATE.md age, read-only.
const EFFORTS = new Set(['low', 'medium', 'high', 'xhigh', 'max']);
function agentKind(a){ return a.stood_down ? 'off' : a.alive === false ? 'down' : a.alive === true ? 'up' : 'unk'; }
function modelFamily(a){ const m = String(a.model_label || ''); return m.startsWith('opus') ? 'opus' : m === 'local' ? 'local' : ''; }
// Normalize a row's model fields into {label, state, run, title}. `runtime_model*` (a Fleet row from
// fleet-status.json) is OBSERVED evidence about a running process; a Fleet row's `model` is only the
// mode-resolved CONFIGURED model. A Worker row's own `model` IS observed (its harness recorded it at
// launch), so its model_state is trusted as given. state: running | stale | configured | unknown.
function effModel(a){
  const st = a.model_state || 'unknown';
  const obs = a.runtime_model_label || a.runtime_model || null;
  const cfg = a.model_label || a.model || null;
  const obsRaw = a.runtime_model || a.model || '';
  const cfgRaw = a.model || '';
  const age = a.runtime_model_age;
  if((st === 'running' || st === 'stale') && (obs || cfg)){
    const label = obs || cfg;
    return {label: label, state: st, run: true,
      title: (st === 'stale' ? 'Last observed running model: ' : 'Observed running model: ') + obsRaw
        + (age == null ? '' : ' (' + ago(age) + ' ago)')
        + (obs && cfgRaw && cfgRaw !== obsRaw ? '. Configured for this mode: ' + cfgRaw : '')};
  }
  if(st === 'configured' && cfg)
    return {label: cfg, state: 'configured', run: false,
      title: 'Configured model for this mode, not observed running: ' + cfgRaw};
  return {label: null, state: 'unknown', run: false, title: 'No model reported for this row'};
}
// One chip for both views: the observed model when there is one, else the configured model labelled as
// such, else "model unknown". State is always spelled out in text, never colour alone.
function modelChip(a){
  const m = effModel(a);
  if(!m.label) return '<span class="mdl none">-</span>';
  return '<span class="mdl ' + modelFamily({model_label: m.label}) + (m.run ? ' run' : '') + '" title="'
    + esc(m.title) + '">' + esc(m.label) + '</span>'
    + (m.state === 'stale' ? '<span class="mst stale" title="' + esc(m.title) + '">stale</span>' : '');
}
// Context bar scaled to the agent's ceiling; the tick is its clear floor. Unknown/stale readings say so in
// words instead of drawing a bar (a percent is never multiplied into a fake token count).
function ctxMeter(a){
  const t = a.ctx_tokens, soft = a.context_floor, ceil = a.context_ceiling || a.context_window;
  if(t == null) return '<div class="meter none" title="No token count reported">'
    + esc(a.context_pct == null ? '-' : a.context_pct + '%') + '</div>';
  if(a.context_state !== 'ok' || ceil == null)
    return '<div class="meter none" title="' + esc(a.context_state === 'stale' ? 'Last reading is old' : 'No limit known') + '">'
      + esc(kTok(t) + (a.context_state === 'stale' ? ' (old)' : '')) + '</div>';
  const heat = t >= ceil ? 'hot' : soft != null && t >= soft ? 'warm' : '';
  const pos = v => (Math.min(v, ceil) / ceil * 100).toFixed(1) + '%';
  return '<div class="meter" title="' + esc(fmtNum(t)) + ' tokens in context (' + esc(a.context_pct)
      + '% of ' + esc(fmtNum(a.context_window)) + '). Ceiling ' + esc(kTok(ceil))
      + (soft == null ? '' : ', clear floor ' + esc(kTok(soft))) + '.">'
    + '<span class="track"><span class="fill ' + heat + '" style="width:' + pos(t) + '"></span>'
    + (soft == null ? '' : '<span class="tick" style="left:' + pos(soft) + '"></span>') + '</span>'
    + '<span class="mval ' + heat + '">' + esc(kTok(t) + ' of ' + kTok(ceil)) + '</span></div>';
}
// Supervisor trouble sorts with the down agents (bad) or just after them (warn)
function rank(a){
  const f = a.flags || [];
  return a.alive === false || f.some(x => x.sev === 'bad') ? 0 : f.length ? 1 : 2;
}
// Why an agent needs a human now -> {sev, head, why} or null. Order = most urgent first.
function needWhy(a){
  const ceil = a.context_ceiling;
  if(a.stood_down) return null;
  const bad = (a.flags || []).filter(x => x.sev === 'bad');
  if(a.alive === false) return {sev: 'bad', head: 'Down', why: bad.length ? bad.map(x => x.t).join('; ')
    : 'Pane gone and the supervisor has not brought it back.'};
  if(bad.length) return {sev: 'bad', head: bad[0].t, why: bad.slice(1).map(x => x.t).join('; ')};
  if(a.context_state === 'ok' && a.ctx_tokens != null && ceil != null && a.ctx_tokens >= ceil)
    return {sev: 'bad', head: 'Over the ceiling at ' + kTok(a.ctx_tokens),
    why: kTok(a.ctx_tokens - ceil) + ' past the ' + kTok(ceil) + ' ceiling. Must clear at its next clean seam.'};
  if(a.silent) return {sev: 'warn', head: 'Silent for ' + ago(a.busy_age),
    why: 'Busy for ' + ago(a.busy_age) + ' but STATE.md/TASKS.md ' + (a.ledger_age == null ? 'are unreadable'
      : 'last moved ' + ago(a.ledger_age) + ' ago') + '. Possible wedge or permission prompt.'};
  return null;
}
// Status column: [icon, word]. Supervisor flags and needWhy() win over the busy/idle reading.
function agentStatus(a){
  if(a.stood_down) return ['off', 'Stood down'];
  const n = needWhy(a), warn = (a.flags || [])[0];
  if(n) return [n.sev === 'bad' ? 'block' : 'wait', n.head];
  if(warn) return ['wait', warn.t];
  if(a.busy === true) return ['run', a.busy_age == null || a.busy_age < 60 ? 'Working' : 'Working, ' + ago(a.busy_age)];
  if(a.busy === false) return ['idle', 'Idle'];
  if(a.alive === true) return ['run', 'Online'];
  return ['off', 'Unknown'];   // missing/stale metadata is not evidence that a TFA has no session
}
function needCard(a, n){
  const name = String(a.name ?? '?');
  return '<div>' + stIcon(n.sev === 'bad' ? 'block' : 'wait') + '<div><div class="h"><b>' + esc(name) + '</b> ' + esc(n.head) + '</div>'
    + (n.why ? '<div class="s">' + esc(n.why) + '</div>' : '') + '</div>'
    + '<span class="when" title="STATE.md/TASKS.md last edited">' + esc(a.ledger_age == null ? '' : 'ledger ' + ago(a.ledger_age)) + '</span>'
    + '<span class="acts">' + (a.runtime_kind !== 'container' && (a.busy === true || a.busy === false) && /^[A-Za-z0-9_.-]+$/.test(name)   // pane verdict + shell-safe name
      ? '<code class="cmd" tabindex="0" title="click to copy">' + esc('tmux attach -t ' + name) + '</code>' : '') + '</span></div>';
}
function agentRow(a){
  const name = String(a.name ?? '?'), open = fleetOpen.has(name), [k, word] = agentStatus(a), la = a.ledger_age;
  const eff = EFFORTS.has(a.effort) ? '<span class="mst-l">' + esc(a.effort + ' effort') + '</span>' : '';
  const now = a.ledger_source === 'now';
  return '<tr class="ag pick' + (open ? ' open' : '') + '" data-agent="' + esc(name) + '" tabindex="0" aria-expanded="' + open + '">'
    + '<td><div class="an"><b>' + esc(a.name ?? '?') + '</b>'
    + (a.agent_type === 'tfa' ? ' <span class="tag">' + esc(a.runtime_kind === 'container' ? 'container' : 'tmux') + '</span>' : '')
    + '<span class="role" title="' + esc(a.role || '') + '">' + esc(
      [a.agent_type === 'tfa' ? 'Temporary' : '', a.runtime && a.runtime !== 'claude' ? HARNESS[a.runtime] || a.runtime : '',
        a.pool_bot, a.age == null ? '' : 'age ' + ago(a.age), a.role]
        .filter(Boolean).join(' · ')) + '</span></div></td>'
    + '<td class="m-end"><span class="stw ' + k + '">' + stIcon(k) + esc(word) + '</span></td>'
    + '<td class="m-hide"><div class="mdl-c">' + modelChip(a) + eff + '</div></td>'
    + '<td class="m-sub">' + ctxMeter(a) + '</td>'
    + '<td class="m-sub"><div class="led' + (a.ledger && !now ? ' old' : '') + '"' + (a.ledger && !now
      ? ' title="From its notes (STATE.md): this agent has no NOW.md line yet"' : '') + '>' + (a.ledger ? esc(a.ledger) : '<i>-</i>') + '</div></td>'
    + '<td class="num m-hide"><span class="lage' + (la > 86400 ? ' old' : la > 21600 ? ' stale' : '')
    + '" title="' + (now ? 'NOW.md' : 'STATE.md') + ' last edited">' + esc(la == null ? '-' : ago(la)) + '</span></td></tr>';
}
// Phone: agents that need attention first, then working ones, idle ones folded into one row.
function fleetGroups(shown){
  const head = (t, n) => '<tr class="grp-h"><td colspan="6">' + esc(t) + ' ' + n + '</td></tr>';
  const groups = [['Needs attention', shown.filter(a => !a.stood_down && fleetSort(a) <= 1)],
    ['Working', shown.filter(a => !a.stood_down && fleetSort(a) === 2)], ['Idle', shown.filter(a => !a.stood_down && fleetSort(a) === 3)],
    ['Stood down', shown.filter(a => a.stood_down)]];
  return groups.filter(g => g[1].length).map(([t, xs]) => head(t, xs.length) + (t !== 'Idle' || fleetIdle || xs.length < 4 ? xs.map(agentRow).join('')
    + (t === 'Idle' && xs.length >= 4 ? '<tr class="more"><td colspan="6"><button class="lnk" id="f-idle">Hide idle agents</button></td></tr>' : '')
    : '<tr class="pick" id="f-idle" tabindex="0" role="button" aria-expanded="false"><td>' + stIcon('idle') + ' ' + esc(xs.slice(0, 3).map(a => a.name).join(', ')
      + ' and ' + (xs.length - 3) + ' more') + '</td><td class="m-end">Show</td></tr>')).join('');
}
function fleetSort(a){ return a.stood_down ? 4 : rank(a) === 0 ? 0 : needWhy(a) ? 1 : a.busy === true ? 2 : 3; }
function paintFleet(f){
  f = f || {error: 'no fleet data in /state'};
  $('f-mode').textContent = 'Mode: ' + (f.mode || f.mode_error || 'unknown');
  const rows = Array.isArray(f.agents) ? f.agents : [];
  const act = rows.filter(a => !a.stood_down), bench = rows.filter(a => a.stood_down);
  const needs = act.map(a => [a, needWhy(a)]).filter(p => p[1]);
  const working = act.filter(a => a.busy === true && a.alive !== false);
  const idle = act.filter(a => a.busy === false && a.alive !== false);
  const tfas = rows.filter(a => a.agent_type === 'tfa');
  const standing = rows.filter(a => a.agent_type == null || a.agent_type === 'standing');
  $('f-strip').innerHTML = cell('Working', working.length, 'agents mid-task') + cell('Idle', idle.length, 'ready for work')
    + cell('Needs attention', needs.length, needs.length === 1 ? 'agent' : 'agents') + cell('Stood down', bench.length, 'by mode or by hand');
  const gen = f.generated_age == null ? '' : f.generated_age < 60 ? 'Updated just now' : 'Updated ' + ago(f.generated_age) + ' ago';
  const sup = f.supervisor;   // Only speak up when the supervisor feed exists but is unhealthy
  $('f-gen').className = 'upd' + (f.stale || f.error ? ' bad' : '');
  $('f-gen').innerHTML = esc(gen) + (f.stale ? ' <span class="pill bad">stale</span>' : '')
    + (sup && sup.error ? ' <span class="pill warn">' + esc(sup.error) + '</span>'
      : sup && sup.stale ? ' <span class="pill warn">supervisor ' + esc(ago(sup.age)) + ' old</span>'
      : sup && sup.tmux_server_up === false ? ' <span class="pill bad">tmux server down</span>' : '');
  $('f-err').hidden = !f.error && !f.container_error && !f.status_error && rows.length > 0;
  $('f-err').textContent = [f.error, f.container_error, f.status_error].filter(Boolean).join('; ') || 'No agents listed in fleet-status.json.';
  $('f-needs-w').hidden = true;   // Fleet is the agent table; attention is also visible in each row.
  $('f-needs-n').textContent = needs.length;
  $('f-needs').innerHTML = needs.map(p => needCard(p[0], p[1])).join('');
  $('f-n').textContent = rows.length;
  $('f-seg').innerHTML = segHtml('fleet', fleetFilter, [['all', 'All'], ['standing', 'Standing ' + standing.length],
    ['tfa', 'Temporary ' + tfas.length]]);
  const pool = (fleetFilter === 'tfa' ? tfas : fleetFilter === 'standing' ? standing : rows).slice()
    .sort((x, y) => fleetSort(x) - fleetSort(y) || String(x.name).localeCompare(String(y.name)));
  const off = pool.filter(a => a.stood_down).length, shown = fleetBench ? pool : pool.filter(a => !a.stood_down);
  $('tb-f').hidden = !needs.length; $('tb-f').textContent = needs.length;
  $('f-rows').innerHTML = (shown.length ? PHONE.matches ? fleetGroups(shown) : shown.map(agentRow).join('') : '<tr><td colspan="6" class="empty">No agents.</td></tr>')
    + (off ? '<tr class="more"><td colspan="6"><button class="lnk" id="f-bench">' + esc(fleetBench ? 'Hide stood-down agents'
      : 'Show ' + off + ' stood-down ' + (off === 1 ? 'agent' : 'agents')) + '</button></td></tr>' : '');
}
// Tower: click a `tmux attach` chip to copy it (user-select:all is the no-clipboard fallback)
$('f-needs').addEventListener('click', e => {
  const c = e.target.closest('.cmd');
  if(!c || !navigator.clipboard) return;
  navigator.clipboard.writeText(c.textContent).then(() => { c.classList.add('copied'); setTimeout(() => c.classList.remove('copied'), 1200); }, () => {});
});
let fleetBench = false;   // stood-down agents are hidden until asked for
let fleetIdle = false, runsAll = false, qAll = false;   // phone: idle agents folded, runs and waiting capped at RUNS_PHONE until asked
const RUNS_PHONE = 10;
function toggleAgent(e){
  if(e.type === 'click' && e.target.id === 'f-bench'){ fleetBench = !fleetBench; paintFleet(lastState && lastState.fleet); return; }
  if(e.target.closest && e.target.closest('#f-idle') && (e.type === 'click' || e.key === 'Enter' || e.key === ' ')){
    e.preventDefault(); fleetIdle = !fleetIdle; paintFleet(lastState && lastState.fleet); return;
  }
  const r = e.target.closest('.ag[data-agent]');
  if(!r || (e.type === 'keydown' && e.key !== 'Enter' && e.key !== ' ')) return;
  if(e.type === 'keydown') e.preventDefault();
  const n = r.dataset.agent, open = !fleetOpen.has(n);
  open ? fleetOpen.add(n) : fleetOpen.delete(n);
  r.classList.toggle('open', open);
  r.setAttribute('aria-expanded', open);
}
['f-rows'].forEach(id => { $(id).addEventListener('click', toggleAgent); $(id).addEventListener('keydown', toggleAgent); });

// ---- runs. /state's live[] is the coalescer's ADVERTISED state (a subject waiting on a repository lock still
// reads open_running); workers[] is real liveness. "Running" is only ever a worker; an admitted subject without
// one (or one parked on a lock or slot) is "Waiting to start", with the reason in words.
function tid(key){ const m = /^vikunja:task:(\d+)$/.exec(String(key)); return m ? m[1] : null; }
const titles = new Map();   // run key -> ticket title, from live workers and recorded history (newest wins)
function learnTitles(d){
  for(const h of (d.history || []).slice().reverse()) if(h.title) titles.set(h.key, h.title);
  for(const x of [...(d.queue || []), ...d.pending, ...d.live]) if(x.title) titles.set(x.key, x.title);
  for(const w of d.workers) if(w.title) titles.set(w.key, w.title);
}
function runName(key, w){
  const id = tid(key), t = title(key);
  return {ref: id ? '#' + id : (t.src ? t.src + ' ' : '') + t.name, title: (w && w.title) || titles.get(key) || ''};
}
function runLabel(key, w){
  const r = runName(key, w);
  return r.title ? '<span class="ref">' + esc(r.ref) + '</span>' + esc(r.title) : esc(r.ref);
}
function ticketUrl(key){
  const id = tid(key), tpl = lastState && lastState.ticket_url;
  return id && typeof tpl === 'string' && /^https?:\/\//.test(tpl) && tpl.includes('{}') ? tpl.split('{}').join(id) : '';
}
// What needs a human decision. Today: the durable journal's own blockers (an ambiguous launch after a restart
// blocks every new run until someone settles it, and a journal error stops intake).
function needsYou(d){
  const dr = d.drain || {}, out = [];
  if(dr.error) out.push({k: 'block', key: null, head: 'The intake journal reports an error',
    sub: String(dr.error) + '. New runs cannot start until it is fixed.'});
  for(const key of dr.recovery_subjects || []) out.push({k: 'block', key: String(key),
    head: 'Launch outcome unknown after a restart', sub: 'New runs are blocked until you settle this one.'});
  return out;
}
function derive(d){
  const wk = new Map(d.workers.map(w => [w.key, w])), liveBy = new Map(d.live.map(l => [l.key, l]));
  const retiring = new Set(d.live.filter(l => ['closing', 'closed', 'parking'].includes(l.state)).map(l => l.key));
  const paused = !!(d.drain && d.drain.enabled);
  const started = new Map();
  for(const h of d.history || []) if(!started.has(h.key)) started.set(h.key, h.ts);
  const running = d.workers.filter(w => !(liveBy.get(w.key) || {}).waiting);
  const waiting = d.live.filter(l => !retiring.has(l.key) && (!wk.has(l.key) || l.waiting)).map(l => ({...l,
      why: WAIT_WHY[l.waiting] || (l.waiting ? String(l.waiting) : l.state === 'open_idle' ? 'Idle, no worker attached' : 'Starting up'),
      group: l.waiting ? WAIT_GROUP[l.waiting] || String(l.waiting) : 'Starting up'}));
  // A retiring live entry is the PREVIOUS launch, not the pending/intake follow-up.
  const queued = key => !liveBy.has(key) || retiring.has(key);
  const queue = new Map((d.queue || []).filter(q => queued(q.key)).map(q => [q.key, {...q, movable: true}]));
  for(const p of d.pending) if(queued(p.key)) queue.set(p.key, {...(queue.get(p.key) || {}), ...p, movable: true});
  for(const q of queue.values()) if(retiring.has(q.key)) q.why = 'Previous worker tearing down';
  const ordered = [...queue.values()].sort((a, b) => Number(b.key === d.next_subject) - Number(a.key === d.next_subject));
  return {wk, started, running, waiting, queue: ordered, needs: needsYou(d), drain: d.drain || null,
          cap: d.concurrency.cap, used: d.concurrency.in_use};
}
function allRuns(d, D){
  const rows = [];
  for(const w of D.running){
    const t0 = D.started.get(w.key);
    rows.push({key: w.key, w, live: true, k: w.in_turn ? 'run' : 'idle', word: w.in_turn ? 'Running' : 'Idle', grp: 'running',
      model: w.model_label || w.model, started: t0, time: t0 != null ? d.now - t0 : w.turn_age, tokens: w.tokens});
  }
  for(const x of D.waiting) rows.push({key: x.key, w: D.wk.get(x.key), k: 'wait', word: 'Waiting', why: x.why, grp: 'waiting', route: x.route});
  for(const h of previous(d)) rows.push({key: h.key, past: h.session, k: 'off', word: 'Ended', grp: 'finished', started: h.ts,
    model: h.model, harness: h.harness, tokens: h.tokens, ended: h.ended, time: h.ended != null ? h.ended - h.ts : null});
  return rows;
}
function runCard(r, inView){
  const w = r.w || {}, nm = runName(r.key, r.w), url = ticketUrl(r.key);
  const now = lastState ? lastState.now : Date.now() / 1000;
  // Technical fields (backend, step count, session id) only with "Everything" on
  const kv = [['Status', r.word + (r.why ? ': ' + r.why : '')], ['Model', w.model_label || w.model || r.model],
    ['Harness', HARNESS[w.harness || r.harness] || w.harness || r.harness], ['Kind of work', w.route || r.route], ['Turns', w.turns],
    ['Tokens', w.tokens != null ? kfmt(w.tokens) : r.tokens != null ? kfmt(r.tokens) : null],
    ['Started', r.started != null ? stamp(r.started) + ' (' + agoT(now - r.started) + ')' : null],
    ['Ran for', r.ended != null && r.started != null ? dur(r.ended - r.started) : null],
    ['Last activity', w.last_activity ? agoT(now - w.last_activity) : null]]
    .concat(showHarness ? [['Backend', w.backend], ['Steps', w.items], ['Session', r.past || w.session]] : [])
    .filter(p => p[1] !== null && p[1] !== undefined && p[1] !== '');
  const open = !inView && r.grp !== 'waiting'
    ? '<button class="btn' + (r.past ? '' : ' pri') + '" data-key="' + esc(r.key) + '"' + (r.past ? ' data-past="' + esc(r.past) + '"' : '') + '>'
      + (r.past ? 'Open transcript' : 'Watch live') + '</button>' : '';
  const ctl = !can ? '' : r.live ? actBtn('Cancel run', 'cancel', {subject_key: r.key}, {bad: true, done: 'Cancelled ' + nm.ref,
      confirm: 'Cancel ' + nm.ref + '? The worker stops now and the ticket will not start again on its own.'})
    : r.grp === 'finished' && tid(r.key) && !(lastState && lastState.live.some(l => l.key === r.key))
      ? '<div class="rerun"><div class="rerun-controls">' + modelPick(r.key)
        + actBtn('Run again', 'rerun', {task_id: Number(tid(r.key))}, {done: 'Queued ' + nm.ref + ' to run again', pickFor: r.key})
        + '</div>' + rerunHint(r.key) + '</div>' : '';
  return (inView ? '<h3>Run</h3>' : '<div class="ref">' + esc(nm.ref) + '</div><h3>' + esc(nm.title || nm.ref) + '</h3>')
    + '<dl class="kv">' + kv.map(([k, v]) => '<dt>' + esc(k) + '</dt><dd>' + esc(v) + '</dd>').join('') + '</dl>'
    + (open || url || ctl ? '<div class="acts">' + open + (url ? '<a class="btn" href="' + esc(url) + '" target="_blank" rel="noopener">Open ticket</a>' : '') + ctl + '</div>' : '');
}
function drainHead(dr, long){
  const on = !!(dr && dr.enabled), durable = !!(dr && dr.durable);
  return '<div class="qhead"><div><b>Pause new runs</b><span class="sub">' + esc(!durable
      ? 'Unavailable: pausing needs the durable intake journal (BROKER_STATE_PATH).'
      : 'Queued tickets wait here instead of starting.' + (long ? ' Running work is not touched.' : '')) + '</span></div>'
    + (can && durable ? '<button class="sw qstate" role="switch" aria-checked="' + on + '" aria-label="Pause new runs"'
        + actAttrs('drain', {enabled: !on}, on ? 'New runs resumed' : 'New runs paused') + '></button>'
      : '<span class="qstate chip ' + (on ? 'wait' : 'mode') + '">' + (on ? 'On' : 'Off') + '</span>') + '</div>';
}
function bars(pairs){
  const max = Math.max(1, ...pairs.map(p => p[1]));
  return pairs.length ? pairs.map(([k, n]) => '<div><span>' + esc(k) + '</span><i style="width:' + (n / max * 100).toFixed(0)
    + '%"></i><span class="num">' + esc(n) + '</span></div>').join('') : '<p class="empty">None.</p>';
}
function countBy(xs, f){
  const m = new Map();
  for(const x of xs){ const k = f(x); m.set(k, (m.get(k) || 0) + 1); }
  return [...m].sort((a, b) => b[1] - a[1]);
}

function needRow(n){
  const url = n.key ? ticketUrl(n.key) : '';
  return '<div>' + stIcon(n.k) + '<div><div class="h">' + (n.key ? '<span class="ref">' + esc(runName(n.key).ref) + '</span>' : '')
    + '<b>' + esc(n.head) + '</b></div><div class="s">' + esc(n.sub) + '</div></div><span></span>'
    + '<span class="acts' + (can && n.key ? ' keep' : '') + '">' + (can && n.key ? actBtn('Retry', 'reconcile', {subject_key: n.key, decision: 'retry'},
        {done: 'Retrying ' + runName(n.key).ref,
        confirm: 'Retry ' + runName(n.key).ref + '? If the last launch did start, this runs it a second time.'})
      + actBtn('Abandon', 'reconcile', {subject_key: n.key, decision: 'abandon'}, {bad: true, done: 'Abandoned ' + runName(n.key).ref,
        confirm: 'Abandon ' + runName(n.key).ref + '? It is marked done and will not start again on its own.'}) : '')
    + (url ? '<a class="btn" href="' + esc(url) + '" target="_blank" rel="noopener">Open ticket</a>' : '') + '</span></div>';
}
function runRow(w, D, d){
  const t0 = D.started.get(w.key), r = runName(w.key, w);
  return '<tr class="pick" tabindex="0" data-key="' + esc(w.key) + '"><td><div class="runc">' + stIcon(w.in_turn ? 'run' : 'idle')
    + '<div><div class="t">' + esc(r.title || r.ref) + '</div><div class="s2">'
    + esc([r.title ? r.ref : '', w.in_turn ? 'Running' : 'Idle between turns', w.route].filter(Boolean).join(' · ')) + '</div></div></div></td>'
    + '<td class="m-sub nw">' + esc(w.model_label || w.model || 'model unknown') + '<div class="s2 dk">' + esc(HARNESS[w.harness] || w.harness || '') + '</div></td>'
    + '<td class="num m-end">' + esc(dur(t0 != null ? d.now - t0 : w.turn_age)) + '</td>'
    + '<td class="num m-hide">' + esc(kfmt(w.tokens)) + '</td>'
    + '<td class="r m-hide"><button class="btn" data-key="' + esc(w.key) + '">Watch</button></td></tr>';
}

function queueWhy(q, d){
  if(d.drain && d.drain.error) return 'Intake journal error';
  if(d.drain && (d.drain.recovery_subjects || []).length) return 'Launch recovery needs a decision';
  if(d.drain && d.drain.enabled) return 'Paused: broker draining';
  if(q.why) return q.why;
  if(q.spawn_in > 0) return 'Collecting events, ready in ' + dur(q.spawn_in);
  return 'Awaiting admission: dispatch policy, group limits and capacity apply';
}
function stageRow(x, d, waiting, controls){
  const url = ticketUrl(x.key), reason = waiting ? x.why : queueWhy(x, d);
  return '<div>' + stIcon('wait') + '<div><div class="h">' + runLabel(x.key)
    + '</div><div class="s">' + esc((title(x.key).src || 'other') + ' · Age: ' + dur(x.arrived_age))
    + '</div><div class="s">' + esc(reason) + '</div></div><span></span><span class="acts">'
    + (url ? '<a class="btn ghost" href="' + esc(url) + '" target="_blank" rel="noopener">Open ticket</a>' : '')
    + (controls && waiting && can && x.waiting === 'concurrency' ? actBtn('Move to front', 'move-to-front', {subject_key: x.key},
      {done: 'Selected ' + runName(x.key).ref + ' as next eligible'}) : '') + '</span></div>';
}

function paint(d){
  lastState = d;
  learnTitles(d);
  const D = lastDerived = derive(d), paused = !!(D.drain && D.drain.enabled);
  $('dot').className = 'dot';
  document.querySelectorAll('.upd:not(#f-gen)').forEach(u => { u.textContent = 'Updated ' + clock(d.now); u.classList.remove('bad'); });
  $('inst').textContent = d.instance || '';
  $('paused').hidden = !paused;
  $('foot').textContent = can ? 'Refreshes every 5 seconds. Every action you take here is logged with your name.' : 'Read-only, refreshes every 5 seconds';

  // Now
  $('n-strip').innerHTML = cell('Running', D.used, D.cap != null ? 'of ' + D.cap + ' slots' : 'workers', pips(D.used, D.cap), '#now/running')
    + cell('Waiting to start', D.waiting.length, 'behind a lock or limit', '<a class="lnk" href="#queue/waiting">View</a>', '#queue/waiting')
    + cell('Queued', D.queue.length, paused ? 'held while paused' : 'waiting to dispatch', '<a class="lnk" href="#queue/next">View</a>', '#queue/next')
    + cell('Needs you', D.needs.length, D.needs.length === 1 ? 'decision waiting' : 'decisions waiting');
  $('n-needs-w').classList.toggle('zero', !D.needs.length);
  $('live').textContent = clock(d.now);
  $('live').classList.remove('bad');
  const qn = D.queue.length + D.waiting.length;
  $('tb-q').hidden = !qn; $('tb-q').textContent = qn;
  $('n-needs-n').textContent = D.needs.length;
  $('n-needs').innerHTML = D.needs.length ? D.needs.map(needRow).join('') : '<p class="empty">Nothing needs you right now.</p>';

  const free = D.cap == null ? null : Math.max(0, D.cap - D.used);
  $('n-run-n').textContent = D.cap != null ? D.running.length + ' of ' + D.cap : D.running.length;
  $('n-run').innerHTML = (D.running.length ? '<table class="tbl"><thead><tr><th>Run</th><th>Model</th><th>Time</th><th>Tokens</th><th></th></tr></thead><tbody>'
    + D.running.map(w => runRow(w, D, d)).join('') + '</tbody></table>' : '<p class="empty">No workers running.</p>')
    + (free == null ? '' : '<div class="tfoot">' + esc(free + (free === 1 ? ' slot' : ' slots') + ' free.'
      + (paused && D.queue.length ? ' New runs are paused, so the queue will not start them.' : '')) + '</div>');

  $('n-q-n').textContent = D.queue.length + D.waiting.length;
  $('n-q').innerHTML = D.queue.length ? D.queue.slice(0, 3).map(q => stageRow(q, d, false)).join('')
    + '<div class="qmore"><a class="lnk" href="#queue/next">Open Queue (' + esc(D.queue.length) + ')</a></div>'
    : '<p class="empty">Nothing queued.</p>';

  $('n-wait-n').textContent = D.waiting.length;
  $('n-wait').innerHTML = D.waiting.length ? D.waiting.slice(0, 3).map(x => stageRow(x, d, true)).join('')
    + '<div class="qmore"><a class="lnk" href="#queue/waiting">Open Waiting to start (' + esc(D.waiting.length) + ')</a></div>'
    : '<p class="empty">Nothing waiting.</p>';

  const hs = previous(d);
  $('h-card').hidden = !d.stream_enabled;
  $('n-history').textContent = d.history_total ?? hs.length;
  $('h-list').innerHTML = hs.length ? hs.slice(0, 6).map(h => '<button class="row" data-key="' + esc(h.key) + '" data-past="' + esc(h.session) + '">'
      + stIcon('off') + '<div><div class="h">' + runLabel(h.key) + '</div><div class="s">' + esc(['Started ' + stamp(h.ts), h.model,
        h.tokens != null ? kfmt(h.tokens) + ' tokens' : ''].filter(Boolean).join(' · ')) + '</div></div>'
      + '<span class="when">' + esc(agoT(d.now - h.ts)) + '</span><span></span></button>').join('')
    + '<div class="qmore"><a class="lnk" href="#runs" data-filter="finished">View history</a></div>'
    : '<p class="empty">No previous sessions recorded yet.</p>';

  const c = d.counters;
  $('counters').innerHTML = [
    ['Deduped events', c.deduped], ['Dedup re-armed', c.dedup_cleared], ['Echoes dropped', c.echo_dropped],
    ['Allowlist rejected', c.allowlist_rejected], ['Queued intake events', D.drain ? D.drain.queued : null],
    ['Open subjects (advertised)', d.live.length],
  ].map(([k, v]) => '<div class="tile"><span>' + esc(k) + '</span><b>' + esc(v == null ? 'n/a' : fmtNum(v)) + '</b></div>').join('');

  paintRuns(d, D);
  paintQueue(d, D);
  paintFleet(d.fleet);
  paintDetail();
}

function paintRuns(d, D){
  const all = allRuns(d, D), n = g => all.filter(r => r.grp === g).length;
  $('r-seg').innerHTML = segHtml('runs', runsFilter, [['all', 'All'], ['running', 'Running ' + n('running')],
    ['waiting', 'Waiting ' + n('waiting')], ['finished', 'Finished']]);
  const rows = all.filter(r => (runsFilter === 'all' || r.grp === runsFilter) && (!runsQ
    || (r.key + ' ' + runName(r.key, r.w).ref + ' ' + ((r.w && r.w.title) || '')).toLowerCase().includes(runsQ)));
  const sel = rows.find(r => runsSel && r.key === runsSel.key && (r.past || null) === runsSel.past) || rows[0] || null;
  const cap = PHONE.matches && !runsAll && rows.length > RUNS_PHONE + 2 ? RUNS_PHONE : rows.length;
  $('r-rows').innerHTML = rows.length ? rows.slice(0, cap).map(r => '<tr tabindex="0" class="pick' + (r === sel ? ' sel' : '') + '" data-pick="' + esc(r.key) + '"'
      + (r.past ? ' data-ppast="' + esc(r.past) + '"' : '') + ' data-grp="' + r.grp + '">'
      + '<td><div class="t">' + runLabel(r.key, r.w) + '</div></td>'
      + '<td class="m-sub"><span class="stw">' + stIcon(r.k) + esc(r.word) + '</span></td>'
      + '<td class="m-hide nw">' + esc(r.model || '-') + '</td>'
      + '<td class="num m-hide">' + esc(r.started != null ? stamp(r.started) : '-') + '</td>'
      + '<td class="num m-end">' + esc(r.time != null ? dur(r.time) : r.started != null ? agoT(d.now - r.started) : '-') + '</td>'
      + '<td class="num r m-hide">' + esc(r.tokens != null ? kfmt(r.tokens) : '-') + '</td></tr>').join('')
      + (cap < rows.length ? '<tr class="more"><td colspan="6"><button class="lnk" id="r-all">Show ' + esc(rows.length - cap) + ' more</button></td></tr>' : '')
    : '<tr><td colspan="6" class="empty">No runs match.</td></tr>';
  const more = (d.history_total || 0) > (d.history || []).length;
  $('r-foot').innerHTML = '<span>' + esc('Showing ' + rows.length + ' runs' + (d.stream_enabled ? ', ' + (d.history || []).length + ' of '
      + (d.history_total || 0) + ' recorded sessions' : '')) + '</span>'
    + (more ? '<button class="lnk" id="h-more">Load ' + esc(Math.min(HIST_PAGE, d.history_total - d.history.length)) + ' more</button>' : '');
  if(!picking('r-side')) $('r-side').innerHTML = sel ? runCard(sel, false) : '<p class="sub">Pick a run to see its details.</p>';
  if(runSheet && !picking('run-sheet')){
    const sel = all.find(r => r.key === runSheet.key && (r.past || null) === runSheet.past);
    runSheet.sheet.content.innerHTML = sel ? runCard(sel, false) : '<p class="sub">This run is no longer listed.</p>';
  }
}

function paintQueue(d, D){
  const paused = !!(D.drain && D.drain.enabled);
  $('q-drain').innerHTML = drainHead(D.drain, true);
  $('q-n').textContent = D.queue.length;
  $('q-sub').textContent = queueWhy({}, d) + '. Next eligible selection first, then the configured dispatch policy. Running work is not interrupted.';
  $('q-rows').innerHTML = D.queue.length ? D.queue.slice(0, qLimit).map((q, i) => '<tr><td class="num m-hide">' + (i + 1) + '</td>'
      + '<td><span class="t">' + runLabel(q.key) + '</span></td>'
      + '<td class="m-hide">' + esc((title(q.key).src || '-') + (q.kind === 'rerun' ? ' (rerun)' : '')) + '</td>'
      + '<td class="num m-sub">' + esc(q.events + (q.events === 1 ? ' event' : ' events')) + '<div class="s2' + (queueWhy(q, d) === queueWhy({}, d) ? ' g' : '') + '">' + esc(queueWhy(q, d)) + '</div></td>'
      + '<td class="num m-end">' + esc(q.kind === 'slot' ? 'Waiting for slot' : agoT(q.arrived_age)) + '</td>'
      + '<td>' + (q.key === d.next_subject ? '<span class="tag">Next eligible</span>' : can && q.movable
        ? actBtn('Move to front', 'move-to-front', {subject_key: q.key}, {done: 'Selected ' + runName(q.key).ref + ' as next eligible'}) : '')
      + '</td></tr>').join('')
    : '<tr><td colspan="6" class="empty">Nothing queued.</td></tr>';
  $('q-foot').hidden = D.queue.length <= qLimit;
  $('q-foot').innerHTML = '<button class="lnk" id="q-more">Show all ' + esc(D.queue.length) + '</button>';
  $('q-cap').innerHTML = '<b>Capacity</b><div class="capv"><b>' + esc(D.used) + '</b><span class="sub">'
    + esc(D.cap != null ? 'of ' + D.cap + ' slots in use' : 'workers running') + '</span></div>' + pips(D.used, D.cap);
  $('q-wait-n').textContent = D.waiting.length;
  $('q-wait-drain').hidden = !paused;
  const qcap = PHONE.matches && !qAll && D.waiting.length > RUNS_PHONE + 2 ? RUNS_PHONE : D.waiting.length;
  $('q-wait').innerHTML = D.waiting.length ? D.waiting.slice(0, qcap).map(x => stageRow(x, d, true, true)).join('')
    + (qcap < D.waiting.length ? '<div class="more"><button class="lnk" id="q-all">Show ' + esc(D.waiting.length - qcap) + ' more</button></div>' : '')
    : '<p class="empty">Nothing waiting.</p>';
  $('q-src-n').textContent = D.queue.length + D.waiting.length;
  $('q-src').innerHTML = bars(countBy([...D.queue, ...D.waiting], q => title(q.key).src || 'other'));
}

// ---- views. Hash routes include sub-tabs; old view-only bookmarks use their default tab.
const VIEWS = {now: 'v-now', runs: 'v-runs', queue: 'v-queue', fleet: 'fleet-view', settings: 'v-settings', run: 'v-run'};
const SUBTABS = {now: ['running', 'queue', 'recent'], queue: ['waiting', 'next', 'source'], settings: ['workers', 'groups', 'modules']};
function showSubtab(view, tab){
  for(const name of SUBTABS[view] || []){
    const active = name === tab, a = $('tab-' + view + '-' + name);
    $(view + '-' + name).hidden = !active;
    a.setAttribute('aria-selected', active);
    a.tabIndex = active ? 0 : -1;
  }
}
document.addEventListener('keydown', e => {
  const tab = e.target.closest('[role=tab]');
  if(!tab || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
  const tabs = [...tab.closest('[role=tablist]').querySelectorAll('[role=tab]')], i = tabs.indexOf(tab);
  const next = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1
    : (i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
  e.preventDefault(); tabs[next].focus(); location.hash = tabs[next].hash;
});
function showTab(v){
  syncDetailRoute();
  for(const [k, id] of Object.entries(VIEWS)) $(id).hidden = k !== v;
  const navLink = document.querySelector('#tabbar [data-view="' + (v === 'run' ? 'runs' : v) + '"], header nav [data-view="' + v + '"]');
  $('vt').textContent = v === 'run' ? 'Run' : v === 'settings' ? 'Settings' : navLink ? navLink.textContent.trim() : '';
  const nav = v === 'run' ? 'runs' : v;
  document.querySelectorAll('[data-view]').forEach(a => a.setAttribute('aria-current', a.dataset.view === nav ? 'page' : 'false'));
  if(v !== 'run' && selected){ closeStream(); selected = null; selectedPast = null; }
}
function route(){
  const h = location.hash.slice(1), m = /^run\/([^/]+)(?:\/([\w-]{1,64}))?$/.exec(h);
  if(m){
    let key = null;
    try{ key = decodeURIComponent(m[1]); }catch(e){}
    if(key){ select(key, m[2] || null); return; }
  }
  const parts = h.split('/'), v = VIEWS[parts[0]] && parts[0] !== 'run' ? parts[0] : 'now';
  showTab(v);
  if(SUBTABS[v]) showSubtab(v, SUBTABS[v].includes(parts[1]) ? parts[1] : SUBTABS[v][0]);
  if(v === 'runs') runsFilter = ['all', 'running', 'waiting', 'finished'].includes(parts[1]) ? parts[1] : 'all';
  if(v === 'fleet') fleetFilter = ['all', 'standing', 'tfa'].includes(parts[1]) ? parts[1] : 'all';
  if(lastState){ paintRuns(lastState, lastDerived); paintFleet(lastState.fleet); }
}
window.addEventListener('hashchange', route);
// Phone: an open agent transcript takes the whole screen (no tab bar under its composer).
function detailFlag(){ document.body.toggleAttribute('data-detail', /^module\/[^/]+\/./.test(location.hash.slice(1))); }
window.addEventListener('hashchange', detailFlag);
detailFlag();
$('d-title').addEventListener('click', e => e.currentTarget.classList.toggle('open'));
$('n-strip').addEventListener('click', e => { const c = e.target.closest('[data-go]'); if(c && PHONE.matches) location.hash = c.dataset.go; });
PHONE.addEventListener('change', () => { if(lastState) paint(lastState); });

function paintDetail(){
  if(!selected || !lastState) return;
  const d = lastState;
  const row = allRuns(d, lastDerived).find(r => r.key === selected && (r.past || null) === selectedPast)
    || {key: selected, past: selectedPast, k: 'off', word: 'Ended', grp: 'finished'};
  const nm = runName(selected, row.w);
  $('d-ref').textContent = nm.ref;
  $('d-title').textContent = nm.title || nm.ref;
  $('d-badge').innerHTML = selectedPast ? badge('Previous session', 'done')
    : stateBadge(sessions(d).find(x => x.key === selected) || {key: selected, live: null, worker: null});
  if(!picking('d-stats')) $('d-stats').innerHTML = runCard(row, true);
  $('d-msg').hidden = !(can && !selectedPast && d.live.some(l => l.key === selected));
  const prev = (d.history || []).filter(h => h.key === selected);
  $('d-prev-w').hidden = prev.length < 2;   // one session is just this run
  $('d-prev').innerHTML = prev.map(h => '<button class="row" data-key="' + esc(h.key) + '" data-past="' + esc(h.session) + '"'
    + (h.session === selectedPast ? ' aria-current="true"' : '') + '>' + stIcon(h.session === selectedPast ? 'run' : 'off')
    + '<div><div class="h">' + esc('Started ' + stamp(h.ts)) + '</div><div class="s">' + esc(agoT(d.now - h.ts)
      + (showHarness ? ' · session ' + h.session : '')) + '</div></div></button>').join('');
}

function select(key, past){
  past = past || null;
  const h = '#run/' + encodeURIComponent(key) + (past ? '/' + past : '');
  if(location.hash !== h) location.hash = h;   // the hashchange re-enters here and returns below
  showTab('run');
  if(key === selected && past === selectedPast && es) return;
  selected = key; selectedPast = past;
  paintDetail();
  $('sv-x').href = detailRoute && detailRoute.place ? detailRoute.place.hash : '#runs';
  focusDetailRoute($('d-title'));
  if(lastState && lastState.stream_enabled) openStream(key, past);
  else {
    closeStream(); resetLog();
    $('sv-log').appendChild(el('div', 'notice',
      'Live transcripts are turned off on this broker (BROKER_STREAM_ENABLED), so only state is shown.'));
    svStatus('stream off', '');
  }
}

// ---- live transcript (only reachable when /state says stream_enabled; the server enforces auth itself).
// Rendered as a chat/timeline; bookkeeping records fold away unless "Everything" is on.
let es = null, showHarness = false;
function svStatus(t, cls){ $('sv-st').textContent = t; $('sv-st').className = 'conn ' + (cls || ''); }
/* TRANSCRIPT_RENDERER */
const {addLine, resetLog, settleTools} = createTranscript($);
function closeStream(){ if(es){ es.close(); es = null; } }
function openStream(key, past){
  closeStream();
  resetLog(); svStatus('connecting', 'warn');
  // A previous session replays its recorded transcript once from /history/, then the server sends `end`
  const s = es = past ? new EventSource('/history/' + encodeURIComponent(key) + '/' + encodeURIComponent(past))
                      : new EventSource('/stream/' + encodeURIComponent(key));
  s.onopen = () => { resetLog(); svStatus(past ? 'loading' : 'live', past ? 'warn' : 'live'); };  // reconnect re-sends the backfill
  s.onmessage = e => addLine(e.data);
  s.addEventListener('end', () => { svStatus(past ? 'previous session' : 'worker ended', ''); settleTools(); s.close(); if(es === s) es = null; });
  s.onerror = () => {
    if(s.readyState === EventSource.CLOSED){ if(es === s) es = null; explainClose(key, past); }
    else svStatus('reconnecting', 'warn');
  };
}
// EventSource can't see the HTTP status of a refused connect, so ask /stream-status WHY and say it plainly
// (403 not authorized / 503 over BROKER_STREAM_MAX / 404 worker already ended) instead of a red catch-all.
async function explainClose(key, past){
  if(past){ svStatus('previous session unavailable', ''); return; }
  let j = null;
  try{ const r = await fetch('/stream-status/' + encodeURIComponent(key), {cache:'no-store'}); if(r.ok) j = await r.json(); }catch(e){}
  if(key !== selected || selectedPast || es) return;   // the operator moved on while we asked
  const st = j ? j.status : null;
  if(st === 403) svStatus('not authorized', 'bad');
  else if(st === 503) svStatus('stream limit reached (' + j.max + ')', 'warn');
  else if(st === 404){
    const h = ((lastState && lastState.history) || []).find(x => x.key === key);
    svStatus(h ? 'worker ended: open previous session' : 'worker ended', '');
    if(h){
      const b = el('button', 'notice', 'This worker has already ended. Open its previous session (' + h.session + ') →');
      b.onclick = () => select(key, h.session);
      $('sv-log').appendChild(b);
    }
  }
  else svStatus('stream closed', '');
}
// ---- actions: POST /actions/<verb> (actions.py). Drawn only when GET /actions says this viewer may act.
let can = false, settings = null;
const rerunModel = {};       // subject key -> model picked for "Run again" (survives the 5-second repaint)
const rerunOptions = new Map(); // current ticket row, not the row/model of a historical session
async function loadCan(){
  try{ const r = await fetch('/actions', {cache:'no-store'}); can = r.ok && (await r.json()).allowed === true; }catch(e){ can = false; }
  if(can) await loadSettings();
}
async function loadSettings(){
  try{ const r = await fetch('/actions/settings', {cache:'no-store'}); settings = r.ok ? await r.json() : null; }catch(e){ settings = null; }
  paintSettings();
}
const NATIVE = ['haiku', 'opus', 'sonnet'];
function opts(list, cur, dflt){
  return '<option value="">' + esc('Default' + (dflt ? ' (' + dflt + ')' : '')) + '</option>'
    + list.map(m => '<option' + (m === cur ? ' selected' : '') + '>' + esc(m) + '</option>').join('');
}
// Keep every focused control connected, not just model menus. Polls and async rerun options both repaint these boxes.
function picking(id){
  const a = document.activeElement;
  return !!(a && $(id).contains(a));
}
function rerunBlocked(key){
  const o = rerunOptions.get(key), pick = rerunModel[key];
  return !!(pick && (!o || o.loading || o.error || o.pinned || !settings || !settings.models.includes(pick)));
}
function modelPick(key){
  if(!settings) return '';
  let o = rerunOptions.get(key);
  if(!o || (!o.loading && Date.now() - o.at > 15000)){
    o = {loading: true, at: Date.now()}; rerunOptions.set(key, o);
    loadRerunOptions(key, o);
  }
  const ready = !o.loading && !o.error, disabled = !ready || o.pinned;
  return '<label><span class="control-label">Run again on:</span><select class="pick-sel' + (rerunModel[key] ? ' set' : '')
    + '" data-rr="' + esc(key) + '" aria-label="Run again on"' + (disabled ? ' disabled' : '') + '>'
    + '<option value="">' + esc(ready ? 'Row default (' + o.model + ')' : o.loading ? 'Loading current row…' : 'Model unavailable') + '</option>'
    + (rerunBlocked(key) ? '<option selected>' + esc(rerunModel[key]) + '</option>' : '')
    + (ready && !o.pinned ? settings.models.map(m => '<option' + (m === rerunModel[key] ? ' selected' : '') + '>'
      + esc(m) + '</option>').join('') : '') + '</select></label>';
}
async function loadRerunOptions(key, o){
  try{
    const r = await fetch('/actions/rerun/' + encodeURIComponent(tid(key)), {cache:'no-store'});
    const j = await r.json();
    Object.assign(o, r.ok ? j : {error: j.error || 'Could not read the current row.'});
  }catch(e){ o.error = 'Could not read the current row.'; }
  o.loading = false; o.at = Date.now();
  if(lastState) paint(lastState);
}
function rerunHint(key){
  const o = rerunOptions.get(key);
  return '<p class="sub">' + (!o || o.loading ? 'Checking the current row before offering a model override.'
    : o.error ? esc(o.error) : o.pinned ? esc('This row (' + o.row + ') is pinned to ' + o.model + '; change it in ')
      + '<a href="#settings/workers">Settings</a>.' : 'One-run override only. Row default follows current routing and ticket labels.')
    + (rerunBlocked(key) ? ' Selected override ' + esc(rerunModel[key]) + ' cannot be used until validated. '
      + '<button class="lnk" data-rerun-default="' + esc(key) + '">Use row default</button>' : '') + '</p>';
}
function paintSettings(){
  const s = settings;
  $('s-w').hidden = !(can && s);
  $('s-unavailable').hidden = !!(can && s);
  $('s-unavailable').textContent = can ? 'Worker and group settings could not be loaded.' : 'Worker and group settings require dashboard action access.';
  if(!can || !s) return;
  const backends = ['claude'].concat(s.models.filter(m => !NATIVE.includes(m)));
  const off = s.writable ? '' : ' disabled';
  $('s-sub').textContent = !s.writable ? 'Read-only: the broker cannot write its routing overrides file, so changes here cannot be saved.'
    : s.rejected ? 'The routing overrides file on disk is invalid, so the broker is using its last good copy. Fix the file by hand before editing here.'
    : 'Applies to new runs. Backend and harness are defaults: ticket labels can override them unless pinned. Blue outline = changed here; Default puts it back.';
  $('s-rows').innerHTML = s.rows.map(r => {
    const sel = (f, list, dflt) => '<label><span class="control-label">' + (f === 'backend' ? 'Backend' : 'Model')
      + '</span><select class="pick-sel' + (r.overridden.includes(f) ? ' set' : '') + '"' + off
      + ' data-set-row="' + esc(r.row) + '" data-field="' + f + '" aria-label="' + esc(r.row + ' ' + (f === 'backend' ? 'backend' : 'model')) + '">'
      + opts(list, r.overridden.includes(f) ? r[f] : null, dflt) + '</select></label>';
    return '<tr><td><div class="t">' + esc(r.row) + '</div><div class="sub">' + (r.pinned ? 'Pinned' : 'Overridable default') + '</div></td><td class="m-hide">' + esc(HARNESS[r.adapter] || r.adapter) + '</td>'
      + '<td data-l="Backend">' + sel('backend', backends, r.default_backend || 'by fleet mode') + '</td>'
      + '<td data-l="Model pin">' + sel('model_pin', s.models, r.default_model_pin || 'none') + '</td>'
      + '<td data-l="Policy"><label><span class="control-label">Pin row</span><select class="pick-sel' + (r.overridden.includes('pin') ? ' set' : '') + '"' + off
      + ' data-set-row="' + esc(r.row) + '" data-field="pin" aria-label="' + esc(r.row + ' pin') + '">'
      + opts(['false', 'true'], r.overridden.includes('pin') ? String(r.pin) : null, r.default_pin ? 'pinned' : 'overridable')
      + '</select></label></td></tr>';
  }).join('');
  $('s-caps').innerHTML = s.groups.map(g => '<tr><td><div class="t">' + esc(g.group) + '</div></td><td>'
    + '<label><span class="control-label">Max at once</span><input class="pick-sel num' + (g.overridden ? ' set' : '') + '" type="number" min="0" step="1" inputmode="numeric"' + off
    + ' data-cap="' + esc(g.group) + '" value="' + esc(g.cap ?? '') + '" placeholder="No cap" aria-label="' + esc('Max at once for ' + g.group) + '"></label>'
    + (g.overridden ? ' <button class="lnk" data-cap-reset="' + esc(g.group) + '">Default</button>' : '') + '</td></tr>').join('');
}
async function saveSetting(body, done){
  const r = await fetch('/actions/settings', {method: 'POST', cache: 'no-store', body: JSON.stringify(body),
    headers: {'Content-Type': 'application/json', 'X-Broker-Action': '1'}}).catch(e => null);
  const j = r ? await r.json().catch(() => ({})) : {};
  if(r && r.ok && j.settings){ settings = j.settings; rerunOptions.clear(); toast(done); }
  else {
    if(r && (r.status === 403 || r.status === 404)) can = false;
    toast(j.error || 'Failed: ' + (r ? 'HTTP ' + r.status : 'network error'), true);
  }
  paintSettings();
  if(lastState) paint(lastState);
}
$('s-rows').addEventListener('change', e => {
  const t = e.target.closest('[data-set-row]');
  if(!t) return;
  const row = t.dataset.setRow, f = t.dataset.field;
  const value = t.value === '' ? null : f === 'pin' ? t.value === 'true' : t.value;
  saveSetting({rows: {[row]: {[f]: value}}},
    row + ' ' + f + ': ' + (t.value || 'back to default') + '. New runs use it.');
});
$('s-caps').addEventListener('change', e => {
  const t = e.target.closest('[data-cap]');
  if(!t) return;
  const v = t.value.trim();
  if(v !== '' && !/^\d+$/.test(v)){ toast('Use a whole number, 0 or more', true); paintSettings(); return; }
  saveSetting({row_concurrency: {[t.dataset.cap]: v === '' ? null : Number(v)}},
    t.dataset.cap + ': ' + (v === '' ? 'back to default' : 'at most ' + v + ' at once'));
});
$('s-caps').addEventListener('click', e => {
  const b = e.target.closest('[data-cap-reset]');
  if(b) saveSetting({row_concurrency: {[b.dataset.capReset]: null}}, b.dataset.capReset + ': back to default');
});
document.addEventListener('change', e => {
  const t = e.target.closest('[data-rr]');
  if(!t) return;
  if(t.value) rerunModel[t.dataset.rr] = t.value; else delete rerunModel[t.dataset.rr];
  t.classList.toggle('set', !!t.value);
});
function toast(t, bad){
  const n = $('toast');
  n.textContent = t; n.className = 'toast' + (bad ? ' bad' : ''); n.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { n.hidden = true; }, bad ? 7000 : 3500);
}
function actAttrs(verb, body, done){
  return ' data-act="' + verb + '" data-body="' + esc(JSON.stringify(body)) + '" data-done="' + esc(done) + '"';
}
function actBtn(label, verb, body, o){
  return '<button class="btn' + (o.bad ? ' bad' : '') + (o.pickFor ? ' pri' : '') + '"' + actAttrs(verb, body, o.done)
    + (o.confirm ? ' data-confirm="' + esc(o.confirm) + '"' : '') + (o.pickFor ? ' data-model-key="' + esc(o.pickFor) + '"' : '')
    + (o.pickFor && rerunBlocked(o.pickFor) ? ' disabled' : '')
    + '>' + esc(label) + '</button>';
}
async function act(verb, body, done){
  try{
    const r = await fetch('/actions/' + verb, {method: 'POST', cache: 'no-store', body: JSON.stringify(body),
      headers: {'Content-Type': 'application/json', 'X-Broker-Action': '1'}});
    const j = await r.json().catch(() => ({}));
    if(!r.ok){
      if(r.status === 403 || r.status === 404){ can = false; paintSettings(); if(lastState) paint(lastState); }
      toast(j.error || 'Failed: HTTP ' + r.status, true);
      return false;
    }
    toast(done);
    tick();
    return true;
  }catch(e){ toast('Failed: ' + e.message, true); return false; }
}
$('d-msg').addEventListener('submit', async e => {
  e.preventDefault();
  const t = $('d-msg-t'), text = t.value.trim();
  if(!text || !selected) return;
  e.submitter && (e.submitter.disabled = true);
  if(await act('message', {subject_key: selected, text}, 'Sent. It shows in the conversation until the run reads it.')) t.value = '';
  e.submitter && (e.submitter.disabled = false);
});
$('d-msg-t').addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey && !e.isComposing){ e.preventDefault(); $('d-msg').requestSubmit(); }
});
// ---- end actions

document.addEventListener('click', e => {
  const a = e.target.closest('[data-act]');
  if(a){
    const mk = a.dataset.modelKey;
    // Guard stale controls as well as disabling the rendered button. Never drop an explicit override.
    if(a.disabled || (mk && rerunBlocked(mk))) return;
    if(!a.dataset.confirm || confirm(a.dataset.confirm)){
      a.disabled = true;
      const body = JSON.parse(a.dataset.body), pick = mk && rerunModel[mk];
      if(pick) body.model = pick;
      act(a.dataset.act, body, pick ? a.dataset.done + ' on ' + pick : a.dataset.done).then(ok => { if(ok && mk) delete rerunModel[mk]; })
        .finally(() => { a.disabled = !!(mk && rerunBlocked(mk)); });
    }
    return;
  }
  const reset = e.target.closest('[data-rerun-default]');
  if(reset){
    delete rerunModel[reset.dataset.rerunDefault];
    if(lastState) paint(lastState);
    return;
  }
  const s = e.target.closest('[data-seg]');
  if(s && s.dataset.seg === 'theme'){
    theme = THEMES.includes(s.dataset.v) ? s.dataset.v : 'auto';
    try{ localStorage.setItem('eph-theme', theme); }catch(e){}
    return applyTheme(theme);
  }
  if(s){
    if(s.dataset.seg === 'runs') runsFilter = s.dataset.v; else fleetFilter = s.dataset.v;
    location.hash = '#' + s.dataset.seg + '/' + s.dataset.v;
    if(lastState) paint(lastState);
    return;
  }
  const f = e.target.closest('a[data-filter]');
  if(f){ e.preventDefault(); runsFilter = f.dataset.filter; location.hash = '#runs/' + runsFilter; }
  const b = e.target.closest('[data-key]');
  if(b){ select(b.dataset.key, b.dataset.past); return; }
  // Explicit picks use a viewport sheet; a narrow screen opens transcripts directly.
  const p = e.target.closest('[data-pick]');
  if(p){
    if(matchMedia('(max-width:1100px)').matches && p.dataset.grp !== 'waiting'){
      pendingDetailPlace = rememberPlace(p); select(p.dataset.pick, p.dataset.ppast);
    }else {
      runsSel = {key: p.dataset.pick, past: p.dataset.ppast || null};
      const row = allRuns(lastState, lastDerived).find(r => r.key === runsSel.key && (r.past || null) === runsSel.past);
      if(row){
        const sheet = openDetailSheet('Run details', p, () => { runSheet = null; });
        sheet.content.id = 'run-sheet'; runSheet = {...runsSel, sheet};
      }
      paint(lastState);
    }
    return;
  }
  if(e.target.closest('#h-more')){ histLimit += HIST_PAGE; tick(); }
  if(e.target.closest('#q-more')){ qLimit = Infinity; paint(lastState); }
  if(e.target.closest('#r-all')){ runsAll = true; paint(lastState); }
  if(e.target.closest('#q-all')){ qAll = true; paint(lastState); }
});
$('r-q').addEventListener('input', e => { runsQ = e.target.value.trim().toLowerCase(); if(lastState) paint(lastState); });
function setHarness(on){
  showHarness = on;
  paintDetail();
  $('sv-log').classList.toggle('show-harness', on);
  $('hr').setAttribute('aria-pressed', on);
  $('cv').setAttribute('aria-pressed', !on);
}
$('hr').onclick = () => setHarness(true);
$('cv').onclick = () => setHarness(false);

// Shared detail surfaces: keep the list's place even when a poll replaces its rows (ticket 2809).
let detailSheet = null, detailRoute = null, pendingDetailPlace = null;
function rememberPlace(target){
  if(detailSheet && detailSheet.dialog.contains(target)) return detailSheet.place;
  const attrs = ['data-quick-module', 'data-quick-agent', 'data-pick', 'data-ppast', 'data-key', 'data-past', 'href'];
  const identity = attrs.filter(a => target.hasAttribute(a)).map(a => [a, target.getAttribute(a)]);
  const scope = target.closest('section') || document;
  return {x: scrollX, y: scrollY, hash: location.hash || '#now', find: () => target.isConnected ? target
    : [...scope.querySelectorAll(target.tagName)].find(n => identity.length && identity.every(([a, v]) => n.getAttribute(a) === v))};
}
function restorePlace(place){
  if(!place) return;
  // Module lists arrive asynchronously. Wait for their row, without retaining an unbounded observer.
  const restore = () => {
    if((location.hash || '#now') !== place.hash) return true;
    const target = place.find();
    if(target){ target.focus({preventScroll: true}); scrollTo({left: place.x, top: place.y, behavior: 'instant'}); }
    return !!target;
  };
  scrollTo({left: place.x, top: place.y, behavior: 'instant'});
  if(restore()) return;
  const observer = new MutationObserver(() => { if(restore()) observer.disconnect(); });
  observer.observe(document.querySelector('main'), {childList: true, subtree: true});
  setTimeout(() => observer.disconnect(), 5000);
}
function openDetailSheet(title, origin, cleanup = () => {}, ...headExtras){
  if(detailSheet) detailSheet.close();
  const dialog = el('dialog', 'detail-sheet'), head = el('div', 'detail-head');
  const heading = el('h2', null, title), close = el('button', 'btn', 'Close'), content = el('div', 'side');
  heading.id = 'detail-sheet-title'; dialog.setAttribute('aria-labelledby', heading.id);
  head.append(heading, ...headExtras, close); dialog.append(head, content);
  const sheet = {dialog, content, place: rememberPlace(origin), closed: false, close: (restore = true) => {
    if(sheet.closed) return;
    sheet.closed = true;
    cleanup(); dialog.close(); dialog.remove();
    document.body.classList.remove('detail-open');
    if(detailSheet === sheet) detailSheet = null;
    if(restore) restorePlace(sheet.place);
  }};
  close.onclick = () => sheet.close();
  dialog.addEventListener('cancel', e => { e.preventDefault(); sheet.close(); });
  dialog.addEventListener('close', () => sheet.close());
  document.body.appendChild(dialog); document.body.classList.add('detail-open');
  detailSheet = sheet; dialog.showModal(); close.focus({preventScroll: true});
  return sheet;
}
// Full transcripts retain their existing routes and phone layout. They share the same return-place contract.
document.addEventListener('click', e => {
  const origin = e.target.closest('[data-key], a[href]');
  if(origin && !detailRoute && (origin.hasAttribute('data-key') || /^#module\/[^/]+\/.+/.test(origin.hash || '')))
    pendingDetailPlace = rememberPlace(origin);
}, true);
function syncDetailRoute(){
  const full = /^#(?:run\/.+|module\/[^/]+\/.+)/.test(location.hash);
  if(full){
    if(!detailRoute) detailRoute = {place: pendingDetailPlace, hash: null};
    if(detailSheet) detailSheet.close(false);
    if(detailRoute.hash !== location.hash){ detailRoute.hash = location.hash; detailRoute.focus = true; scrollTo({top: 0}); }
  }else if(detailRoute){
    const place = detailRoute.place; detailRoute = null;
    requestAnimationFrame(() => { if(place && (location.hash || '#now') === place.hash) restorePlace(place); });
  }
  pendingDetailPlace = null;
}
function focusDetailRoute(target){
  if(detailRoute && detailRoute.focus){ target.tabIndex = -1; target.focus({preventScroll: true}); detailRoute.focus = false; }
}
window.addEventListener('hashchange', () => { if(detailSheet) detailSheet.close(false); });
document.addEventListener('keydown', e => {
  if(e.key === 'Escape' && !detailSheet && detailRoute){
    e.preventDefault();
    location.hash = detailRoute.place ? detailRoute.place.hash
      : location.hash.startsWith('#run/') ? '#runs' : location.hash.split('/').slice(0, 2).join('/');
  }
  const row = e.target.closest('tr[data-pick], tr[data-key]');
  if(row && e.target === row && ['Enter', ' '].includes(e.key)){ e.preventDefault(); row.click(); }
});
async function tick(){
  try{
    const r = await fetch('/state' + (histLimit > HIST_PAGE ? '?hist=' + histLimit : ''), {cache:'no-store'});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    paint(await r.json());
    if(!booted){ booted = true; route(); }   // a deep link waits for the first state
    $('err').style.display = 'none';
  }catch(e){
    $('dot').className = 'dot bad';
    $('live').classList.add('bad');
    document.querySelectorAll('.upd').forEach(u => u.classList.add('bad'));
    $('err').textContent = 'state fetch failed: ' + e.message;
    $('err').style.display = '';
  }
}
loadCan().then(tick);
setInterval(tick, 5000);
</script>
</body>
</html>
"""


# Optional module UI is inserted only for authorized modules. With none configured
# the returned HTML is exactly PAGE, including the original navigation and script.
PAGE = (PAGE.replace('/* TRANSCRIPT_TEXT_HELPERS */', transcript_basic())
        .replace('/* TRANSCRIPT_FORMAT */', transcript_format())
        .replace('/* TRANSCRIPT_MARKDOWN */', transcript_text())
        .replace('/* TRANSCRIPT_RENDERER */', transcript_renderer())
        .replace('/* TRANSCRIPT_CSS */', transcript_css()))


def module_page(modules):
    if not modules:
        return PAGE
    config = json.dumps(modules).replace("<", "\\u003c").replace("&", "\\u0026")
    page = PAGE.replace("function route(){\n", "function route(){\n  if(moduleRoute()) return;\n")
    # A compact button beside the agent name, so it does not crowd the name or push the role line down.
    page = page.replace("</style>", ".ag .an [data-quick-module]{height:26px;min-width:0;padding:0 10px;margin-left:10px;"
                        "font-size:12.5px;vertical-align:1px}\n.detail-sheet.quick .detail-head{gap:12px}"
                        ".detail-sheet.quick .detail-head h2{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
                        ".detail-sheet.quick .detail-head .lnk{display:inline-flex;align-items:center;white-space:nowrap}"
                        ".detail-sheet.quick .detail-head .btn{min-width:0;padding:0 14px}\n"
                        "@media (max-width:720px){.tbl td [data-quick-module]{display:inline-flex}"
                        # Phone: the quick view rises from the bottom edge, where the thumb already is.
                        ".detail-sheet.quick{width:100%;max-width:100%;margin:auto 0 0;border-radius:16px 16px 0 0;"
                        "border-bottom:0;max-height:calc(100dvh - 48px)}}\n</style>")
    page = page.replace("function toggleAgent(e){\n",
                        "function toggleAgent(e){\n  if(e.target.closest('[data-quick-module]')) return;\n")
    page = page.replace("+ '<td><div class=\"an\"><b>' + esc(a.name ?? '?') + '</b>'",
                        "+ '<td><div class=\"an\"><b>' + esc(a.name ?? '?') + '</b>' + moduleAffordance(name)")
    return page.replace("loadCan().then(tick);", MODULE_JS.replace("__MODULE_CONFIG__", config) + "\nloadCan().then(tick);")


MODULE_JS = r"""
// Modules are trusted site-installed code, native DOM, not iframes.
const viewerModules = __MODULE_CONFIG__;
const viewerRuntime = new Map();
let viewerStatus = {}, viewerBusy = false, quickCleanup = null, quickAbort = null, quickName = null, quickStatus = null, quickSheet = null;
for(const m of viewerModules){
  const key = 'module-' + m.name, section = el('section');
  section.id = 'v-' + key; section.hidden = true;
  document.querySelector('main').appendChild(section);
  VIEWS[key] = section.id;
  for(const nav of [document.querySelector('header nav'), $('tabbar')]){
    const a = el('a', null, nav.id === 'tabbar' ? null : m.label);
    if(nav.id === 'tabbar'){
      a.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4V5Z"/></svg>';
      a.appendChild(el('span', null, m.label));
    }
    a.href = '#module/' + m.name; a.dataset.view = key; nav.appendChild(a);
  }
  viewerRuntime.set(m.name, {section, index: new Set(), cleanup: null, abort: null, token: null, js: null});
}
// What Fleet knows about an agent, for module views: status, its NOW line, model and context.
function agentSummary(name){
  const a = ((lastState && lastState.fleet && lastState.fleet.agents) || []).find(x => String(x.name) === name);
  if(!a) return null;
  const [state, label] = agentStatus(a), m = effModel(a);
  return {state, label, doing: a.ledger_source === 'now' && a.ledger ? String(a.ledger) : '', model: m.label || '',
    context_tokens: a.context_state === 'ok' && a.ctx_tokens != null ? a.ctx_tokens : null,
    context_limit: a.context_ceiling || a.context_window || null,
    detail: [a.agent_type === 'tfa' ? 'Temporary' : '', HARNESS[a.runtime] || (a.runtime === 'claude' ? 'Claude Code' : a.runtime)]
      .filter(Boolean).join(' · ')};
}
function viewerContext(m, signal, agentName){
  return {baseUrl: '/viewer/' + m.name + '/', canAct: can, signal, agentName, agentSummary,
    openAgent: name => { location.hash = '#module/' + m.name + '/' + encodeURIComponent(name); }};
}
function moduleRoute(){
  const match = /^module\/([a-z][a-z0-9_]*)(?:\/(.*))?$/.exec(location.hash.slice(1));
  const m = match && viewerModules.find(x => x.name === match[1]);
  if(!m) return false;
  showTab('module-' + m.name);
  renderViewer(m);
  return true;
}
function clearViewer(r){
  if(r.abort) r.abort.abort();
  if(typeof r.cleanup === 'function'){ try{ r.cleanup(); }catch(e){} }
  r.cleanup = null; r.token = null; r.section.replaceChildren();
}
function downViewer(m, s, error){
  const r = viewerRuntime.get(m.name);
  clearViewer(r);
  const card = el('div', 'card side');
  card.appendChild(el('h2', null, 'Agent viewer is down'));
  card.appendChild(el('p', 'sub', (s.state || 'down') + ' since ' +
    (s.since ? new Date(s.since * 1000).toLocaleString() : 'unknown') +
    '. Restarts: ' + (s.restarts || 0)));
  card.appendChild(el('p', null, error || s.last_error || 'Waiting for the module.'));
  if(can && m.child){
    const b = el('button', 'btn', 'Reload');
    b.onclick = async () => { b.disabled = true; await act('module_reload', {name: m.name}, 'Reload requested'); await pollViewers(); };
    card.appendChild(b);
  }
  r.section.appendChild(card);
  if(!r.section.hidden) focusDetailRoute(card);
}
async function viewerCode(m, s){
  const r = viewerRuntime.get(m.name), version = (s.version || '') + ':' + s.restarts + ':' + s.since;
  if(r.requestedVersion !== version){
    r.requestedVersion = version; r.codeVersion = null; r.js = null;
    r.codeFailures = 0; r.codeRetryAt = 0;
  }
  if(r.js) return r.js;
  if(Date.now() < r.codeRetryAt) throw new Error('Viewer load retry pending.');
  // Browsers cache failed imports by URL. Each retry needs a fresh URL, with
  // shared in-flight loads and exponential backoff capped at one minute.
  r.codeAttempt = (r.codeAttempt || 0) + 1;
  const pending = import('/viewer/' + m.name + '/ui.js?v=' + encodeURIComponent(version) + '&attempt=' + r.codeAttempt);
  r.js = pending;
  try{
    const js = await pending;
    if(r.js === pending){ r.codeVersion = version; r.codeFailures = 0; r.codeRetryAt = 0; }
    return js;
  }catch(e){
    if(r.js === pending){
      r.js = null; r.codeVersion = null; r.codeFailures++;
      r.codeRetryAt = Date.now() + Math.min(60000, 5000 * 2 ** Math.min(r.codeFailures - 1, 4));
    }
    throw e;
  }
}
async function renderViewer(m){
  const r = viewerRuntime.get(m.name), s = viewerStatus[m.name] || {};
  if(s.state !== 'up'){ downViewer(m, s); return; }
  if(r.section.hidden) return;
  let agentName;
  try{ agentName = decodeURIComponent(location.hash.split('/').slice(2).join('/')) || null; }catch(e){ agentName = null; }
  const token = JSON.stringify([s.version, s.restarts, s.since, agentName, can]);
  if(r.token === token) return;
  clearViewer(r); r.token = token; r.abort = new AbortController();
  const abort = r.abort;
  const content = el('div'); r.section.appendChild(content);
  if(agentName){ content.setAttribute('role', 'region'); content.setAttribute('aria-label', m.label + ': ' + agentName); }
  try{
    const js = await viewerCode(m, s);
    if(abort.signal.aborted) return;
    const cleanup = await js.mount(content, viewerContext(m, abort.signal, agentName));
    if(abort.signal.aborted){ if(typeof cleanup === 'function') cleanup(); }
    else { r.cleanup = cleanup; if(agentName) focusDetailRoute(content); }
  }catch(e){ if(!abort.signal.aborted) downViewer(m, s, 'Could not load viewer: ' + e.message); }
}
function moduleAffordance(name){
  const m = viewerModules.find(m => viewerRuntime.get(m.name).index.has(name));
  return m ? '<button class="btn" data-quick-module="' + esc(m.name) + '" data-quick-agent="' + esc(name) + '">Quick view</button>' : '';
}
function closeQuickViewer(){
  if(quickSheet) quickSheet.close();
}
$('f-rows').addEventListener('click', async e => {
  const button = e.target.closest('[data-quick-module]');
  if(!button) return;
  const m = viewerModules.find(m => m.name === button.dataset.quickModule), name = button.dataset.quickAgent;
  closeQuickViewer(); quickAbort = new AbortController(); const abort = quickAbort;
  quickName = m.name; quickStatus = JSON.stringify(viewerStatus[m.name]);
  const full = el('a', 'lnk', 'Full view ›');
  full.href = '#module/' + m.name + '/' + encodeURIComponent(name);
  quickSheet = openDetailSheet(name, button, () => {
    abort.abort();
    if(typeof quickCleanup === 'function'){ try{ quickCleanup(); }catch(e){} }
    quickCleanup = null; quickAbort = null; quickName = null; quickSheet = null;
  }, full);
  quickSheet.dialog.classList.add('quick');
  const content = el('div'); quickSheet.content.appendChild(content);
  try{
    const js = await viewerCode(m, viewerStatus[m.name]);
    if(abort.signal.aborted) return;
    const cleanup = await js.quickView(content, name, viewerContext(m, abort.signal, name));
    if(abort.signal.aborted){ if(typeof cleanup === 'function') cleanup(); }
    else quickCleanup = cleanup;
  }catch(e){ if(!abort.signal.aborted) content.textContent = 'Quick view unavailable: ' + e.message; }
});
async function pollViewers(){
  if(viewerBusy) return;
  viewerBusy = true;
  try{
    const r = await fetch('/viewer-status', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    viewerStatus = await r.json();
  }catch(e){ viewerStatus = {}; }
  paintModuleSettings();
  if(quickName && quickStatus !== JSON.stringify(viewerStatus[quickName])) closeQuickViewer();
  await Promise.all(viewerModules.map(async m => {
    const runtime = viewerRuntime.get(m.name), s = viewerStatus[m.name] || {};
    runtime.index.clear();
    renderViewer(m);
    if(s.state === 'up'){
      try{
        const r = await fetch('/viewer/' + m.name + '/fleet-index', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
        const names = r.ok ? await r.json() : [];
        if(Array.isArray(names)) runtime.index = new Set(names.filter(n => typeof n === 'string'));
      }catch(e){}
    }
  }));
  if(lastState) paintFleet(lastState.fleet);
  viewerBusy = false;
}
function paintModuleSettings(){
  const box = $('s-modules'); box.replaceChildren();
  for(const m of viewerModules){
    const row = el('div'), info = el('div'), s = viewerStatus[m.name] || {};
    info.appendChild(el('b', null, m.label));
    info.appendChild(el('p', 'sub', 'State: ' + (s.state || 'unknown') + '. Disable or enable via the configured disabled-file marker.'));
    row.append(el('span'), info, el('span'));
    if(can && m.child){
      const reload = el('button', 'btn', 'Reload');
      reload.onclick = async () => { reload.disabled = true; await act('module_reload', {name: m.name}, 'Reload requested'); await pollViewers(); };
      row.appendChild(reload);
    }
    box.appendChild(row);
  }
}
window.addEventListener('hashchange', () => {
  closeQuickViewer();
  for(const r of viewerRuntime.values()) if(r.section.hidden) clearViewer(r);
});
pollViewers(); setInterval(pollViewers, 5000);
"""


# ---------------------------------------------------------------- selftest


def _selftest():
    """Exercises snapshot() against a fake broker. The shapes it reads (nested dicts, a Worker-ish
    object, absent attrs) are exactly what a real one has, but nothing here spawns anything."""
    class W:
        pass

    class Co:
        pass

    class Ad:
        pass

    class B:
        pass

    b = B()
    b.coalescer, b.echo_guard, b.rejected = Co(), Co(), 3
    b.coalescer.lock = __import__("threading").Lock()
    b.coalescer.deduped, b.coalescer.dedup_cleared = 7, 2
    b.coalescer.live = {"vikunja:task:1": {"state": "open_running", "forwards": 2,
                                           "events": [1, 2, 3], "fwd_buffer": [1], "expire": time.time() + 30}}
    b.coalescer.pending = {"vikunja:task:2": {"events": [1], "spawn_at": time.time() + 5}}
    b.echo_guard.dropped = 4
    b.adapter = Ad()
    b.adapter._lock = __import__("threading").Lock()
    w = W(); w.run_id = "r1"; w.session_id = "abcdef1234567890"; w.turns = 3
    w.tokens = 1500; w.forwards = 2; w.in_turn = True; w.done = False
    w.turn_started = time.time() - 12
    b.adapter._workers = {"vikunja:task:1": w}

    d = snapshot(b, cap=3)

    assert d["concurrency"] == {"cap": 3, "in_use": 1}, d["concurrency"]
    assert d["live"][0]["state"] == "open_running" and d["live"][0]["buffered"] == 1
    assert d["pending"][0]["spawn_in"] > 0
    assert d["workers"][0]["session"] == "abcdef12", "session_id must be truncated to 8"
    assert len(d["workers"][0]["session"]) == 8
    assert d["workers"][0]["turn_age"] > 11
    assert d["counters"] == {"deduped": 7, "dedup_cleared": 2, "echo_dropped": 4,
                             "allowlist_rejected": 3}, d["counters"]
    # event bodies must never ride the view; counts only
    assert "events" not in d["live"][0] or isinstance(d["live"][0]["events"], int)
    import json
    json.dumps(d)                                   # must be JSON-serializable end to end
    assert d["stream_enabled"] is stream.enabled()
    assert "<!DOCTYPE html>" in PAGE and "/state" in PAGE and "EventSource" in PAGE
    # fleet parsing
    assert model_label("claude-opus-5-5") == "opus 5.5" and model_label("local-vllm") == "local"
    assert context_percent(13) == 13 and context_percent("41%") == 41 and context_percent("x") is None
    assert ledger_excerpt("- _note_") == "note" and hist_param("/state?hist=50") == 50
    assert len(d["history"]) <= HISTORY_PAGE and "history_total" in d
    print("dash selftest: OK")


if __name__ == "__main__":
    _selftest()
