"""Site overlay. Code defaults are neutral; a deployment's own values live in config/broker.yaml ($BROKER_CONFIG,
see read_config) and/or the older KEY=VALUE file $BROKER_SITE_ENV or config/site.env next to this module (private;
the OSS export leaves both out and ships the .example files; NOT export-ignore, CI builds the RPM from `git archive`).
Loaded on first import with setdefault: the real environment (systemd drop-ins, tests) always wins, then broker.yaml
settings, then site.env. Import this before any module that reads env at import time."""
import json
import logging
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
SITE_ENV = os.getenv("BROKER_SITE_ENV") or os.path.join(_HERE, "config", "site.env")
# The single config file: $BROKER_CONFIG or config/broker.yaml (a .json path is read as JSON, no PyYAML needed).
BROKER_CONFIG = os.getenv("BROKER_CONFIG") or os.path.join(_HERE, "config", "broker.yaml")
# Top-level keys of broker.yaml. `settings` feeds the environment; every other key is the content of the
# config/<key>.json file it replaces (routes: the rows list), except `capabilities` (routing.load_site_capabilities),
# which has no file. Anything else is a typo and stops the broker.
SECTIONS = frozenset({"settings", "routes", "capabilities", "constraints", "windows", "provenance", "inbox-accounts",
                      "zulip-wake", "cd-tier", "deploy-paths", "pool-vikunja-ids"})


class BrokerConfigError(ValueError):
    """broker.yaml is present but invalid. Raised at import, so the broker refuses to start."""


def _yaml_load(text):
    try:
        import yaml
    except ImportError:
        raise BrokerConfigError("reading a YAML config needs PyYAML (python3-pyyaml); "
                                "or point BROKER_CONFIG at a .json file")

    class _Strict(yaml.SafeLoader):
        pass

    def _mapping(loader, node, deep=False):
        keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
        odd = [k for k in keys if not isinstance(k, str)]
        if odd:   # 1 and true would collide in the dict, and JSON has string keys only
            raise BrokerConfigError(f"key(s) {odd} must be strings, quote them (line {node.start_mark.line + 1})")
        dup = sorted({k for k in keys if keys.count(k) > 1})
        if dup:   # safe_load keeps the last silently; a repeated key is almost always a paste error
            raise BrokerConfigError(f"duplicate key(s) {dup} (line {node.start_mark.line + 1})")
        return loader.construct_mapping(node, deep=deep)

    _Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    try:
        return yaml.load(text, Loader=_Strict)
    except yaml.YAMLError as e:
        raise BrokerConfigError(f"not valid YAML ({e})")


def _json_no_dups(pairs):
    """object_pairs_hook for json.loads: a repeated key is refused, the same as in YAML."""
    keys = [k for k, _ in pairs]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise BrokerConfigError(f"duplicate key(s) {dup}")
    return dict(pairs)


def read_config(path=None):
    """-> {section: value} from broker.yaml. Missing default file = {}; an explicit BROKER_CONFIG that does not
    exist, a parse error, a non-mapping or an unknown section -> BrokerConfigError (message names the file)."""
    path = path or BROKER_CONFIG
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        if os.getenv("BROKER_CONFIG") and path == os.getenv("BROKER_CONFIG"):
            raise BrokerConfigError(f"BROKER_CONFIG={path} does not exist")
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise BrokerConfigError(f"{path}: unreadable ({e})")
    try:
        if path.endswith(".json"):
            try:
                doc = json.loads(text, object_pairs_hook=_json_no_dups)
            except BrokerConfigError:
                raise
            except ValueError as e:
                raise BrokerConfigError(f"not valid JSON ({e})")
        else:
            doc = _yaml_load(text)
    except BrokerConfigError as e:
        raise BrokerConfigError(f"{path}: {e}")
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise BrokerConfigError(f"{path}: expected a mapping of sections")
    extra = sorted(str(k) for k in doc if k not in SECTIONS)
    if extra:
        raise BrokerConfigError(f"{path}: unknown section(s) {extra} (known: {', '.join(sorted(SECTIONS))})")
    empty = sorted(k for k, v in doc.items() if v is None)
    if empty:   # `constraints:` with nothing under it would otherwise fall back to the json file silently
        raise BrokerConfigError(f"{path}: section(s) {empty} are empty; remove them or fill them in")
    try:        # plain data only, so every later copy (section()) can't fail: YAML reads 2026-10-18 as a date
        return json.loads(json.dumps(doc))
    except (TypeError, ValueError) as e:
        raise BrokerConfigError(f"{path}: a value isn't plain data, quote dates and times ({e})")


