"""runtime_settings.py: validated live broker policy, with environment values as startup defaults.

Only the explicit registry below can change. Paths, credentials and routing identities
remain bootstrap. Parse the entire candidate before publishing any values; a bad edit
keeps the previous policy. Routing overrides remain the final per-row authority.
"""
import copy
import importlib
import json
import logging
import math
import os
import re
import shutil
import sys
from dataclasses import replace

import site_env

log = logging.getLogger("eph-broker.settings")


def text(value):
    return site_env._setting_value("runtime", value)


def flag(value):
    value = text(value).strip().lower()
    if value not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
        raise ValueError("expected a boolean")
    return value in ("1", "true", "yes", "on")


def number(value, minimum=0, integral=False):
    if isinstance(value, bool):
        raise ValueError("expected a number")
    n = float(value)
    if not math.isfinite(n) or n < minimum or (integral and n != int(n)):
        raise ValueError("invalid number")
    return int(n) if integral else n


def positive(value):
    n = number(value)
    if n <= 0:
        raise ValueError("expected a positive number")
    return n


def integer(value):
    return number(value, minimum=1, integral=True)


def bounded(maximum):
    def parse(value):
        n = number(value)
        if n > maximum:
            raise ValueError("out of range")
        return n
    return parse


def choice(*values):
    def parse(value):
        value = text(value).strip().lower()
        if value not in values:
            raise ValueError("unknown choice")
        return value
    return parse


def csv(value):
    return [s.strip() for s in text(value).split(",") if s.strip()]


def window_map(value):
    data = json.loads(text(value))
    if not isinstance(data, dict):
        raise ValueError("expected window map")
    return {k: number(v) for k, v in data.items()}


def row_caps(value):
    for item in csv(value):
        name, sep, n = item.partition("=")
        if not sep or not name.strip() or name.strip() != name.strip().lower():
            raise ValueError("expected lowercase group=integer")
        number(n.strip(), integral=True)
    return text(value)


def repo_allowlist(value):
    paths = set(text(value).split(":")) - {""}
    if any(not os.path.isabs(p) for p in paths):
        raise ValueError("expected absolute repo paths")
    return paths


