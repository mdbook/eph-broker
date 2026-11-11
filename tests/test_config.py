#!/usr/bin/env python3
"""broker.yaml self-tests (site_env.read_config / section / load_settings and the loaders that read a section).

stdlib assert only, same runner shape as test_routes.py. Import-time behaviour (settings precedence, refusing a bad
file) runs in a subprocess with its own environment; everything else patches site_env.CONFIG and restores it.
"""
import ast, hashlib, json, logging, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["BROKER_SITE_ENV"] = os.path.join(HERE, "fixtures", "site.test.env")
os.environ["BROKER_PROVENANCE_PATH"] = os.path.join(HERE, "fixtures", "provenance.test.json")
os.environ["BROKER_CONFIG"] = os.path.join(HERE, "fixtures", "broker.test.json")
os.environ["ROUTES_PATH"] = "/nonexistent/eph-test/routes.json"
import site_env

import routing
import provenance
import inbox_accounts

PASS = []
REPO = "/srv/git/sample-app"          # in both allowlists of tests/fixtures/site.test.env
try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


def _write(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(text)
    return p


def _probe(env, code, root=ROOT):
    """Run `code` after `import site_env` in a fresh interpreter whose environment is exactly `env` (+PATH)."""
    full = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": root, **env}
    return subprocess.run([sys.executable, "-c", "import site_env, os\n" + code], env=full, cwd=root,
                          capture_output=True, text=True, timeout=60)


class patched_config:
    """with patched_config({...}): site_env.CONFIG is that doc; explicit-path env vars named in `unset` are cleared."""
    def __init__(self, doc, unset=()):
        self.doc, self.unset = doc, unset

    def __enter__(self):
        self.saved = site_env.CONFIG
        self.env = {k: os.environ.pop(k) for k in self.unset if k in os.environ}
        site_env.CONFIG = self.doc

    def __exit__(self, *a):
        site_env.CONFIG = self.saved
        os.environ.update(self.env)


def test_no_file_is_empty_and_sets_nothing():
    with tempfile.TemporaryDirectory() as d:
        assert site_env.read_config(os.path.join(d, "broker.yaml")) == {}
        p = _probe({"BROKER_SITE_ENV": os.devnull,
                    "BROKER_CONFIG": os.path.join(HERE, "fixtures", "broker.test.json")},
                   "print(site_env.CONFIG, os.getenv('CONCURRENCY'))")
        assert p.returncode == 0 and p.stdout.strip() == "{} None", (p.stdout, p.stderr)
    assert site_env.CONFIG == {} and site_env.section("routes") is None
    ok("no broker.yaml: empty config, no settings, every section None (behaviour unchanged)")


def test_settings_precedence_env_then_yaml_then_site_env():
    with tempfile.TemporaryDirectory() as d:
        cfg = _write(d, "broker.json", json.dumps({"settings": {"ZZ_A": "yaml", "ZZ_B": "yaml", "ZZ_C": True,
                                                                "ZZ_D": 7, "ZZ_E": False}}))
        site = _write(d, "site.env", "ZZ_A=site\nZZ_B=site\nZZ_S=site\n")
        p = _probe({"BROKER_CONFIG": cfg, "BROKER_SITE_ENV": site, "ZZ_A": "env"},
                   "print(*(os.environ.get(k) for k in ('ZZ_A', 'ZZ_B', 'ZZ_C', 'ZZ_D', 'ZZ_E', 'ZZ_S')))")
        assert p.returncode == 0, p.stderr
        assert p.stdout.split() == ["env", "yaml", "1", "7", "0", "site"], p.stdout
    ok("settings: real env wins, then broker.yaml, then site.env; true/false become 1/0, numbers become strings")


def test_bad_files_refuse_to_start():
    with tempfile.TemporaryDirectory() as d:
        cases = {
            "missing": (os.path.join(d, "nope.yaml"), "does not exist"),
            "unknown": (_write(d, "u.json", json.dumps({"setings": {}})), "unknown section"),
            "not-map": (_write(d, "l.json", "[1, 2]"), "mapping of sections"),
            "bad-json": (_write(d, "b.json", "{"), "not valid JSON"),
            "list-setting": (_write(d, "s.json", json.dumps({"settings": {"X": [1]}})), "string, number"),
            "bad-key": (_write(d, "k.json", json.dumps({"settings": {"1X": "a"}})), "not a variable name"),
        }
        cases["empty"] = (_write(d, "e.json", json.dumps({"constraints": None})), "are empty")
        cases["dup-json"] = (_write(d, "dj.json", '{"settings": {"X": "a", "X": "b"}}'), "duplicate key")
        cases["dup-json-top"] = (_write(d, "dt.json", '{"windows": {}, "windows": {}}'), "duplicate key")
        if HAVE_YAML:
            cases["dup"] = (_write(d, "d.yaml", "settings:\n  X: a\n  X: b\n"), "duplicate key")
            cases["bad-yaml"] = (_write(d, "y.yaml", "settings: [\n"), "not valid YAML")
            cases["date"] = (_write(d, "t.yaml", "windows:\n  initial: {a: 2026-10-18}\n"), "quote dates")
            cases["empty-yaml"] = (_write(d, "n.yaml", "constraints:\n"), "are empty")
            cases["int-key"] = (_write(d, "i.yaml", "windows:\n  1: a\n  true: b\n"), "must be strings")
        for name, (path, want) in cases.items():
            p = _probe({"BROKER_CONFIG": path, "BROKER_SITE_ENV": os.devnull}, "")
            assert p.returncode != 0 and want in p.stderr and "BrokerConfigError" in p.stderr, (name, p.stderr)
        if HAVE_YAML:   # quoted, 1 and true are just two keys
            assert site_env.read_config(_write(d, "k.yaml", "windows:\n  '1': a\n  'true': b\n")) == {
                "windows": {"1": "a", "true": "b"}}
    ok("a missing explicit BROKER_CONFIG, unknown or empty section, non-mapping, parse error, list setting, bad "
       "key, a repeated key (JSON or YAML), a (YAML) non-string key or an unquoted date stops the broker at import")


def test_yaml_and_json_read_the_same():
    if not HAVE_YAML:
        return ok("YAML parity skipped (no PyYAML)")
    with tempfile.TemporaryDirectory() as d:
        doc = {"settings": {"A": "x"}, "windows": {"initial": {"vikunja:task": 3}}, "routes": [{"id": "r"}]}
        y = _write(d, "c.yaml", "settings:\n  A: x\nwindows:\n  initial: {'vikunja:task': 3}\nroutes:\n  - id: r\n")
        j = _write(d, "c.json", json.dumps(doc))
        assert site_env.read_config(y) == site_env.read_config(j) == doc
    ok("a .yaml and a .json config with the same content read the same")


def test_model_denylist_config():
    with tempfile.TemporaryDirectory() as d:
        for invalid in ("family", {}, [1], [""], ["   "], None):
            path = _write(d, "deny.json", json.dumps({"model_denylist": invalid}))
            try:
                site_env.read_config(path)
                raise AssertionError(f"accepted invalid model_denylist: {invalid!r}")
            except site_env.BrokerConfigError:
                pass
        for valid in ([], ["restricted-family", "other.model"]):
            doc = {"model_denylist": valid}
            assert site_env.read_config(_write(d, "deny.json", json.dumps(doc))) == doc
    with patched_config({"model_denylist": ["other.model"]}):
        site_env.require_model_allowed("OTHERxMODEL")  # literal substring, not a regex
        try:
            site_env.require_model_allowed("prefix-OTHER.MODEL-v2")
            raise AssertionError("case-insensitive substring must refuse")
        except site_env.ModelDenied:
            pass
    with patched_config({}):
        site_env.require_model_allowed("any-model")
    ok("model_denylist: validates lists, literal case-insensitive matching, unrestricted when omitted")


def test_default_config_selection_without_pyyaml():
    with tempfile.TemporaryDirectory() as d:
        shutil.copyfile(os.path.join(ROOT, "site_env.py"), os.path.join(d, "site_env.py"))
        # Shadow even an installed PyYAML: default JSON must work in a stdlib-only installation.
        _write(d, "yaml.py", 'raise ImportError("no PyYAML in this test")\n')
        cdir = os.path.join(d, "config"); os.makedirs(cdir)
        env = {"BROKER_SITE_ENV": os.devnull}
        code = "print(os.path.basename(site_env.BROKER_CONFIG), site_env.CONFIG)"
        p = _probe(env, code, root=d)
        assert p.returncode == 0 and p.stdout.strip() == "broker.yaml {}", (p.stdout, p.stderr)
        cfg = _write(cdir, "broker.json", '{"model_denylist": ["restricted-family"]}')
        p = _probe(env, code, root=d)
        assert p.returncode == 0 and p.stdout.strip() == "broker.json {'model_denylist': ['restricted-family']}", (
            p.stdout, p.stderr)
        _write(cdir, "broker.yaml", 'model_denylist: ["other-family"]\n')
        p = _probe(env, code, root=d)
        assert p.returncode != 0 and "BrokerConfigError" in p.stderr and "needs PyYAML" in p.stderr, p.stderr
        p = _probe(dict(env, BROKER_CONFIG=cfg), code, root=d)
        assert p.returncode == 0 and "restricted-family" in p.stdout, (p.stdout, p.stderr)
        p = _probe(dict(env, BROKER_CONFIG=os.path.join(d, "missing.json")), code, root=d)
        assert p.returncode != 0 and "does not exist" in p.stderr, p.stderr
    ok("default config: YAML before JSON, JSON starts without PyYAML, no config is a no-op; explicit paths win "
       "and missing explicit paths refuse startup")


def test_model_denylist_only_config_preserves_json_config():
    import app
    with tempfile.TemporaryDirectory() as d:
        cdir = os.path.join(d, "config"); os.makedirs(cdir)
        # Include every JSON-backed section and an extra legacy file: none should be replaced.
        names = site_env.SECTIONS - {"settings", "capabilities", "model_denylist"}
        want = {name: {"initial": {}} if name == "windows" else {"from": name} for name in names}
        want["legacy"] = {"unchanged": True}
        for name, value in want.items():
            _write(cdir, name + ".json", json.dumps(value))
        cfg = _write(d, "broker.json", '{"model_denylist": ["restricted-family"]}')
        doc = site_env.read_config(cfg)
        assert doc == {"model_denylist": ["restricted-family"]}
        if HAVE_YAML:
            ycfg = _write(d, "broker.yaml", '# Site launch policy only.\nmodel_denylist: ["restricted-family"]\n')
            assert site_env.read_config(ycfg) == doc
        assert all(site_env.section(name, doc) is None for name in names)
        saved_env, saved_path = os.environ.get("BROKER_CONFIG"), site_env.BROKER_CONFIG
        site_env.BROKER_CONFIG = os.environ["BROKER_CONFIG"] = cfg
        try:
            with patched_config(doc):
                assert site_env.load_settings(doc) == []
                assert app.Config(cdir).data == want
        finally:
            site_env.BROKER_CONFIG = saved_path
            if saved_env is None:
                os.environ.pop("BROKER_CONFIG", None)
            else:
                os.environ["BROKER_CONFIG"] = saved_env
    ok("denylist-only broker.json (and YAML when available) parses and leaves every JSON config file unchanged")


def _uncomment(text, key):
    """The commented-out `# key:` block of the example, uncommented, as YAML text."""
    lines = text.splitlines()
    i = lines.index(f"# {key}:")
    block = [lines[i]]
    for line in lines[i + 1:]:
        if not line.startswith("#   "):
            break
        block.append(line)
    return "\n".join(line[2:] for line in block) + "\n"


def test_example_matches_the_shipped_json_defaults():
    if not HAVE_YAML:
        return ok("example parse skipped (no PyYAML)")
    import yaml
    path = os.path.join(ROOT, "config", "broker.example.yaml")
    ex = site_env.read_config(path)
    assert ex["model_denylist"] == []
    for name in ("constraints", "windows"):
        with open(os.path.join(ROOT, "config", f"{name}.json")) as f:
            want = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        assert ex[name] == want, (name, ex[name], want)
    assert "routes" not in ex and "capabilities" not in ex, sorted(ex)
    with open(path) as f:
        text = f.read()
    with open(os.path.join(ROOT, "config", "routes.example.json")) as f:
        assert yaml.safe_load(_uncomment(text, "routes"))["routes"] == json.load(f)["routes"]
    assert routing.load_site_capabilities(yaml.safe_load(_uncomment(text, "capabilities"))["capabilities"])
    p = _probe({"BROKER_CONFIG": path, "BROKER_SITE_ENV": os.devnull,   # the quick start copies provenance.json
                "BROKER_PROVENANCE_PATH": os.path.join(ROOT, "config", "provenance.example.json")},
               "import routing, provenance, inbox_accounts\nprint(len(routing.SITE_CAPABILITIES))")
    assert p.returncode == 0 and p.stdout.strip() == "0", (p.stdout, p.stderr)
    ok("broker.example.yaml copied as is loads with no error (routes and capabilities commented out); its "
       "constraints/windows equal the shipped json, its commented routes equal routes.example.json and its "
       "commented capabilities load")


def test_section_is_a_copy_and_names_are_checked():
    doc = {"windows": {"initial": {"a": 1}}}
    s = site_env.section("windows", doc)
    s["initial"]["a"] = 9
    assert doc["windows"]["initial"]["a"] == 1 and site_env.section("constraints", doc) is None
    try:
        site_env.section("windowz", doc)
        raise AssertionError("unknown section name accepted")
    except KeyError:
        pass
    ok("section() returns a fresh copy, None when unset, KeyError for a name that isn't a section")


def test_hot_config_sections_replace_json_and_survive_a_bad_reload():
    import app
    with tempfile.TemporaryDirectory() as d:
        cdir = os.path.join(d, "config"); os.makedirs(cdir)
        _write(cdir, "windows.json", json.dumps({"initial": {"zz:a": 1}}))
        _write(cdir, "cd-tier.json", json.dumps({"from": "json"}))
        cfg = _write(d, "broker.json", json.dumps({"windows": {"initial": {"zz:a": 2}}}))
        saved_env, saved_win = os.environ.get("BROKER_CONFIG"), dict(app.C.INITIAL_WINDOWS)
        os.environ["BROKER_CONFIG"] = cfg
        saved_path, site_env.BROKER_CONFIG = site_env.BROKER_CONFIG, cfg
        try:
            c = app.Config(cdir)
            assert c.data["windows"] == {"initial": {"zz:a": 2}} and c.data["cd-tier"] == {"from": "json"}
            assert app.C.INITIAL_WINDOWS["zz:a"] == 2.0
            _write(d, "broker.json", "{")                      # a bad hand-edit, then SIGHUP
            c.reload()
            assert c.data["windows"] == {"initial": {"zz:a": 2}}, c.data
            if HAVE_YAML:                                       # a date: refused whole, the reload survives
                ycfg = _write(d, "broker.yaml", "windows:\n  initial: {zz:a: 2026-10-18}\n")
                site_env.BROKER_CONFIG = os.environ["BROKER_CONFIG"] = ycfg
                c.reload()
                assert c.data["windows"] == {"initial": {"zz:a": 2}} and c.yaml["windows"]["initial"] == {"zz:a": 2}
                site_env.BROKER_CONFIG = os.environ["BROKER_CONFIG"] = cfg
            seen = []
            h = logging.Handler(); h.emit = lambda r: seen.append(r.getMessage())
            app.log.addHandler(h)
            try:                                                # a restart-only section changed: one line says so
                _write(d, "broker.json", json.dumps({"windows": {"initial": {"zz:a": 2}}, "constraints": {"x": 1}}))
                c.reload()
            finally:
                app.log.removeHandler(h)
            assert any("['constraints'] changed" in m and "restart" in m for m in seen), seen
            _write(d, "broker.json", json.dumps({"cd-tier": {"from": "yaml"}}))
            c.reload()
            assert c.data["windows"] == {"initial": {"zz:a": 1}} and c.data["cd-tier"] == {"from": "yaml"}
        finally:
            site_env.BROKER_CONFIG = saved_path
            os.environ["BROKER_CONFIG"] = saved_env
            app.C.INITIAL_WINDOWS.clear(); app.C.INITIAL_WINDOWS.update(saved_win)
    ok("Config: a broker.yaml section replaces its json file, a bad edit (or an unquoted date) keeps the last good "
       "file, a changed restart-only section is logged, SIGHUP picks up a fixed one")


def test_reload_keeps_last_good_on_any_bad_edit():
    import app
    with tempfile.TemporaryDirectory() as d:
        cdir = os.path.join(d, "config"); os.makedirs(cdir)
        _write(cdir, "windows.json", json.dumps({"_comment": "x", "initial": {"zz:b": 1}, "receive": {"zz:b": 4}}))
        _write(cdir, "deploy-paths.json", json.dumps({"g/app": {"kind": "none"}}))
        _write(cdir, "pool-vikunja-ids.json", json.dumps({"pool-1": 31}))
        cfg = _write(d, "broker.json", "{}")
        saved_env, saved_path = os.environ.get("BROKER_CONFIG"), site_env.BROKER_CONFIG
        saved_win = (dict(app.C.INITIAL_WINDOWS), dict(app.C.RECEIVE_WINDOWS))
        site_env.BROKER_CONFIG = os.environ["BROKER_CONFIG"] = cfg
        try:
            c = app.Config(cdir)
            good = json.loads(json.dumps(c.data))
            for bad in ({"initial": {"zz:b": "3s"}}, [1, 2], {"initial": [1]}, {"receive": {"zz:b": True}},
                        {"initial": {"zz:b": -1}}, {"initial": {"zz:b": float("inf")}}, {"receive": {"zz:b": None}}):
                _write(cdir, "windows.json", json.dumps(bad))
                c.reload()                                     # used to raise inside the SIGHUP handler
                assert c.data["windows"] == good["windows"], (bad, c.data)
                assert app.C.INITIAL_WINDOWS["zz:b"] == 1.0 and app.C.RECEIVE_WINDOWS["zz:b"] == 4.0
            _write(cdir, "deploy-paths.json", "{oops")
            _write(cdir, "pool-vikunja-ids.json", "")
            c.reload()                                         # a typo keeps the last good copy, not None
            assert c.data["deploy-paths"] == good["deploy-paths"] and c.data["pool-vikunja-ids"] == {"pool-1": 31}
            _write(cdir, "windows.json", json.dumps({"initial": {"zz:b": 2}}))
            os.remove(os.path.join(cdir, "pool-vikunja-ids.json"))
            c.reload()                                         # a fixed file applies; a removed one is gone
            assert app.C.INITIAL_WINDOWS["zz:b"] == 2.0 and "pool-vikunja-ids" not in c.data, c.data
            _write(cdir, "windows.json", json.dumps({"initial": {"zz:b": "3s"}}))
            fresh = app.Config(cdir)                           # bad at startup: no crash loop, just no windows
            assert "windows" not in fresh.data and "deploy-paths" not in fresh.data   # no last good copy yet
            seen = []
            h = logging.Handler(); h.emit = lambda r: seen.append(r.getMessage())
            app.log.addHandler(h)
            try:                                               # cd-tier is read per verdict, so it is live
                _write(d, "broker.json", json.dumps({"cd-tier": {"extra_patterns": {}}}))
                c.reload()
                saved_broker = app.BROKER
                app.BROKER = type("B", (), {"config": type("C", (), {"reload": lambda self: 1 / 0})()})()
                try:
                    app._reload_on_hup()                       # the handler itself never raises
                finally:
                    app.BROKER = saved_broker
            finally:
                app.log.removeHandler(h)
            assert c.data["cd-tier"] == {"extra_patterns": {}} and not any("restart" in m for m in seen), seen
            assert any("config reload failed" in m for m in seen), seen
        finally:
            site_env.BROKER_CONFIG = saved_path
            os.environ["BROKER_CONFIG"] = saved_env
            for live, saved in zip((app.C.INITIAL_WINDOWS, app.C.RECEIVE_WINDOWS), saved_win):
                live.clear(); live.update(saved)
    ok("SIGHUP: a wrong-shape windows value or an unreadable file keeps the last good copy (no exception, startup "
       "too), a fixed file applies, a removed one goes, cd-tier is live, the HUP handler logs instead of raising")


def _signed_prompt(d):
    text = "You write docs.\n"
    os.makedirs(os.path.join(d, "prompts", "docs"))
    _write(os.path.join(d, "prompts", "docs"), "agent.md", text)
    return os.path.join(d, "prompts"), _write(d, "approved.json", json.dumps(
        {"docs/agent": hashlib.sha256(text.encode()).hexdigest()}))


def test_fresh_install_startup_is_quiet():
    import app
    with tempfile.TemporaryDirectory() as d:   # *.example.json files are templates, not config
        _write(d, "windows.json", json.dumps({"initial": {}}))
        _write(d, "routes.example.json", json.dumps({"routes": []}))
        _write(d, "inbox-accounts.example.json", "{not json")
        saved_env = os.environ.pop("BROKER_CONFIG", None)
        saved_path, site_env.BROKER_CONFIG = site_env.BROKER_CONFIG, os.path.join(d, "broker.yaml")
        try:
            assert sorted(app.Config(d).data) == ["windows"]
        finally:
            site_env.BROKER_CONFIG = saved_path
            if saved_env is not None:
                os.environ["BROKER_CONFIG"] = saved_env
    seen = []
    h = logging.Handler(); h.emit = lambda r: seen.append(r.getMessage())
    lg = logging.getLogger("eph-broker.inbox_accounts")
    lg.addHandler(h)
    saved = {k: os.environ.pop(k, None) for k in ("INBOX_ROW_SOURCE", "ZULIP_ROW_SOURCE")}
    try:   # no inbox accounts: warned only when an inbox row (mail triage or zulip chat) is on
        missing = "/nonexistent/eph-test/inbox-accounts.json"
        assert inbox_accounts.load_accounts(missing, explicit=False) == {} and seen == [], seen
        for k in saved:
            os.environ[k] = "on"
            assert inbox_accounts.load_accounts(missing, explicit=False) == {}
            assert len(seen) == 1 and "every inbox spawn refuses" in seen.pop(), (k, seen)
            del os.environ[k]
    finally:
        lg.removeHandler(h)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
        src = f.read()
    assert "Phase-1: spawns" not in src and 'log.info("eph-broker up on %s:%d  concurrency=%d hmac_env=%s",' in src
    ok("fresh install: Config skips *.example.json, the no-inbox-accounts warning needs an inbox row on, and "
       "the startup line is a neutral one-liner")


def test_routes_section_parity_with_routes_json():
    rows = [{"id": "docs", "match": {"label": "eph:docs"}, "prompt": "docs/agent", "capability": "read",
             "repo": REPO}]
    with tempfile.TemporaryDirectory() as d:
        pdir, appr = _signed_prompt(d)
        from_json = routing.load_config_routes(_write(d, "routes.json", json.dumps({"routes": rows})), pdir, appr)
        with patched_config({"routes": rows}, unset=("ROUTES_PATH",)):
            from_yaml = routing.load_config_routes(None, pdir, appr)
            with patched_config({"routes": {"docs": rows[0]}}, unset=("ROUTES_PATH",)):
                try:
                    routing.load_config_routes(None, pdir, appr)
                    raise AssertionError("a mapping of routes was accepted")
                except routing.RoutesConfigError as e:
                    assert "must be a list" in str(e)
            bad = [dict(rows[0], capability="admin")]
            with patched_config({"routes": bad}, unset=("ROUTES_PATH",)):
                try:
                    routing.load_config_routes(None, pdir, appr)
                    raise AssertionError("unknown capability accepted")
                except routing.RoutesConfigError as e:
                    assert "unknown capability" in str(e) and site_env.BROKER_CONFIG in str(e), e
        with patched_config({"routes": [dict(rows[0], id="other")]}):   # ROUTES_PATH set: the file wins
            assert routing.load_config_routes(None, pdir, appr) == []
    assert [(e[0], e[1], e[2], e[3]) for e in from_yaml] == [(e[0], e[1], e[2], e[3]) for e in from_json]
    assert len(from_yaml) == 1
    ok("routes: the broker.yaml list gives the same rows as routes.json, through the same checks; an explicit "
       "ROUTES_PATH still wins")


def test_provenance_and_inbox_sections_parity():
    with open(os.environ["BROKER_PROVENANCE_PATH"]) as f:
        roster = json.load(f)
    with open(os.path.join(HERE, "fixtures", "inbox-accounts.test.json")) as f:
        accounts = json.load(f)
    want = provenance._load_roster(os.environ["BROKER_PROVENANCE_PATH"])
    with patched_config({"provenance": roster}, unset=("BROKER_PROVENANCE_PATH",)):
        assert provenance._load_roster() == want
        with patched_config({"provenance": dict(roster, extra=[])}, unset=("BROKER_PROVENANCE_PATH",)):
            try:
                provenance._load_roster()
                raise AssertionError("unknown roster key accepted")
            except RuntimeError as e:
                assert "unknown key" in str(e) and "provenance" in str(e)
    with patched_config({"provenance": {"agent_basenames": ["x"]}}):   # BROKER_PROVENANCE_PATH set: file wins
        assert provenance._load_roster() == want
    with patched_config({"inbox-accounts": accounts}, unset=("BROKER_INBOX_ACCOUNTS",)):
        assert inbox_accounts.load_accounts(explicit=False) == accounts["accounts"]
        with patched_config({"inbox-accounts": {"accounts": {"a": {"email": "x"}}}},
                            unset=("BROKER_INBOX_ACCOUNTS",)):
            try:
                inbox_accounts.load_accounts(explicit=False)
                raise AssertionError("bad account accepted")
            except ValueError as e:
                assert "exactly" in str(e)
    ok("provenance and inbox-accounts: a broker.yaml section loads like the json file, same checks; an explicit "
       "path env var still wins")


def test_settings_environment_inventory():
    """Keep literal environment reads (including the small typed wrappers) in the settings reference.

    Parse source without importing optional tools or reading deployment configuration. Dynamic row/module
    names are documented as templates; pass-through/runtime names are deliberately outside this inventory.
    """
    root = Path(ROOT)
    paths = list(root.glob("*.py"))
    for directory in ("adapters", "hooks", "trackers", "tools", "scripts"):
        paths.extend((root / directory).rglob("*.py"))
    trees = [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in paths]

    def dotted(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return dotted(node.value) + "." + node.attr
        return ""

    def env_read(node, path):
        if not isinstance(node, ast.Call):
            return False
        name = dotted(node.func)
        return name in ("os.getenv", "os.environ.get") or (
            "hermes_gate/providers" in path.as_posix() and name == "env.get")

    wrappers = set()
    for path, tree in trees:
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.args.args:
                parameter = fn.args.args[0].arg
                if any(env_read(n, path) and n.args and isinstance(n.args[0], ast.Name)
                       and n.args[0].id == parameter for n in ast.walk(fn)):
                    wrappers.add(fn.name)

    names = set()
    for path, tree in trees:
        for node in ast.walk(tree):
            key = None
            if isinstance(node, ast.Call) and node.args and (
                    env_read(node, path) or dotted(node.func).split(".")[-1] in wrappers):
                key = node.args[0]
            elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                  and dotted(node.value) == "os.environ"):
                key = node.slice
                if isinstance(key, ast.Index):   # Python 3.9-compatible AST handling
                    key = key.value
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", key.value):
                names.add(key.value)

    reference = (root / "docs/settings.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]*)`", reference))
    assert not names - documented, "missing settings: " + ", ".join(sorted(names - documented))
    for suffix in ("CMD", "UPSTREAM", "SOCKET", "MAX_STREAMS", "LABEL", "GROUP", "VERSION_FILE", "DISABLED_FILE"):
        assert "`BROKER_MODULE_<NAME>_" + suffix + "`" in reference
    for suffix in ("NAME", "EMAIL", "IDENTITY_ENABLED"):
        assert "`<ROW>_GIT_" + suffix + "`" in reference
    ok("settings reference covers literal environment reads, typed wrappers and dynamic module/identity templates")


