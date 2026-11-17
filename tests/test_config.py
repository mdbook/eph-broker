#!/usr/bin/env python3
"""broker.yaml self-tests (site_env.read_config / section / load_settings and the loaders that read a section).

stdlib assert only, same runner shape as test_routes.py. Import-time behaviour (settings precedence, refusing a bad
file) runs in a subprocess with its own environment; everything else patches site_env.CONFIG and restores it.
"""
import ast, hashlib, json, logging, os, re, shutil, subprocess, sys, tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


def test_hold_labels_config():
    with tempfile.TemporaryDirectory() as d:
        for invalid in ("blocked", {}, [True], [0], [-1], [1.5], [""], ["   "], None):
            path = _write(d, "holds.json", json.dumps({"hold_labels": invalid}))
            try:
                site_env.read_config(path)
                raise AssertionError(f"accepted invalid hold_labels: {invalid!r}")
            except site_env.BrokerConfigError:
                pass
        for valid in ([], ["waiting", 9123]):
            doc = {"hold_labels": valid}
            assert site_env.read_config(_write(d, "holds.json", json.dumps(doc))) == doc
    with patched_config({}):
        assert site_env.has_hold_label([" BLOCKED "])
        assert not site_env.has_hold_label(["unblocked", "blocked-later"])
    with patched_config({"hold_labels": ["Waiting", 9123]}):
        assert site_env.has_hold_label(["waiting"])
        assert site_env.has_hold_label([], [9123])
        assert site_env.has_hold_label([{"id": 9123, "title": "dependency"}])
        assert not site_env.has_hold_label(["blocked", "9123"])
    with patched_config({"hold_labels": []}):
        assert not site_env.has_hold_label(["blocked"], [9123])
    ok("hold_labels: validates names/ids, exact matching, blocked default, explicit empty disables")


def test_default_config_selection_without_pyyaml():
    with tempfile.TemporaryDirectory() as d:
        shutil.copyfile(os.path.join(ROOT, "site_env.py"), os.path.join(d, "site_env.py"))
        shutil.copyfile(os.path.join(ROOT, "config_paths.py"), os.path.join(d, "config_paths.py"))
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
        names = site_env.SECTIONS - {"settings", "capabilities", "model_denylist", "hold_labels"}
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