# name -> (module, attribute, parser). These globals are read at use time in the existing code.
# Avoid module reload: that would lose queues, locks, durable state and running worker identities.
GLOBALS = {
    "app": {
        "CONCURRENCY": ("CONCURRENCY", integer), "BROKER_MAX_BODY": ("MAX_BODY_BYTES", integer),
        "TFA_DEDUP_S": ("TFA_DEDUP_S", number), "CD_RETRY_PENDING_TTL_S": ("CD_RETRY_PENDING_TTL_S", positive),
    },
    "coalescer": {name: (name, parser) for name, parser in (
        ("INITIAL_DEFAULT", number), ("RECEIVE_DEFAULT", number), ("TICK", positive),
        ("MAX_SPAWNS_PER_HOUR", integer), ("MAX_COMPLETED", integer), ("DM_HISTORY_MAX_CHARS", integer))},
    "adapters.cc": {name: (name, parser) for name, parser in (
        ("TURN_CAPTURE_MAX_CHARS", integer), ("TURN_CAPTURE_KEEP", integer), ("TURN_CAPTURE_MAX_AGE_S", positive),
        ("LOCAL_WALL_CLOCK_MULT", positive), ("HARDKILL_REPROMPT_CAP_S", positive),
        ("TURN_CAP_PUBLISH_CLEAN_COMMIT", flag), ("USAGE_CAP_FALLBACK_ENABLED", flag),
        ("USAGE_CAP_FALLBACK_CHAIN", text), ("USAGE_CAP_SEVEN_DAY_THRESHOLD", bounded(100)),
        ("USAGE_CAP_FIVE_HOUR_THRESHOLD", bounded(100)), ("MR_REVIEWER_OF_RECORD", text))},
    "routing": {
        "GENERAL_REPO_ALLOWLIST": ("GENERAL_REPO_ALLOWLIST", repo_allowlist),
        "CODE_REPO_ALLOWLIST": ("CODE_REPO_ALLOWLIST", repo_allowlist),
        "CODE_REPO_RUNNERS": ("CODE_REPO_RUNNERS", lambda v: _runner_map(v)),
        "ISSUE_REPO_MAP": ("ISSUE_REPO_MAP", lambda v: _routing_parse("parse_issue_repo_map", v)),
        "ISSUE_AUTHOR_ALLOWLIST": ("ISSUE_AUTHOR_ALLOWLIST", lambda v: _routing_parse("parse_issue_author_allowlist", v)),
        "NATIVE_FALLBACK_MODEL": ("_NATIVE_FALLBACK_MODEL", text),
        "ISSUE_TRIGGERING_EVENTS": ("ISSUE_TRIGGERING_EVENTS", lambda v: set(csv(v))),
        **{name: (name, text) for name in ("MR_REVIEW_QUICK_PATHS", "MR_REVIEW_QUICK_MODEL",
            "MR_REVIEW_QUICK_MODEL_CODEX", "MR_REVIEW_STANDARD_MODEL", "MR_REVIEW_FULL_MODEL")},
        **{name: (name, integer) for name in ("MR_REVIEW_FULL_DIFF_LINES", "MR_REVIEW_QUICK_MAX_DIFF_LINES")},
    },
    "gitlab_mr": {"GITLAB_MR_TIMEOUT": ("_HTTP_TIMEOUT", positive)},
    "cd": {"CD_WORKER_BRANCH_PREFIX": ("WORKER_BRANCH_PREFIX", text), "CD_RETRY_MAX": ("_RETRY_MAX", integer)},
    "notify": {"NOTIFY_BACKEND": ("NOTIFY_BACKEND", choice("zbus", "webhook", "email")),
        "NOTIFY_SMTP_STARTTLS": ("NOTIFY_SMTP_STARTTLS", flag)},
    "adapters.codex": {"CODEX_REASONING_SUMMARY": ("REASONING_SUMMARY",
        choice("", "auto", "concise", "detailed", "none"))},
    "events": {"EVENTS_WEBHOOK_KINDS": ("EVENTS_WEBHOOK_KINDS", lambda v: _event_kinds(v))},
    "repo_catalog": {
        "REPO_CATALOG_TTL_S": ("CACHE_TTL_S", number), "REPO_CATALOG_HTTP_TIMEOUT": ("HTTP_TIMEOUT_S", positive),
        "REPO_SUBPROJECT_REPOS": ("SUBPROJECT_REPOS", csv),
        "REPO_SUBPROJECT_STOPWORDS": ("SUBPROJECT_STOPWORDS", lambda v: set(s.lower() for s in csv(v))),
    },
    "repo_infer": {
        "REPO_WEAK_NAMES": ("WEAK_NAMES", lambda v: set(s.lower() for s in csv(v))),
        **{env: (attr, parser) for env, attr, parser in (
            ("REPO_INFER_SHORTLIST_K", "SHORTLIST_K", integer), ("REPO_INFER_LAYA_TIMEOUT", "LAYA_TIMEOUT_S", positive),
            ("REPO_INFER_LAYA_TOP", "LAYA_TOP", bounded(1)), ("REPO_INFER_LAYA_MARGIN", "LAYA_MARGIN", bounded(1)),
            ("REPO_INFER_LAYA_TEXT_CHARS", "LAYA_TEXT_CHARS", integer),
            ("REPO_INFER_4B_MODEL", "QWEN_MODEL", text), ("REPO_INFER_4B_TIMEOUT", "QWEN_TIMEOUT_S", positive),
            ("REPO_INFER_4B_MAX_TOKENS", "QWEN_MAX_TOKENS", integer),
            ("REPO_INFER_4B_TEXT_CHARS", "QWEN_TEXT_CHARS", integer))},
    },
    "mention_handoff": {"MENTION_HANDOFF_DEDUP_S": ("DEDUP_S", number),
        "MENTION_HANDOFF_PROJECTS": ("MENTION_HANDOFF_PROJECTS", lambda v: frozenset(integer(s) for s in csv(v)))},
}
GLOBALS["adapters.cc"].update({
    "API_OVERLOAD_RETRY_DELAYS_S": ("API_OVERLOAD_RETRY_DELAYS_S", lambda v: tuple(number(s) for s in csv(v))),
    "WORKER_ENV_PASSTHROUGH": ("_WORKER_ENV_PASSTHROUGH", lambda v: _passthrough(v)),
})
for _name, _builtin in (("MR_REVIEW_FULL_PATHS", "_MR_REVIEW_FULL_PATHS_BUILTIN"),
                        ("MR_REVIEW_QUICK_EXCLUDE_PATHS", "_MR_REVIEW_QUICK_EXCLUDE_BUILTIN"),
                        ("MR_REVIEW_SKIP_PATHS", "_MR_REVIEW_SKIP_PATHS_BUILTIN")):
    GLOBALS["routing"][_name] = (_name, lambda v, builtin=_builtin: _add_patterns(v, builtin))