def _setting_value(key, v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float, str)):
        return str(v)
    # Scalars only for now: a list-shaped knob keeps its own separator (":" or ","), written as one string
    raise BrokerConfigError(f"{BROKER_CONFIG}: settings.{key} must be a string, number or true/false")


def load_settings(doc):
    """setdefault every `settings:` entry into the environment (the real environment wins). Returns the keys set."""
    settings = doc.get("settings") or {}
    if not isinstance(settings, dict):
        raise BrokerConfigError(f"{BROKER_CONFIG}: settings must be a mapping of NAME: value")
    out = []
    for k, v in settings.items():
        if not (isinstance(k, str) and k.isidentifier()):
            raise BrokerConfigError(f"{BROKER_CONFIG}: settings key {k!r} is not a variable name")
        v = _setting_value(k, v)
        if k not in os.environ:
            os.environ[k] = v
            out.append(k)
    return out


CONFIG = read_config()


def section(name, config=None):
    """The broker.yaml section `name` (a fresh copy), or None when the file doesn't set it. Callers fall back to
    their config/<name>.json file on None and log that the file is ignored otherwise."""
    if name not in SECTIONS:
        raise KeyError(name)
    doc = CONFIG if config is None else config
    return json.loads(json.dumps(doc[name])) if name in doc else None


def load(path=SITE_ENV):
    """setdefault every KEY=VALUE line of `path` (blank lines and # comments skipped, one layer of matching
    quotes stripped). Missing file = no-op. Returns the keys it set."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = (s.strip() for s in ln.split("=", 1))
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k.isidentifier() and k not in os.environ:
            os.environ[k] = v
            out.append(k)
    return out


# An explicit BROKER_SITE_ENV that does not exist is a typo, not "no site": refuse to start
# (a silent no-op would drop every site allowlist/deny). If the default file is missing (an OSS install), warn once.
# broker.yaml settings load first, so they win over site.env; the real environment wins over both.
if not os.path.exists(SITE_ENV):
    if os.getenv("BROKER_SITE_ENV"):
        raise FileNotFoundError(f"BROKER_SITE_ENV={SITE_ENV} does not exist")
    if not CONFIG:
        logging.getLogger("eph-broker.site_env").warning(
            "no %s or %s: neutral defaults (see config/broker.example.yaml)", BROKER_CONFIG, SITE_ENV)
load_settings(CONFIG)
load()


def ticket_url(task_id):
    """Link for a tracker ticket: TICKET_URL_TEMPLATE (`{}` = global id) or plain `#<id>` when unset.
    Plain substitution, never str.format: a stray brace in the template must not raise mid-notify."""
    t = os.getenv("TICKET_URL_TEMPLATE", "")
    return t.replace("{}", str(task_id)) if "{}" in t else f"#{task_id}"


def ticket_id_regex():
    """Regex whose group 1 is the ticket id in a TICKET_URL_TEMPLATE link (scheme-less, so http/https both
    match); None when the template is unset or has no `{}`."""
    t = os.getenv("TICKET_URL_TEMPLATE", "")
    if "{}" not in t:
        return None
    head = re.sub(r"^[a-z]+://", "", t.split("{}", 1)[0])
    return re.compile(re.escape(head) + r"(\d+)")


def gitlab_web_url():
    """GitLab web root (no trailing /) from GITLAB_WEB_URL; "" when unset."""
    return os.getenv("GITLAB_WEB_URL", "").rstrip("/")


def gitlab_api_url():
    """GitLab REST root: GITLAB_API, else GITLAB_WEB_URL + /api/v4, else "" (GitLab features then fail closed)."""
    web = gitlab_web_url()
    return os.getenv("GITLAB_API") or (web + "/api/v4" if web else "")


_BARE_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


def bad_push_hosts():
    """BROKER_PUSH_HOSTS entries that can never match a push URL's hostname (scheme, port, path, userinfo,
    wildcard...): the guard compares bare lowercase hostnames, so these silently allow nothing."""
    return sorted(h for h in push_hosts() if not _BARE_HOST.fullmatch(h))


def push_hosts():
    """Allowlist: the ONLY hosts a worker branch may be pushed to (BROKER_PUSH_HOSTS, comma list).
    Unset/empty = nothing may be pushed (fail closed)."""
    return frozenset(h.strip().lower() for h in os.getenv("BROKER_PUSH_HOSTS", "").split(",") if h.strip())