def test_settings_row_defaults():
    """Each formerly ambiguous row has its own code-matching default."""
    reference = Path(ROOT, "docs/settings.md").read_text(encoding="utf-8")
    tree = ast.parse(Path(ROOT, "routing.py").read_text(encoding="utf-8"))
    for name in ("DEV_ASSIGNEE", "INFRA_AGENT", "INFRA_MR_TARGET_REF", "INBOX_AGENT"):
        defaults = {n.args[1].value for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "getenv" and len(n.args) > 1
                    and isinstance(n.args[0], ast.Constant) and n.args[0].value == name
                    and isinstance(n.args[1], ast.Constant)}
        assert len(defaults) == 1, (name, defaults)
        assert "| `" + name + "` | `" + defaults.pop() + "` |" in reference, name
    ok("documented code, infrastructure and mail row defaults match routing source")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(tests)} broker.yaml self-tests...")
    FAILURES = []
    for t in tests:
        try:
            t()
        except BaseException as e:   # noqa: BLE001
            FAILURES.append((t.__name__, e))
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    if FAILURES:
        print(f"\n{len(FAILURES)} of {len(tests)} CHECKS FAILED ({len(PASS)} passed):")
        for name, e in FAILURES:
            print(f"  - {name}: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"\nALL {len(PASS)} CHECKS PASSED")