def _add_patterns(value, builtin):
    import routing
    return ",".join(dict.fromkeys(csv(getattr(routing, builtin)) + csv(value)))


def _event_kinds(value):
    import events
    values = csv(value) or events.KINDS
    if set(values) - set(events.KINDS):
        raise ValueError("unknown event kind")
    return frozenset(values)


def _passthrough(value):
    from adapters.cc import _parse_env_passthrough
    if any(any(word in name.upper() for word in ("TOKEN", "SECRET", "KEY", "PASSWORD")) for name in csv(value)):
        raise ValueError("secret-shaped passthrough name")
    return _parse_env_passthrough(text(value))


def _routing_parse(name, value):
    import routing
    if name == "parse_issue_repo_map" and any(not routing.parse_issue_repo_map(s) for s in csv(value)):
        raise ValueError("invalid issue repo map")
    return getattr(routing, name)(text(value))


def _runner_map(value):
    import routing
    from adapters.cc import _CODE_TEST_RUNNERS
    for item in csv(value):
        path, sep, key = item.partition("=")
        if not sep or not os.path.isabs(path.strip()) or key.strip() not in _CODE_TEST_RUNNERS:
            raise ValueError("invalid repo runner map")
    return {**routing._CODE_REPO_RUNNER_SEEDS, **routing.parse_repo_runner_map(text(value))}


# Already read on demand. Publishing their validated strings makes them live without new getter paths.
ENVIRONMENT = {name: flag for name in (
    "CD_ENABLED", "CD_ACCEPT_HERMES", "MR_ROUTINE_OPS_DM", "COMMENT_RESPAWN_ENABLED", "MENTION_HANDOFF_ENABLED",
    "REPO_INFER_ENABLED", "REPO_INFER_SHADOW", "REPO_INFER_COMMENT_RERUN_ENABLED", "REPO_PUSH_BROAD_CRED_ENABLED",
    "CODE_JUDGED_BASH_ENABLED", "CODE_NONCLAUDE_OK", "EPH_WIKI_SKILLS_ENABLED",
    "EPH_WRITE_GRANTS_ENABLED", "CC_SHOW_THINKING_SUMMARIES", "CODEX_ALLOW_PRERELEASE", "CODEX_SANDBOX_NETWORK",
    "BROKER_STREAM_ENABLED", "BROKER_ACTIONS_ENABLED", "BROKER_STEER_MID_TURN", "ZULIP_WAKE_ENABLED",
    "ZULIP_AUTOREPLY_ENABLED",
)}
ENVIRONMENT.update({name: text for name in (
    "CD_REPO_ALLOWLIST", "AUTO_MERGE_REPOS", "CONTINUE_MR_PREFIXES", "MR_NOTE_AUTHOR_ALLOWLIST",
    "MODEL_LABEL_TRUSTED_CREATORS", "REPO_INFER_RERUN_USERS", "REPO_DENY", "REPO_TIER_B",
    "TICKET_URL_TEMPLATE", "DASH_INSTANCE_NAME", "ZULIP_WAKE_SENDERS", "ZULIP_WAKE_BOT_SENDERS",
    "ZULIP_AUTOREPLY_EXCLUDE", "ZULIP_AUTOREPLY_TEXT", "CODEX_BACKEND")})