def test_external_config_is_authoritative_and_prompt_edits_remain_gated():
    import config_paths
    from prompts import PromptLoader, PromptNotApproved
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        _write(d, "broker.json", '{"settings": {"CONCURRENCY": 7}}')
        _write(d, "site.env", "PORT=9898\n")
        _write(d, "constraints.json", '{"max_turns": 13}')
        _write(d, "repo-companions.json", '{"example/app": []}')
        os.makedirs(os.path.join(d, "prompts", "code"))
        prompt = _write(os.path.join(d, "prompts", "code"), "change-proposer.md", "site prompt")
        approved = _write(d, "approved-prompts.json", json.dumps({
            "code/change-proposer": hashlib.sha256(b"site prompt").hexdigest()}))
        p = _probe({"BROKER_CONFIG_DIR": d, "BROKER_PROVENANCE_NEUTRAL": "1"},
                   "import app, routing, repo_catalog, repo_companions, inbox_accounts, provenance\n"
                   "from routing_overrides import RoutingOverrides\n"
                   "assert app.CONFIG_DIR == os.environ['BROKER_CONFIG_DIR']\n"
                   "assert app.Config(app.CONFIG_DIR).data['constraints']['max_turns'] == 13\n"
                   "assert routing.ROUTES_PATH == app.CONFIG_DIR + '/routes.json'\n"
                   "assert repo_catalog.ALIASES_FILE == app.CONFIG_DIR + '/repo-aliases.json'\n"
                   "assert repo_companions.load() == {'example/app': []}\n"
                   "assert inbox_accounts.ACCOUNTS_PATH == app.CONFIG_DIR + '/inbox-accounts.json'\n"
                   "assert provenance._OVERRIDE_PATH == app.CONFIG_DIR + '/provenance.json'\n"
                   "assert RoutingOverrides().path == app.CONFIG_DIR + '/routing-overrides.json'\n"
                   "assert app.PROMPTS_DIR == app.CONFIG_DIR + '/prompts'\n"
                   "assert app.APPROVED_PROMPTS == app.CONFIG_DIR + '/approved-prompts.json'\n"
                   "print(os.getenv('CONCURRENCY'), os.getenv('PORT'))")
        assert p.returncode == 0 and p.stdout.strip() == "7 9898", (p.stdout, p.stderr)
        with patch.dict(os.environ, {"BROKER_CONFIG_DIR": d}):
            loader = PromptLoader(config_paths.prompts_dir(), config_paths.approved_path())
            assert loader.load("change-proposer", "code") == "site prompt"
            Path(prompt).write_text("changed site prompt")
            try:
                loader.load("change-proposer", "code")
                raise AssertionError("unsigned site edit accepted")
            except PromptNotApproved:
                pass
            Path(approved).write_text(json.dumps({
                "code/change-proposer": hashlib.sha256(b"changed site prompt").hexdigest()}))
            assert loader.load("change-proposer", "code") == "changed site prompt"
            Path(approved).write_text("{}")
            try:
                loader.load("change-proposer", "code")
                raise AssertionError("missing site signature fell back to packaged approval")
            except PromptNotApproved:
                pass
            Path(prompt).unlink()
            try:
                loader.load("change-proposer", "code")
                raise AssertionError("missing site prompt fell back to packaged prompt")
            except FileNotFoundError:
                pass
        # Individual paths remain explicit escape hatches, including legacy CONFIG_DIR.
        with patch.dict(os.environ, {"BROKER_CONFIG_DIR": d, "CONFIG_DIR": "/custom/json",
                                    "PROMPTS_DIR": "/custom/prompts", "APPROVED_PROMPTS": "/custom/approved"}):
            assert config_paths.config_dir() == "/custom/json"
            assert config_paths.prompts_dir() == "/custom/prompts"
            assert config_paths.approved_path() == "/custom/approved"
    ok("external config wins for every reader; prompt edits and signatures reload per launch, unsigned refuses")


def test_rpm_migrates_before_upgrade_and_never_clobbers_site_config():
    _check_rpm_migration("absent")


def test_rpm_migrates_when_only_environment_file_exists():
    _check_rpm_migration("environment")


def test_rpm_migration_preserves_existing_operator_files_and_metadata():
    _check_rpm_migration("operator")