ENVIRONMENT.update({name: number for name in (
    "DISPATCH_METADATA_WAIT", "ZULIP_AUTOREPLY_WINDOW_S", "ZULIP_RECONCILE_S", "ZULIP_MAX_AGE_S",
    "ZULIP_HISTORY_TIMEOUT_S", "ZULIP_REGISTRY_TTL_S", "ZULIP_BOT_CACHE_TTL_S")})
ENVIRONMENT.update({name: positive for name in (
    "AUTO_MERGE_POLL_S", "ZULIP_WAKE_POLL", "ZULIP_AUTOREPLY_POLL", "BROKER_STREAM_MAX_S")})
ENVIRONMENT.update({name: integer for name in ("AUTO_MERGE_POLLS", "EPH_WRITE_GRANT_TTL_S", "BROKER_STREAM_MAX")})
ENVIRONMENT.update({"MR_ITERATION_MAX": lambda v: number(v, integral=True), "ROW_CONCURRENCY": row_caps,
    "DISPATCH_PRIORITY": choice("reviews-first", "fifo"), "BROKER_HMAC_MODE": choice("off", "log", "enforce")})
ENVIRONMENT.update({"AGENT_DM_RECIPIENTS": lambda v: _recipients(v), "AGENT_DM_DENY": lambda v: _recipients(v)})
ENVIRONMENT["BROKER_PUSH_HOSTS"] = lambda v: _push_hosts(v)
ENVIRONMENT["WORKER_SANDBOX"] = lambda v: _sandbox_mode(v)
ENVIRONMENT["EPH_GATE_NONSECRET_PATHS"] = lambda v: _nonsecret_paths(v)
ENVIRONMENT["EPH_GATE_TEST_ENV_ALLOW"] = lambda v: _passthrough(v)


def _sandbox_mode(value):
    mode = choice("bwrap", "off")(value)
    if mode == "bwrap" and not shutil.which("bwrap"):
        raise ValueError("bubblewrap unavailable")
    return mode


def _nonsecret_paths(value):
    from adapters.hermes_gate import gate
    mapping = json.loads(text(value))
    if not isinstance(mapping, dict):
        raise ValueError("expected repo-to-globs object")
    for repo, globs in mapping.items():
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)+", repo):
            raise ValueError("expected exact repo key")
        gate.nonsecret_globs(globs)
    return text(value)


def _push_hosts(value):
    if any(not site_env._BARE_HOST.fullmatch(host.lower()) for host in csv(value)):
        raise ValueError("expected bare push hosts")
    return text(value)


def _recipients(value):
    from adapters.hermes_gate.dm import recipients
    recipients(text(value))
    return text(value)

# Row policy uses the original template identity, not row_key: an already-resolved route must not get
# startup fields reapplied when model labels/tiering create a replacement Route.
ROWS = (
    ("_ANSIBLE_ROW", "ANSIBLE", "ANSIBLE"), ("_WIKI_DOCS_ROW", "WIKI_DOCS", "WIKI_DOCS"),
    ("_INBOX_ROW", "INBOX_TRIAGE", "INBOX_TRIAGE"), ("_ZULIP_INBOX_ROW", "INBOX_CHAT", "INBOX_CHAT"),
    ("_CODE_ROW", "CODE_CHANGE", "CODE_CHANGE"), ("_ISSUE_CODE_ROW", "ISSUE_CODE", "ISSUE_CODE"),
    ("_CODE_UI_ROW", "CODE_UI", None),   # UI browser harness is fixed to cc
    ("_INFRA_ROW", "INFRA", "INFRA"), ("_INFRA_GENERAL_ROW", "INFRA_GENERAL", "INFRA_GENERAL"),
    ("_MR_REVIEW_ROW", "MR_REVIEWER", "MR_REVIEW"), ("_GENERAL_ROW", "GENERAL", "GENERAL"),
    ("_TFA_ROW", "TFA", None),
)


class RuntimeSettings:
    def __init__(self, broker):
        self.broker = broker
        self.targets, self.parsers, self.defaults = {}, dict(ENVIRONMENT), {}
        for module, specs in GLOBALS.items():
            obj = sys.modules[broker.__class__.__module__] if module == "app" else importlib.import_module(module)
            for name, (attr, parser) in specs.items():
                self.targets[name] = (obj, attr)
                self.parsers[name] = parser
                self.defaults[name] = copy.deepcopy(getattr(obj, attr))
        import routing
        self.rows = {}
        for attr, backend, adapter in ROWS:
            route = getattr(routing, attr, None)  # site-only rows are absent in the public export
            if route is None:
                continue
            fields = {backend + "_BACKEND": ("backend", text)}
            if adapter:
                fields[adapter + "_ADAPTER"] = ("adapter", choice(*routing.ADAPTERS))
            fields[backend + "_PIN"] = ("pin", flag)
            if backend == "MR_REVIEWER":
                fields["MR_REVIEWER_MODEL"] = ("model_pin", text)
            runner = {"CODE_CHANGE": "CODE", "CODE_UI": "CODE_UI", "ISSUE_CODE": "ISSUE_CODE",
                      "INFRA": "INFRA"}.get(backend)
            if runner:
                from adapters.cc import _CODE_TEST_RUNNERS
                fields[runner + "_TEST_RUNNER"] = ("test_runner", choice("", *_CODE_TEST_RUNNERS))
            if backend == "GENERAL":
                fields[backend + "_SETTING_SOURCES"] = ("setting_sources", text)
            self.rows[id(route)] = (route, fields)
            self.parsers.update({name: parser for name, (_, parser) in fields.items()})
        self.environment = {name: os.environ.get(name) for name in self.parsers}
        self.current = {}
        self.model_denylist_default = copy.deepcopy(site_env.CONFIG.get("model_denylist", []))
        self.constraint_defaults = {name: copy.deepcopy(getattr(broker.adapter, name)) for name in (
            "max_turns", "max_turns_by_capability", "wall_clock_s", "wall_clock_by_capability",
            "inbox_max_input_tokens", "inbox_max_cost_usd", "model_prices", "max_forwards", "rotate_turns", "rotate_tokens")}
        self.progress_default = broker.liveness.progress_timeout
        self.window_defaults = {name: copy.deepcopy(getattr(importlib.import_module("coalescer"), name))
                                for name in ("INITIAL_WINDOWS", "RECEIVE_WINDOWS")}
        self.parsers.update({name: window_map for name in self.window_defaults})
        self.environment.update({name: os.environ.get(name) for name in self.window_defaults})

    def apply(self, doc):
        settings = doc.get("settings", {})
        try:
            if not isinstance(settings, dict):
                raise ValueError("settings must be a mapping")
            denylist = doc.get("model_denylist", self.model_denylist_default)
            if not isinstance(denylist, list) or any(not isinstance(s, str) or not s.strip() for s in denylist):
                raise ValueError("invalid model_denylist")
            parsed = {}
            for name, value in settings.items():
                if name not in self.parsers:
                    # Bootstrap settings may coexist with runtime policy; never apply them live.
                    if self.environment.get(name, os.environ.get(name)) != text(value):
                        log.warning("settings reload: %s is bootstrap/unknown; restart required", name)
                    continue
                try:
                    parsed[name] = self.parsers[name](value)
                except (ValueError, TypeError, OverflowError):
                    raise ValueError("invalid " + name) from None
            from routing_overrides import backend_ok, RoutingOverrides
            row_values = {}
            for ident, (route, fields) in self.rows.items():
                changes = {field: parsed[name] for name, (field, _) in fields.items() if name in parsed}
                if changes.get("backend") and changes["backend"] != "claude" and not backend_ok(changes["backend"]):
                    raise ValueError("unresolvable row backend")
                if changes.get("model_pin"):
                    RoutingOverrides._pin_name(changes["model_pin"])
                replace(route, **changes)  # validate cross-field harness constraints before publishing
                row_values[ident] = changes
        except (ValueError, TypeError, OverflowError) as exc:
            log.error("settings reload: %s; keeping previous settings", exc)
            return False
        for name in set(self.current) | set(parsed):
            if name in self.targets:
                obj, attr = self.targets[name]
                setattr(obj, attr, parsed.get(name, copy.deepcopy(self.defaults[name])))
            if name not in parsed:
                value = self.environment[name]
            elif type(parsed[name]) is bool:
                value = "1" if parsed[name] else "0"
            elif isinstance(parsed[name], (int, float)):
                value = str(parsed[name])
            else:
                value = text(settings[name])
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        import routing
        routing._RUNTIME_ROWS = row_values
        site_env.CONFIG = {**site_env.CONFIG, "model_denylist": copy.deepcopy(denylist)}
        import coalescer
        for name, default in self.window_defaults.items():
            value = {**default, **parsed.get(name, {})}
            section = "initial" if name == "INITIAL_WINDOWS" else "receive"
            value.update(self.broker.config.data.get("windows", {}).get(section, {}))
            setattr(coalescer, name, value)
        # Hermes can be imported lazily after a reload; use the same live multiplier in either case.
        if "adapters.hermes" in sys.modules:
            from adapters import cc
            sys.modules["adapters.hermes"].LOCAL_WALL_CLOCK_MULT = cc.LOCAL_WALL_CLOCK_MULT
        self.current = parsed
        self.broker.semaphore.resize(self.broker_concurrency())
        return True

    def broker_concurrency(self):
        return self.targets["CONCURRENCY"][0].CONCURRENCY

    def apply_constraints(self, cfg):
        try:
            values = constraints(cfg, self.constraint_defaults)
        except (TypeError, ValueError, OverflowError):
            log.error("settings reload: invalid constraints; keeping previous constraints")
            return False
        from liveness import resolve_progress_timeout
        progress = resolve_progress_timeout(cfg.get("progress_timeout_s", self.progress_default),
                                            values["wall_clock_s"], values["wall_clock_by_capability"])
        # Raise the backstop first so it cannot kill a newly widened turn budget.
        self.broker.liveness.progress_timeout = progress
        for name, value in values.items():
            setattr(self.broker.adapter, name, value)
        self.broker.coalescer.max_forwards = values["max_forwards"]
        return True