def _check_rpm_migration(existing):
    import config_paths
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        package = Path(d, "package")
        (package / "config").mkdir(parents=True)
        (package / "prompts" / "code").mkdir(parents=True)
        (package / "config" / "constraints.json").write_text('{"max_turns": 17}')
        (package / "prompts" / "code" / "change-proposer.md").write_text("old signed prompt")
        (package / "approved-prompts.json").write_text('{"old": "signature"}')
        (package / "config" / "constraints.json").chmod(0o640)
        target = Path(d, "site")
        preserved = {}
        if existing != "absent":
            target.mkdir(mode=0o750)
            (target / "eph-broker.env").write_text("EXAMPLE_SETTING=kept\n")
            (target / "eph-broker.env").chmod(0o600)
        if existing == "operator":
            (target / "constraints.json").write_text('{"max_turns": 23}')
            (target / "constraints.json").chmod(0o600)
            (target / "prompts" / "code").mkdir(parents=True, mode=0o700)
            (target / "prompts" / "code" / "change-proposer.md").write_text("operator prompt")
            (target / "approved-prompts.json").symlink_to("missing-operator-approvals")
        if target.exists():
            for path in [target, *target.rglob("*")]:
                stat = path.lstat()
                content = path.readlink() if path.is_symlink() else path.read_bytes() if path.is_file() else None
                preserved[path] = (stat.st_mode, stat.st_uid, stat.st_gid, stat.st_ino, content)
        spec = Path(ROOT, "packaging/eph-broker.spec").read_text()
        migration = spec.split("# BEGIN site config migration\n")[1].split("# END site config migration")[0]
        migration = migration.replace("/opt/eph-broker", str(package)).replace("/etc/eph-broker", str(target))
        p = subprocess.run(["sh", "-c", migration], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        for path, metadata in preserved.items():
            stat = path.lstat()
            content = path.readlink() if path.is_symlink() else path.read_bytes() if path.is_file() else None
            assert (stat.st_mode, stat.st_uid, stat.st_gid, stat.st_ino, content) == metadata
        assert (target / config_paths.SEED_MARKER).is_file()
        assert (target / "constraints.json").read_text() == ('{"max_turns": 23}' if existing == "operator"
                                                           else '{"max_turns": 17}')
        if existing != "operator":
            src_stat = (package / "config" / "constraints.json").stat()
            dst_stat = (target / "constraints.json").stat()
            assert (src_stat.st_mode, src_stat.st_uid, src_stat.st_gid) == (
                dst_stat.st_mode, dst_stat.st_uid, dst_stat.st_gid)
            assert (target / "prompts" / "code" / "change-proposer.md").read_text() == "old signed prompt"
            assert (target / "approved-prompts.json").read_text() == '{"old": "signature"}'
        before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        # RPM now replaces its own files, then %post skips the completed migration.
        (package / "config" / "constraints.json").write_text('{"max_turns": 99}')
        (package / "prompts" / "code" / "change-proposer.md").write_text("new packaged prompt")
        (package / "approved-prompts.json").write_text('{}')
        config_paths.seed_site_config(str(package), str(target))
        p = subprocess.run(["sh", "-c", migration], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        assert before == {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        (target / "approved-prompts.json").unlink()
        config_paths.seed_site_config(str(package), str(target))
        p = subprocess.run(["sh", "-c", migration], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        assert not (target / "approved-prompts.json").exists(), "upgrade must not restore removed approvals"
        fresh = Path(d, "fresh")
        config_paths.seed_site_config(str(package), str(fresh))
        assert (fresh / "config").exists() is False
        assert (fresh / "constraints.json").read_text() == '{"max_turns": 99}'
        assert (fresh / "prompts" / "code" / "change-proposer.md").read_text() == "new packaged prompt"
        assert spec.index("# BEGIN site config migration") < spec.index("%post\n")
        assert "/usr/bin/python3 /opt/eph-broker/config_paths.py /etc/eph-broker" in spec
        assert "Environment=BROKER_CONFIG_DIR=/etc/eph-broker" in Path(
            ROOT, "packaging/eph-broker.service").read_text()
    ok("RPM migration (%s) preserves operator data and metadata; upgrades never clobber" % existing)


def test_config_seed_merges_once_without_changing_existing_metadata():
    import config_paths
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        source = Path(d, "package")
        (source / "config" / "nested").mkdir(parents=True)
        (source / "config" / "nested" / "new.json").write_text("{}")
        (source / "config" / "constraints.json").write_text('{"max_turns": 99}')
        (source / "prompts" / "code").mkdir(parents=True)
        (source / "prompts" / "code" / "agent.md").write_text("packaged prompt")
        (source / "approved-prompts.json").write_text("{}")
        for existing in ("environment", "operator"):
            target = Path(d, existing)
            target.mkdir(mode=0o750)
            env = target / "eph-broker.env"
            env.write_text("EXAMPLE_SETTING=kept\n")
            env.chmod(0o600)
            if existing == "operator":
                (target / "nested").mkdir(mode=0o700)
                (target / "constraints.json").write_text('{"max_turns": 23}')
                (target / "prompts").symlink_to("operator-prompts")
            preserved = {p: p.lstat() for p in [target, *target.rglob("*")]}
            config_paths.seed_site_config(str(source), str(target))
            for path, old in preserved.items():
                new = path.lstat()
                assert (old.st_mode, old.st_uid, old.st_gid, old.st_ino) == (
                    new.st_mode, new.st_uid, new.st_gid, new.st_ino)
            assert env.read_text() == "EXAMPLE_SETTING=kept\n"
            assert (target / "constraints.json").read_text() == ('{"max_turns": 23}' if existing == "operator"
                                                               else '{"max_turns": 99}')
            assert (target / "nested" / "new.json").read_text() == "{}"
            assert (target / config_paths.SEED_MARKER).is_file()
            (target / "approved-prompts.json").unlink()
            config_paths.seed_site_config(str(source), str(target))
            assert not (target / "approved-prompts.json").exists()
            if existing == "operator":
                assert os.readlink(target / "prompts") == "operator-prompts"
    ok("seeding merges missing paths once, preserving the EnvironmentFile, existing config and directory metadata")


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
                _write(d, "broker.json", json.dumps({"windows": {"initial": {"zz:a": 2}}, "routes": []}))
                c.reload()
            finally:
                app.log.removeHandler(h)
            assert any("['routes'] changed" in m and "restart" in m for m in seen), seen
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


@contextmanager
def _live_broker():
    import app
    import coalescer
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        path = Path(d, "broker.json")
        path.write_text("{}")
        config_dir = Path(d, "config")
        config_dir.mkdir()
        rows, profiles, config = routing._RUNTIME_ROWS, routing.PROFILES, site_env.CONFIG
        with patch.dict(os.environ), patch.object(app, "CONFIG_DIR", str(config_dir)), \
                patch.object(coalescer, "COMPLETED_PATH", ""), \
                patch.object(site_env, "BROKER_CONFIG", str(path)):
            broker = app.Broker()
            try:
                yield broker, path
            finally:
                broker.runtime_settings.apply({})
                routing._RUNTIME_ROWS, routing.PROFILES = rows, profiles
                site_env.CONFIG = config


def _reload(broker, path, settings=None, **sections):
    path.write_text(json.dumps({"settings": settings or {}, **sections}))
    broker.config.reload()


def test_live_settings_sighup_changes_existing_broker_and_keeps_bootstrap():
    import app
    import coalescer
    import dispatch
    import cd
    from adapters import cc, hermes, codex
    with _live_broker() as (b, path):
        identity = (b.adapter, b.coalescer, b.state, b.semaphore)
        path.write_text(json.dumps({"settings": {
            "CONCURRENCY": 2, "DISPATCH_PRIORITY": "fifo", "ROW_CONCURRENCY": "code=0",
            "MAX_SPAWNS_PER_HOUR": 7, "CD_ENABLED": True, "PORT": 9999,
            "LOCAL_WALL_CLOCK_MULT": 2.5, "API_OVERLOAD_RETRY_DELAYS_S": "0.5,1.5",
            "CODEX_REASONING_SUMMARY": "concise", "CODE_CHANGE_ADAPTER": "codex",
            "INITIAL_DEFAULT": 0.25, "BROKER_STREAM_ENABLED": "on"}}))
        port = app.PORT
        with patch.object(app, "BROKER", b):
            app._reload_on_hup()
        assert b.semaphore.limit == app.CONCURRENCY == 2
        assert not dispatch.reviews_first() and coalescer.row_concurrency()["code"] == 0
        assert coalescer.MAX_SPAWNS_PER_HOUR == 7 and coalescer.INITIAL_DEFAULT == 0.25
        assert cd.enabled() and app.PORT == port
        assert cc.LOCAL_WALL_CLOCK_MULT == hermes.LOCAL_WALL_CLOCK_MULT == 2.5
        assert cc.API_OVERLOAD_RETRY_DELAYS_S == (0.5, 1.5) and codex.REASONING_SUMMARY == "concise"
        assert routing._CODE_ROW.with_model().adapter == "codex"
        assert app.stream.enabled()
        assert identity == (b.adapter, b.coalescer, b.state, b.semaphore)
        _reload(b, path)  # removal returns to startup defaults
        assert app.PORT == port and b.semaphore.limit == b.runtime_settings.defaults["CONCURRENCY"]
    ok("SIGHUP updates the existing broker's scheduler, CD, harness, stream and retry policy; bootstrap stays fixed")


def test_live_settings_bad_file_and_bad_values_keep_last_good():
    import app
    import dispatch
    with _live_broker() as (b, path):
        _reload(b, path, {"CONCURRENCY": 2, "DISPATCH_PRIORITY": "fifo"})
        path.write_text("{")
        b.config.reload()
        assert b.semaphore.limit == 2 and not dispatch.reviews_first()
        for name, invalid in (("CONCURRENCY", 0), ("CONCURRENCY", True), ("CONCURRENCY", 1.5),
                              ("TICK", "nan"), ("TICK", -1), ("DISPATCH_PRIORITY", "typo"),
                              ("ROW_CONCURRENCY", "code=-1"), ("CD_ENABLED", "maybe"),
                              ("MR_REVIEWER_BACKEND", "unresolvable-test-backend"),
                              ("CODE_CHANGE_ADAPTER", "typo"), ("CODEX_REASONING_SUMMARY", "typo"),
                              ("WORKER_SANDBOX", "typo"), ("EPH_GATE_NONSECRET_PATHS", "[]"),
                              ("INITIAL_WINDOWS", "[]"), ("EVENTS_WEBHOOK_KINDS", "no-such-kind")):
            with patch.object(logging.getLogger("eph-broker.settings"), "error") as error:
                _reload(b, path, {"CONCURRENCY": 4, "DISPATCH_PRIORITY": "reviews-first", name: invalid})
                assert error.called, name
            assert b.semaphore.limit == app.CONCURRENCY == 2, name
            assert not dispatch.reviews_first(), name
    ok("parse errors and invalid runtime values log and keep the whole last-good settings block")


def test_live_api_overload_retries_follow_reload_and_removal():
    from adapters import cc
    with _live_broker() as (b, path), patch.object(cc, "USAGE_CAP_FALLBACK_ENABLED", False):
        adapter = b.adapter  # constructed before the policy changes
        default = b.runtime_settings.defaults["API_OVERLOAD_RETRY_DELAYS_S"]
        sleeps, calls = [], []

        def overloaded(worker, route, system_prompt, prompt_text, resume):
            calls.append((prompt_text, resume))
            worker.last_output = json.dumps({"is_error": True, "api_error_status": 529})
            return None

        with patch.object(adapter, "_one_turn", side_effect=overloaded), \
                patch.object(adapter, "_api_overload_sleep", side_effect=sleeps.append), \
                patch.object(adapter, "_post_ticket_note") as note, patch.object(adapter, "_alert_ops"):
            for settings, expected in (({"API_OVERLOAD_RETRY_DELAYS_S": "0.5,1.5"}, (0.5, 1.5)),
                                       ({}, default)):
                _reload(b, path, settings)
                sleeps.clear()
                calls.clear()
                note.reset_mock()
                worker = cc.Worker("vikunja:task:9992", "retry-reload", str(path.parent))
                assert adapter._turn_with_envelope(worker, routing._CODE_ROW, "SYS", "PROMPT", False,
                                                   "retry-reload") is None
                assert len(calls) == 1 + len(expected) and all(c == ("PROMPT", False) for c in calls)
                assert sum(sleeps) == sum(expected), (sleeps, expected)
                if settings:
                    assert sleeps == list(expected), sleeps
                assert worker.api_overloaded and note.call_count == 1
                assert "across %d back-off" % len(expected) in note.call_args[0][1]
    ok("existing adapter retries with reloaded delays; removing the setting restores the startup default schedule")


def test_live_api_overload_keeps_explicit_constructor_override():
    from adapters import cc
    with _live_broker() as (b, path), patch.object(cc, "USAGE_CAP_FALLBACK_ENABLED", False):
        sleeps = []
        adapter = cc.CCAdapter(router=None, worktree_mgr=None, prompt_loader=None, envelope_parser=None,
                               api_overload_delays_s=(0.25,), api_overload_sleep=sleeps.append)
        worker = cc.Worker("vikunja:task:9993", "retry-override", str(path.parent))
        worker.last_output = json.dumps({"is_error": True, "api_error_status": 429})
        with patch.object(adapter, "_one_turn", return_value=None) as turn, \
                patch.object(adapter, "_post_ticket_note"), patch.object(adapter, "_alert_ops"):
            _reload(b, path, {"API_OVERLOAD_RETRY_DELAYS_S": "1,2"})
            adapter._turn_with_envelope(worker, routing._CODE_ROW, "SYS", "PROMPT", False, "retry-override")
            assert sleeps == [0.25] and turn.call_count == 2
    ok("an explicit retry schedule override stays fixed across policy reloads")


@contextmanager
def _live_zulip_reconciliation(flag):
    import zulip_wake
    with patch.dict(os.environ, ZULIP_WAKE_ENABLED="0", ZULIP_AUTOREPLY_ENABLED="0"), \
            _live_broker() as (b, path):
        zw = b.zulip_wake
        ready, stop = threading.Event(), threading.Event()

        def loop():
            zw.reconcile()
            ready.set()
            stop.wait()  # hold the driver alive until the test has exercised further reloads

        wake_map = {"chat-bot": {"row": "inbox-chat", "account": "test"}} if flag == "ZULIP_WAKE_ENABLED" else {}
        with patch.object(zw.registry, "retired", return_value={"chat-bot": "/example/bot-env"}), \
                patch.object(zw, "wake_map", return_value=wake_map), \
                patch.object(zw, "_loop", side_effect=loop) as loops, \
                patch.object(zulip_wake._Listener, "start") as starts, \
                patch.object(zulip_wake._Listener, "stop"):
            try:
                assert zw.start() is False and zw._thread is None
                yield b, path, ready, loops, starts
            finally:
                stop.set()
                if zw._thread is not None:
                    zw._thread.join(5)
                    assert not zw._thread.is_alive()


def test_live_zulip_disabled_at_startup_enables_after_sighup():
    import app
    for flag in ("ZULIP_WAKE_ENABLED", "ZULIP_AUTOREPLY_ENABLED"):
        with _live_zulip_reconciliation(flag) as (b, path, ready, loops, starts):
            path.write_text(json.dumps({"settings": {flag: True}}))
            with patch.object(app, "BROKER", b):
                app._reload_on_hup()
            assert ready.wait(5), flag
            assert loops.call_count == starts.call_count == 1
            listener = b.zulip_wake.listeners["chat-bot"]
            assert listener.mode == ("wake" if flag == "ZULIP_WAKE_ENABLED" else "autoreply")
    ok("either Zulip feature enabled via SIGHUP starts reconciliation and its listener after an all-off startup")


def test_live_zulip_repeated_reload_keeps_one_reconciliation_loop():
    for flag in ("ZULIP_WAKE_ENABLED", "ZULIP_AUTOREPLY_ENABLED"):
        with _live_zulip_reconciliation(flag) as (b, path, ready, loops, starts):
            _reload(b, path, {flag: True})
            assert ready.wait(5), flag
            thread = b.zulip_wake._thread
            for settings in ({flag: True}, {flag: True}, {}, {flag: True}):
                _reload(b, path, settings)
                assert b.zulip_wake._thread is thread
            assert loops.call_count == starts.call_count == 1
            # Disabling still uses the existing reconciliation stop path, not a second loop.
            _reload(b, path)
            listener = b.zulip_wake.listeners["chat-bot"]
            b.zulip_wake.reconcile()
            assert not b.zulip_wake.listeners
            listener.stop.assert_called_once()
    ok("repeated reloads and disable/re-enable keep one Zulip reconciliation loop; disabling stops listeners")

def test_concurrency_reload_grows_and_shrinks_with_real_waiters():
    with _live_broker() as (b, path):
        _reload(b, path, {"CONCURRENCY": 1})
        slots = b.semaphore
        assert slots.acquire(key="holder")
        admitted = [threading.Event() for _ in range(3)]
        release = [threading.Event() for _ in range(3)]
        errors, threads = [], []

        def run(i):
            try:
                assert slots.acquire(timeout=5, key=str(i))
                admitted[i].set()
                try:
                    assert release[i].wait(5)
                finally:
                    slots.release()
            except BaseException as exc:
                errors.append(exc)

        holder = True
        try:
            for i in range(3):
                t = threading.Thread(target=run, args=(i,))
                threads.append(t)
                t.start()
                deadline = time.monotonic() + 5
                while True:
                    with slots._condition:
                        if len(slots._waiters) == i + 1:
                            break
                    assert time.monotonic() < deadline
                    time.sleep(0.001)
            _reload(b, path, {"CONCURRENCY": 3})
            assert admitted[0].wait(2) and admitted[1].wait(2), "growth must wake all new capacity at once"
            assert not admitted[2].is_set()
            _reload(b, path, {"CONCURRENCY": 1})
            assert threads[0].is_alive() and threads[1].is_alive(), "shrink cannot kill holders"
            slots.release()
            holder = False
            release[0].set()
            threads[0].join(2)
            assert not admitted[2].wait(0.05), "one remaining holder still occupies the shrunken cap"
            release[1].set()
            assert admitted[2].wait(2)
        finally:
            if holder:
                slots.release()
            for event in release:
                event.set()
            for t in threads:
                t.join(5)
        assert not errors and not any(t.is_alive() for t in threads)
        assert slots is b.adapter._sem is b.coalescer.slots
        assert slots.acquire(blocking=False)
        slots.release()
        try:
            slots.release()
            raise AssertionError("over-release accepted")
        except ValueError:
            pass
    ok("live grow immediately fills new slots; shrink leaves holders alone and blocks admission until below cap")


def test_resize_preserves_operator_reservations_and_repeated_shrink_debt():
    from dispatch import PrioritySlots
    slots = PrioritySlots(3)
    assert all(slots.acquire(blocking=False) for _ in range(3))
    slots.resize(1)
    slots.resize(2)
    assert not slots.acquire(blocking=False)
    slots.release()
    assert not slots.acquire(blocking=False)
    slots.release()
    slots.promote("selected", pending=True, reserve=True)
    slots.resize(3)
    assert not slots.acquire(blocking=False, key="ordinary")
    assert slots.acquire(blocking=False, key="selected")
    slots.release()
    slots.release()
    assert slots.limit == 3
    ok("repeated resize accounts for existing ownership and retains operator reservations")


def test_live_reviewer_model_and_backend_survive_full_tier_and_dispatch():
    from adapters import cc
    from routing_overrides import OVERRIDES
    diff = "diff --git a/auth.py b/auth.py\n--- a/auth.py\n+++ b/auth.py\n@@ -1 +1 @@\n-old\n+new\n"
    # Explicitly unpinned startup template, regardless of the fixture/site's defaults.
    template = replace(routing._MR_REVIEW_ROW, model_pin="", backend="", pin=False)
    with patch.object(routing, "_MR_REVIEW_ROW", template), patch.object(OVERRIDES, "apply", side_effect=lambda r: r), \
            patch.object(routing, "resolve_model", return_value="claude-default"), _live_broker() as (b, path):
        assert not template.model_pin and not template.backend
        _reload(b, path, {"MR_REVIEWER_MODEL": "sonnet"})
        route = template.with_model()
        tier = routing.resolve_mr_review_tier(route, diff)
        assert tier[0] == "full" and tier[2] == "sonnet", tier
        seen = []
        directory = str(path.parent)
        wt = SimpleNamespace(make=lambda *a, **kw: directory, mr_diff=lambda *a, **kw: diff,
                             cut_sha=lambda *a: "a" * 40, remove=lambda *a: None)
        a = cc.CCAdapter(router=SimpleNamespace(route=lambda *a: template.with_model()), worktree_mgr=wt,
                         prompt_loader=SimpleNamespace(load=lambda *a, **kw: "SYS"),
                         envelope_parser=None, alert_ops=lambda *a, **kw: None)

        def turn(worker, dispatched, *args, **kwargs):
            seen.append((dispatched.model, dispatched.backend, dispatched.backend_pinned))
            worker.done = True
            return {"status": "pass", "summary": "synthetic review"}

        with patch.object(a, "_turn", side_effect=turn), patch.object(a, "_post_envelope"), \
                patch.object(a, "_post_mr_verdict"), patch.object(cc.events, "emit"):
            a._run_worker("mr:example/app!9991", {"n_events": 1, "task_id": None, "mr": {
                "project": "example/app", "mr_iid": 9991, "repo_path": directory,
                "source_branch": "eph/review-test", "target_ref": "main"}}, "synthetic-review-run")
        assert seen == [("sonnet", "", True)], seen
        _reload(b, path, {"MR_REVIEWER_BACKEND": "claude"})
        route = template.with_model()
        assert routing.resolve_mr_review_tier(route, diff)[2:4] == (route.model, "claude")
    ok("reloaded reviewer model/backend wins full-tier resolution and the CC-dispatched route carries sonnet")


def test_live_route_pins_show_in_settings_and_file_overrides_still_win():
    from routing_overrides import OVERRIDES, RoutingOverrides
    with _live_broker() as (b, path), patch.object(routing, "resolve_model", return_value="claude-default"):
        loader = RoutingOverrides(str(path.with_name("overrides.json")))
        with patch("routing_overrides.OVERRIDES", loader):
            before = routing._MR_REVIEW_ROW.with_model()
            _reload(b, path, {"MR_REVIEWER_MODEL": "sonnet"})
            after = routing._MR_REVIEW_ROW.with_model()
            assert before.model == "claude-default" and after.model == "sonnet"
            assert routing.row_pinned("mr-review")
            view = next(row for row in loader.view()["rows"] if row["row"] == "mr-review")
            assert view["pinned"] and view["model_pin"] == "sonnet"
            Path(loader.path).write_text(json.dumps({"rows": {"mr-review": {"model_pin": "haiku"}}}))
            route = routing._MR_REVIEW_ROW.with_model()
            assert route.model == "haiku" and after.model == "sonnet"
            assert routing.resolve_mr_review_tier(route, "unparseable diff")[2] == "haiku"
    ok("runtime pins reach dashboard/admission checks; existing routes stay unchanged and row override files win")


def test_live_launch_policy_and_optional_rows_reload_without_restart():
    with _live_broker() as (b, path):
        _reload(b, path, model_denylist=["blocked-family"])
        try:
            site_env.require_model_allowed("blocked-family-model")
            raise AssertionError("live denylist not applied")
        except site_env.ModelDenied:
            pass
        path.write_text('{"model_denylist": [false]}')
        b.config.reload()
        assert site_env.CONFIG["model_denylist"] == ["blocked-family"]
        _reload(b, path, model_denylist=[])
        site_env.require_model_allowed("blocked-family-model")
    ok("launch denylist and optional row enablement reload in place; invalid policy keeps the previous list")


def test_live_fractional_constraints_reach_turn_budget_without_immediate_exhaustion():
    from adapters import cc
    with _live_broker() as (b, path):
        _reload(b, path, constraints={"wall_clock_s": 0.5, "wall_clock_by_capability": {"code": 0.75},
                                     "max_turns": 9, "max_forwards": 3})
        assert b.adapter.wall_clock_s == 0.5 and b.adapter.wall_clock_by_capability["code"] == 0.75
        assert b.adapter.max_turns == 9 and b.coalescer.max_forwards == 3
        route = routing.Route(domain="test", repo=str(path.parent), prompt_class="agent", agent="test",
                              model="sonnet", cc_fail_fast=False)
        worker = cc.Worker("test:9992", "synthetic-fractional-run", str(path.parent))
        budgets = []

        class Proc:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                # Steer polling subtracts elapsed time from the fractional turn budget.
                assert 0.4 < timeout <= 0.5
                budgets.append(timeout)
                return json.dumps({"type": "result", "result": "{}", "session_id": "synthetic"}), ""

        with patch.object(cc.subprocess, "Popen", return_value=Proc()), \
                patch.object(cc.sandbox, "wrap", side_effect=lambda argv, *a, **kw: argv), \
                patch.object(cc.events, "emit"):
            b.adapter._one_turn(worker, route, "SYS", "{}", resume=False)
        assert len(budgets) == 1 and 0.4 < budgets[0] <= 0.5
        assert not worker.hard_killed and worker.turn_wall_clock == 0.5
        for cfg in ({"wall_clock_s": 0}, {"wall_clock_s": "nan"}, {"wall_clock_by_capability": {"read": -1}},
                    {"model_prices": {"test": {"input_tokens": -1}}}):
            _reload(b, path, constraints=cfg)
            assert b.adapter.wall_clock_s == 0.5 and b.adapter.wall_clock_by_capability["code"] == 0.75
    ok("fractional duration reloads reach the actual CC timeout without truncation; bad constraints retain budgets")


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