def constraints(cfg, defaults):
    if not isinstance(cfg, dict):
        raise ValueError("expected constraints mapping")
    values = copy.deepcopy(defaults)
    for name in values:
        if name not in cfg:
            continue
        v = cfg[name]
        if name in ("wall_clock_s", "inbox_max_cost_usd"):
            values[name] = positive(v)  # durations remain floats, including sub-second budgets
        elif name in ("max_turns_by_capability", "wall_clock_by_capability"):
            parse = positive if name.startswith("wall_clock") else integer
            if not isinstance(v, dict):
                raise ValueError("expected capability mapping")
            values[name] = {**defaults[name], **{k: parse(n) for k, n in v.items()}}
        elif name == "model_prices":
            from adapters.cc import _InboxUsage
            if not isinstance(v, dict):
                raise ValueError("expected price mapping")
            for model, prices in v.items():
                if (not isinstance(model, str) or not model or not isinstance(prices, dict)
                        or set(prices) != set(_InboxUsage.TOKEN_FIELDS)):
                    raise ValueError("invalid model prices")
                for rate in prices.values():
                    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                        raise ValueError("invalid price rate")
                    number(rate)
            values[name] = copy.deepcopy(v)
        else:
            values[name] = integer(v)
    if "progress_timeout_s" in cfg:
        positive(cfg["progress_timeout_s"])
    return values
