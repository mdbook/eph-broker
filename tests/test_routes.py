#!/usr/bin/env python3
"""routes.json self-tests: site-defined agent types (routing.load_config_routes).

stdlib assert only, same runner shape as test_hermes.py. Every case builds its own temp prompts dir, signed
digest file and routes.json, so nothing here reads the deployment's config/ or prompts/.
"""
import hashlib, json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["BROKER_SITE_ENV"] = os.path.join(HERE, "fixtures", "site.test.env")
os.environ["BROKER_CONFIG"] = os.path.join(HERE, "fixtures", "broker.test.json")
os.environ["BROKER_PROVENANCE_PATH"] = os.path.join(HERE, "fixtures", "provenance.test.json")
os.environ["ROUTES_PATH"] = "/nonexistent/eph-test/routes.json"   # the import below must see no routes.json
import site_env  # noqa: F401

import routing
from routing import Router, RoutesConfigError, load_config_routes
from adapters.cc import CCAdapter
from envelope import EnvelopeParser

PASS = []
REPO = "/srv/git/sample-app"          # in both allowlists of tests/fixtures/site.test.env


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


class Site:
    """A temp prompts dir + approved-prompts.json + routes.json. `prompts` maps "domain/class" -> text; every
    prompt is signed unless named in `unsigned`."""
    def __init__(self, routes, prompts=None, unsigned=(), raw=None):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.prompts_dir, self.approved, self.path = (os.path.join(d, "prompts"), os.path.join(d, "approved.json"),
                                                      os.path.join(d, "routes.json"))
        signed = {}
        for name, text in (prompts or {"docs/agent": "You write docs.\n"}).items():
            dom, cls = name.split("/")
            os.makedirs(os.path.join(self.prompts_dir, dom), exist_ok=True)
            with open(os.path.join(self.prompts_dir, dom, f"{cls}.md"), "w") as f:
                f.write(text)
            if name not in unsigned:
                signed[name] = hashlib.sha256(text.encode()).hexdigest()
        with open(self.approved, "w") as f:
            json.dump(signed, f)
        with open(self.path, "w") as f:
            f.write(raw if raw is not None else json.dumps({"routes": routes}))

    def load(self):
        return load_config_routes(self.path, self.prompts_dir, self.approved)

    def close(self):
        self.tmp.cleanup()


def row(**kw):
    base = {"id": "docs", "match": {"label": "eph:docs"}, "prompt": "docs/agent", "capability": "read",
            "repo": REPO}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def refused(routes, needle, **site_kw):
    s = Site(routes, **site_kw)
    try:
        s.load()
    except RoutesConfigError as e:
        assert needle in str(e), (needle, str(e))
        return
    finally:
        s.close()
    raise AssertionError(f"expected RoutesConfigError containing {needle!r}")


def table(extra):
    return routing._with_config_routes(routing._build_profiles(), extra)


def classify(profiles, labels=(), assignees=()):
    r = Router(rows=routing._profiles_to_rows(profiles))
    return r.classify("vikunja:task:1", {"event_name": "task.updated", "labels": list(labels),
                                         "assignees": list(assignees)})


def _shape(profiles):
    return [(e[0], e[1], e[2].row_key, (e[3] if len(e) > 3 else routing._label_signal_predicates).__name__)
            for e in profiles]


# =====================================================================================================
# parity: no routes.json -> the built-in table, unchanged
# =====================================================================================================
def test_no_routes_file_is_byte_identical():
    assert load_config_routes("/nonexistent/eph-test/routes.json") == []
    base = routing._build_profiles()
    assert routing._with_config_routes(base, []) is base
    assert len(routing.PROFILES) == len(base) and all(a is b for a, b in zip(routing.PROFILES, base))
    # a fresh interpreter with no routes.json and one with an empty {"routes": []} build the same table
    probe = ("import json, routing; print(json.dumps([(e[0], str(e[1]), e[2].row_key, repr(e[2]), "
             "(e[3] if len(e) > 3 else routing._label_signal_predicates).__name__) for e in routing.PROFILES]))")
    outs = []
    with tempfile.TemporaryDirectory() as d:
        empty = os.path.join(d, "routes.json")
        with open(empty, "w") as f:
            f.write('{"_comment": "nothing yet", "routes": []}')
        for path in ("/nonexistent/eph-test/routes.json", empty):
            env = {**os.environ, "ROUTES_PATH": path}
            r = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True,
                               timeout=60)
            assert r.returncode == 0, r.stderr
            outs.append(r.stdout)
    assert outs[0] == outs[1] and outs[0].strip(), outs
    ok("routes.json absent or empty: PROFILES is the built-in table, same objects, same order, same repr")


def test_config_rows_leave_builtin_routing_unchanged():
    s = Site([row(), row(id="ops-helper", match={"assignee": "helper"}, prompt="docs/agent")])
    try:
        extra = s.load()
    finally:
        s.close()
    base, mixed = routing._build_profiles(), table(extra)
    assert [e for e in mixed if e not in extra] == base          # built-ins keep their relative order
    cases = [((), ("dev",)), (("eph:code-change",), ()), ((), ("eph",)), (("eph:general",), ()), ((), ("fleet",)),
             (("ansible",), ()), (("wiki",), ()), (("infra:general",), ("infra-eph",)), ((), ("infra-eph",)),
             (("unrelated",), ()), ((), ())]
    for labels, assignees in cases:
        assert classify(base, labels, assignees) == classify(mixed, labels, assignees), (labels, assignees)
    ok("config rows added: every built-in label/assignee event classifies exactly as before")


# =====================================================================================================
# loading + matching
# =====================================================================================================
def test_valid_rows_load_and_route():
    s = Site([row(),
              row(id="both", match={"assignee": "helper", "label": "triage"}),
              row(id="helper", match={"assignee": "helper"}, model="opus", agent="helper-worker")])
    try:
        extra = s.load()
    finally:
        s.close()
    facs = [e[3] for e in extra]
    assert facs == [routing._exact_label_signal_predicates, routing._assignee_label_signal_predicates,
                    routing._assignee_signal_predicates], facs
    assert extra[1][1] == ("helper", "triage")
    r = extra[2][2]
    assert (r.domain, r.prompt_class, r.row_key, r.capability, r.read_only, r.repo, r.agent, r.model_pin,
            r.from_config, r.backend_from_mode) == ("docs", "agent", "helper", "read", True, REPO, "helper-worker",
                                                    "opus", True, True), r
    # defaults: agent and backend pin from the template row (read -> ansible), no model pin
    assert extra[0][2].agent == routing._ANSIBLE_ROW.agent and extra[0][2].model_pin == ""
    assert extra[0][2].backend == routing._ANSIBLE_ROW.backend
    t = table(extra)
    assert classify(t, labels=["eph:docs"]) == "docs"
    assert classify(t, labels=["EPH:Docs "]) == "docs"                       # case and whitespace folded
    assert classify(t, labels=["eph:docs-old"]) is None                       # exact, no substring match
    assert classify(t, assignees=["eph:docs"]) is None                        # labels only, never assignees
    assert classify(t, assignees=["helper"]) == "helper"
    assert classify(t, labels=["triage"], assignees=["helper"]) == "both"    # first match wins, in file order
    routed = Router(rows=routing._profiles_to_rows(t)).route("vikunja:task:1", {"labels": ["eph:docs"]})
    assert routed.row_key == "docs" and routed.from_config and routed.model, routed
    pinned = Router(rows=routing._profiles_to_rows(t)).route("vikunja:task:1", {"assignees": ["helper"]})
    assert pinned.model == "opus" and pinned.backend_pinned, pinned
    ok("routes.json: label / assignee / both rows load in file order, exact match, route() keeps from_config")


def test_config_rows_sit_between_builtins_and_general_fallbacks():
    s = Site([row()])
    try:
        t = table(s.load())
    finally:
        s.close()
    keys = [e[2].row_key for e in t]
    i = keys.index("docs")
    assert keys[i + 1] == "general" and "general" not in keys[:i] and "code" in keys[:i], keys
    assert classify(t, labels=["eph:docs", "eph:code-change"]) == "code"     # a built-in narrow label wins
    assert classify(t, labels=["eph:docs"], assignees=["dev"]) == "code"     # so does a built-in assignee
    assert classify(t, labels=["eph:docs", "eph:general"]) == "docs"         # the config row beats the catch-all
    assert classify(t, labels=["eph:docs"], assignees=["eph"]) == "docs"
    ok("config rows go after every built-in narrow row and before the @eph / @fleet / eph:general fallbacks")


def test_code_and_general_rows_copy_builtin_plumbing():
    s = Site([row(id="svc", capability="code", prompt="svc/proposer", match={"label": "eph:svc"}),
              row(id="ask", capability="general", prompt="svc/general", match={"label": "eph:ask"})],
             prompts={"svc/proposer": "Propose a change.\n", "svc/general": "Help.\n"})
    try:
        code, gen = (e[2] for e in s.load())
    finally:
        s.close()
    assert code.capability == "code" and not code.read_only and code.test_runner == routing._CODE_ROW.test_runner
    assert code.publish["code_repo_path"] == REPO and code.publish["branch_prefix"] == "eph/svc"
    assert code.publish["worktree_base"] == routing._CODE_ROW.publish["worktree_base"]
    assert code.mcp_config_path == "" and code.setting_sources == ""
    assert gen.mcp_config_path == routing._GENERAL_ROW.mcp_config_path and gen.mcp_config_path
    assert gen.setting_sources == routing._GENERAL_ROW.setting_sources
    assert gen.publish["branch_prefix"] == "eph/ask" and gen.publish["code_repo_path"] == REPO
    assert code.env == routing._CODE_ROW.env and code.env is not routing._CODE_ROW.env
    assert (code.agent, gen.agent) == (routing._CODE_ROW.agent, routing._GENERAL_ROW.agent)
    assert (code.backend, gen.backend) == (routing._CODE_ROW.backend, routing._GENERAL_ROW.backend)
    ok("code / general config rows copy the built-in row's publish, MCP and settings plumbing, own branch prefix")


def test_comment_respawn_and_assignee_dispatch_see_config_rows():
    s = Site([row(), row(id="helper", match={"assignee": "helper"})])
    try:
        t = table(s.load())
    finally:
        s.close()
    saved = routing.PROFILES
    routing.PROFILES = t
    try:
        assert {"docs", "helper"} <= routing.comment_respawn_domains()
        assert routing.comment_respawn_domain([], ["eph:docs"]) == "docs"
        assert routing.assignee_dispatch_domains({"assignees": ["helper"]}) == {"helper"}
    finally:
        routing.PROFILES = saved
    ok("comment-respawn and assignee-dispatch sets include config rows (derived from PROFILES)")


# =====================================================================================================
# fail-closed
# =====================================================================================================
def test_unknown_or_unsupported_capability_refused():
    for cap in ("write", "inbox", "mr-review", "wiki", "admin", "READ", "", None, 3):
        refused([row(capability=cap)] if cap is not None else [{**row(), "capability": None}], "capability")
    ok("capability outside read/code/general (incl. built-in-only ones and case variants) refuses the file")


def test_unsigned_or_changed_prompt_refused():
    refused([row()], "not signed", unsigned=("docs/agent",))
    s = Site([row()])
    try:
        with open(os.path.join(s.prompts_dir, "docs", "agent.md"), "a") as f:
            f.write("one more line\n")                       # edited after sign-off
        try:
            s.load(); raise AssertionError("edited prompt must be refused")
        except RoutesConfigError as e:
            assert "mismatch" in str(e), e
    finally:
        s.close()
    refused([row(prompt="docs/missing")], "missing")
    ok("unsigned prompt, a prompt edited after signing, and a missing prompt file all refuse the file")


def test_bad_rows_refused():
    refused([row(id="code")], "built-in")
    refused([row(id="general")], "built-in")
    refused([row(id="tfa")], "built-in")
    refused([row(), row()], "duplicate")
    refused([row(id="Docs")], "id must")
    refused([row(id="a" * 41)], "id must")
    refused([row(match={})], "match")
    refused([row(match={"label": " "})], "match")
    refused([row(match={"label": "x", "source": "gitlab"})], "match")
    refused([row(match="eph:docs")], "match")
    refused([row(prompt="../etc/passwd")], "prompt must")
    refused([row(prompt="docs")], "prompt must")
    refused([row(prompt="docs/../agent")], "prompt must")
    for dom in ("inbox", "infra", "tfa"):
        refused([row(prompt=f"{dom}/agent")], "reserved", prompts={f"{dom}/agent": "x\n"})
    refused([row(repo="relative/path")], "absolute")
    refused([row(capability="code", repo="/srv/git/not-allowed")], "CODE_REPO_ALLOWLIST")
    refused([row(capability="general", repo="/srv/git/not-allowed")], "GENERAL_REPO_ALLOWLIST")
    refused([{**row(), "tools": ["Bash"]}], "unknown key")              # tool grants cannot come from config
    refused([{**row(), "publish": {"branch_prefix": "main"}}], "unknown key")
    refused([{k: v for k, v in row().items() if k != "repo"}], "missing")
    refused([row(model="opus; rm x")], "model")
    refused([row(agent="")], "agent")
    refused([], "not valid JSON", raw="{not json")
    refused([], "expected", raw='[{"id": "docs"}]')
    refused([], "unknown top-level", raw='{"routes": [], "rows": []}')
    refused([], "expected an object", raw='{"routes": ["docs"]}')
    refused([row(id="code\n")], "id must")                                   # "$" would admit a trailing newline
    refused([row(prompt="docs/agent\n")], "prompt must")
    refused([row(model="opus\n")], "model")
    refused([row(agent="w\n")], "agent")
    refused([row(repo="/srv/git/not-allowed")], "neither")                   # read rows need an allowlisted repo too
    s = Site([row()])
    try:
        with open(s.path, "wb") as f:
            f.write(b'{"routes": [], "_c": "\xff\xfe"}')
        try:
            s.load(); raise AssertionError("non-UTF-8 file must be refused")
        except RoutesConfigError as e:
            assert "unreadable" in str(e), e
        with open(s.path, "w") as f:
            json.dump({"routes": [row()]}, f)
        with open(os.path.join(s.prompts_dir, "docs", "agent.md"), "wb") as f:
            f.write(b"\xff not utf-8\n")
        try:
            s.load(); raise AssertionError("non-UTF-8 prompt must be refused")
        except RoutesConfigError as e:
            assert "prompt" in str(e), e
    finally:
        s.close()
    ok("reserved/duplicate/bad ids, bad match, path-like or reserved prompts, unlisted repos, unknown keys "
       "(tools, publish), bad names and malformed JSON all refuse the whole file")


def test_invalid_routes_json_stops_import():
    s = Site([row(capability="write")])
    try:
        env = {**os.environ, "ROUTES_PATH": s.path}
        r = subprocess.run([sys.executable, "-c", "import routing"], cwd=ROOT, env=env, capture_output=True,
                           text=True, timeout=60)
    finally:
        s.close()
    assert r.returncode != 0 and "RoutesConfigError" in r.stderr and "capability" in r.stderr, r.stderr
    s = Site([row()])
    try:
        env = {**os.environ, "ROUTES_PATH": s.path, "PROMPTS_DIR": s.prompts_dir, "APPROVED_PROMPTS": s.approved}
        r = subprocess.run([sys.executable, "-c", "import routing; print([e[2].row_key for e in routing.PROFILES])"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    finally:
        s.close()
    assert r.returncode == 0 and "'docs'" in r.stdout, (r.stdout, r.stderr)
    ok("an invalid routes.json makes `import routing` raise, so the broker does not start; a valid one loads")


# =====================================================================================================
# spawn: a config "read" row cuts its worktree from its own repo
# =====================================================================================================
def test_config_read_row_cuts_from_its_repo():
    class _Stop(Exception):
        pass

    class _WT:
        def __init__(self): self.made = []
        def make(self, key, *a, **kw): self.made.append(kw); raise _Stop()
        def remove(self, key): pass

    s = Site([row()])
    try:
        route = s.load()[0][2]
    finally:
        s.close()
    builtin = routing.Route(domain="ansible", repo="/srv/git/ops", prompt_class="read-report", agent="ansible")
    for r, want in ((route.with_model(), [{"base_repo": REPO}]), (builtin.with_model(), [{}])):
        class _R:
            def route(self, key, brief, _r=r): return _r
        wt = _WT()
        a = CCAdapter(router=_R(), worktree_mgr=wt, prompt_loader=None, envelope_parser=EnvelopeParser(),
                      alert_ops=lambda *a, **k: None)
        a._run_worker("vikunja:task:7", {"task_id": None, "n_events": 1}, "run-7")
        assert wt.made == want, (r.row_key, wt.made)
    ok("a config read row's worktree is cut from its own repo; a built-in read row keeps the default cut")



def test_builtin_rows_never_shadow_or_get_shadowed_by_config_rows():
    # B1: built-in substring rows ahead of the splice would pre-empt these (some with MORE capability)
    for match in ({"label": "wiki-audit"}, {"assignee": "wikibot"}, {"label": "eph:code-change-docs"},
                  {"label": "ansible-notes"}, {"label": "eph:code-change"}, {"assignee": "dev"},
                  {"assignee": "infra-eph"}, {"assignee": "infra-eph", "label": "infra:general"}):
        refused([row(match=match)], "built-in row")
    # N2: nor may a config row take over a built-in fallback trigger after the splice
    for match in ({"assignee": "eph"}, {"assignee": "fleet"}, {"label": "eph:general"}, {"label": "EPH:General"},
                  {"label": "eph:general-docs"}):
        refused([row(match=match)], "built-in row")
    # N7: a config row an earlier config row already covers would never run
    refused([row(), row(id="docs2")], "already taken")
    refused([row(), row(id="docs2", match={"assignee": "helper", "label": "eph:docs"})], "already taken")
    s = Site([row(id="narrow", match={"assignee": "helper", "label": "eph:docs"}), row()])   # narrow first is fine
    try:
        assert [e[2].row_key for e in s.load()] == ["narrow", "docs"]
    finally:
        s.close()
    ok("a config match any built-in row (before or after the splice) or an earlier config row also matches "
       "refuses the file; narrow-before-broad config rows load")


def _spawn_repo(route, ticket):
    """Run _run_worker up to the worktree cut; return (make kwargs list, ops alerts)."""
    class _Stop(Exception):
        pass

    class _WT:
        def __init__(self): self.made = []
        def make(self, key, *a, **kw): self.made.append(kw); raise _Stop()
        def remove(self, key): pass

    class _R:
        def route(self, key, brief): return route

    wt, alerts = _WT(), []
    a = CCAdapter(router=_R(), worktree_mgr=wt, prompt_loader=None, envelope_parser=EnvelopeParser(),
                  ticket_fetcher=lambda tid: ticket, alert_ops=lambda m, **k: alerts.append(m),
                  comment_poster=lambda *a, **k: None)
    a._run_worker("vikunja:task:7", {"task_id": 7, "n_events": 1}, "run-7")
    return [kw.get("base_repo") for kw in wt.made], [m for m in alerts if "rejected" in m]


def test_config_code_and_general_rows_bind_their_repo_at_spawn():
    mine, other = "/srv/git/routes-app", "/srv/git/other-app"     # neither is in the fixture's test-runner map
    saved = (set(routing.CODE_REPO_ALLOWLIST), set(routing.GENERAL_REPO_ALLOWLIST))
    for allow in (routing.CODE_REPO_ALLOWLIST, routing.GENERAL_REPO_ALLOWLIST):
        allow.update((mine, other))                                 # the other repo is reachable on its own
    try:
        s = Site([row(id="svc", capability="code", prompt="svc/p", match={"label": "eph:svc"}, repo=mine),
                  row(id="ask", capability="general", prompt="svc/p", match={"label": "eph:ask"}, repo=mine)],
                 prompts={"svc/p": "Do it.\n"})
        try:
            code, gen = (e[2].with_model() for e in s.load())
        finally:
            s.close()
        for r in (code, gen):
            base = "**id:** 7\n**done:** false\n**description:** fix the docs\n"
            assert _spawn_repo(r, base) == ([mine], []), r.row_key                      # no repo: line -> row repo
            assert _spawn_repo(r, base + f"repo:{mine}\n") == ([mine], []), r.row_key   # same repo is fine
            made, alerts = _spawn_repo(r, base + f"repo:{other}\n")
            assert made == [] and alerts and "differs" in alerts[0], (r.row_key, made, alerts)
        routing.CODE_REPO_ALLOWLIST.discard(mine); routing.GENERAL_REPO_ALLOWLIST.discard(mine)
        for r in (code, gen):                                       # allowlist re-checked at spawn
            made, alerts = _spawn_repo(r, "**id:** 7\n**done:** false\n")
            assert made == [] and alerts and "allowlist" in alerts[0], (r.row_key, made, alerts)
    finally:
        routing.CODE_REPO_ALLOWLIST.clear(); routing.CODE_REPO_ALLOWLIST.update(saved[0])
        routing.GENERAL_REPO_ALLOWLIST.clear(); routing.GENERAL_REPO_ALLOWLIST.update(saved[1])
    ok("config code/general rows cut from their own repo with or without a repo: line, refuse a differing "
       "repo: line, and re-check the allowlist at spawn")


# =====================================================================================================
# site capabilities (broker.yaml `capabilities:`): a built-in base plus extra allow/deny rules
# =====================================================================================================
CAPS = {"docs-writer": {"base": "code", "allow": ["Bash(wc -w:*)"], "deny": ["WebFetch"]},
        "triage": {"base": "read", "allow": ["mcp__tracker__search"], "deny": []},
        "careful": {"base": "general", "deny": ["Bash(curl:*)"]}}


class site_caps:
    def __init__(self, caps):
        self.caps = routing.load_site_capabilities(caps)

    def __enter__(self):
        self.saved, routing.SITE_CAPABILITIES = routing.SITE_CAPABILITIES, self.caps
        return self.caps

    def __exit__(self, *a):
        routing.SITE_CAPABILITIES = self.saved


def _flag(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_no_capabilities_section_changes_nothing():
    assert routing.load_site_capabilities() == {} and routing.SITE_CAPABILITIES == {}
    a = CCAdapter(router=None, worktree_mgr=None, prompt_loader=None, envelope_parser=None)
    for cap in ("read", "code", "write", "mr-review"):
        assert a.build_argv("P", "m", "S", capability=cap) == a.build_argv("P", "m", "S", capability=cap,
                                                                          site_capability="")
    ok("no capabilities section: SITE_CAPABILITIES is empty and every built-in grant is unchanged")


def test_site_capability_grant_is_base_plus_rules():
    a = CCAdapter(router=None, worktree_mgr=None, prompt_loader=None, envelope_parser=None)
    with tempfile.NamedTemporaryFile(suffix=".json") as mcp, site_caps(CAPS) as caps:
        for name, spec in caps.items():
            kw = {"capability": spec["base"], "mcp_config_path": mcp.name}
            base = a.build_argv("P", "m", "S", **kw)
            got = a.build_argv("P", "m", "S", site_capability=name, **kw)
            for flag, extra in (("--allowedTools", spec["allow"]), ("--disallowedTools", spec["deny"])):
                want = _flag(base, flag) if not extra else " ".join([_flag(base, flag), *extra])
                assert _flag(got, flag) == want, (name, flag, _flag(got, flag))
            strip = [t for t in got if t not in (_flag(got, "--allowedTools"), _flag(got, "--disallowedTools"))]
            assert strip == [t for t in base if t not in (_flag(base, "--allowedTools"),
                                                          _flag(base, "--disallowedTools"))], name
            assert _flag(got, "--disallowedTools").startswith(_flag(base, "--disallowedTools")), name
        for bad in (dict(capability="code", site_capability="nope"),       # unknown name
                    dict(capability="read", site_capability="docs-writer")):  # base mismatch
            try:
                a.build_argv("P", "m", "S", **bad)
                raise AssertionError(f"spawned with {bad}")
            except ValueError as e:
                assert "site capability" in str(e)
    ok("a site capability's argv is its base grant with the allow/deny rules appended (the deny floor stays "
       "first and whole); an unknown or mismatched site capability refuses the spawn")


def test_bad_capabilities_refused():
    cases = [
        ({"read": {"base": "read"}}, "built-in capability"),
        ({"Bad_Name": {"base": "read"}}, "lowercase"),
        ({"x": {"base": "write"}}, "base must be"),
        ({"x": {}}, "base must be"),
        ({"x": {"base": "read", "grant": []}}, "unknown key"),
        ({"x": {"base": "read", "allow": "Read"}}, "must be a list"),
        ({"x": {"base": "read", "allow": ["Bash(a(b))"]}}, "must be a list"),
        ({"x": {"base": "read", "deny": ["two words"]}}, "must be a list"),
        ({"x": {"base": "general", "allow": ["Read"]}}, "no allow rules"),
        ({"x": "read"}, "expected"),
    ] + [({"x": {"base": "code", "allow": [r]}}, "every shell command")
         for r in ("Bash", "Bash(*)", "Bash(:*)", "Bash( * )", "Bash(*:*)", "Bash(bash:*)", "Bash(sh -c:*)",
                   "Bash(/usr/bin/env:*)", "Bash(python3 -c:*)", "Bash(xargs -I{} rm)",
                   # quoted heads, VAR= prefixes, command/builtin, versioned interpreters, more runners
                   "Bash(python3.11:*)", 'Bash("bash" -c:*)', "Bash('sh' -c:*)", "Bash(VAR=1 bash:*)",
                   "Bash(command bash:*)", "Bash(builtin eval:*)", "Bash(b'a'sh:*)", "Bash(busybox:*)",
                   "Bash(gawk:*)", "Bash(php:*)", "Bash(tclsh:*)", "Bash(lua5.4:*)", "Bash(deno:*)",
                   "Bash(bun:*)", "Bash(npx:*)", "Bash(uvx:*)", "Bash(setsid:*)", "Bash(uv run:*)",
                   # globbed or expanded heads, command's flags, wrappers
                   "Bash($SHELL:*)", "Bash(${SHELL}:*)", "Bash($'\\x62ash':*)", "Bash(/bin/ba?h:*)",
                   "Bash(b*sh:*)", "Bash(/bin/[b]ash:*)", "Bash(~/bin/sh:*)", "Bash(command -p bash:*)",
                   "Bash(command -v -p sh:*)", "Bash(stdbuf -oL bash:*)", "Bash(ionice -c3 make:*)",
                   "Bash(doas make:*)", "Bash(source ./x:*)", "Bash(. ./x:*)", "Bash(systemd-run:*)",
                   "Bash(mksh:*)", "Bash(nawk:*)",
                   # aliases and versioned names of listed heads
                   "Bash(npm x:*)", "Bash(pnpx:*)", "Bash(bunx:*)", "Bash(tclsh8.6:*)", "Bash(wish8.6:*)",
                   "Bash(ksh93:*)", "Bash(bash5.2:*)", "Bash(node18:*)", "Bash(perl5.36:*)",
                   # script runners: they run files the worker can edit
                   "Bash(make docs)", "Bash(make:*)", "Bash(npm run build)", "Bash(yarn run test)",
                   "Bash(pnpm run lint)", "Bash(bun run x)", "Bash(poetry run pytest)", "Bash(pipenv run x)",
                   "Bash(pdm run x)", "Bash(uv run pytest)", "Bash(tox)", "Bash(tox -e py3)", "Bash(nox:*)",
                   "Bash(cargo run)", "Bash(go run .)")] + [
        ({"x": {"base": "code", "allow": ["Bash(ls, Edit)"]}}, "must be a list"),
        ({"x": {"base": "code", "allow": ["Bash(ls\r)"]}}, "must be a list"),
        ({"x": {"base": "code", "deny": ["Bash(rm\x1b[2K)"]}}, "must be a list")]
    for caps, needle in cases:
        try:
            routing.load_site_capabilities(caps)
            raise AssertionError(f"accepted {caps}")
        except routing.CapabilitiesConfigError as e:
            assert needle in str(e), (caps, needle, str(e))
    try:
        routing.load_site_capabilities(["x"])
        raise AssertionError("accepted a list")
    except routing.CapabilitiesConfigError:
        pass
    assert routing.load_site_capabilities({"x": {"base": "code", "allow": ["Bash(wc -w:*)", "Bash(shellcheck:*)",
                                                                           "Bash(./check.sh)",
                                                                           "Bash(uv sync)", "Bash(publish:*)",
                                                                           "Bash(refresh)", "Bash(hash -r)",
                                                                           "Bash(./publish.sh)", "Bash(makeindex:*)",
                                                                           "Bash(toxic)", "Bash(go fmt)",
                                                                           "Bash(npm view x)", "Bash(cargo fmt)"]}})
    ok("capabilities: built-in names, bad names, unknown bases/keys, malformed rules, allow on general and "
       "shell, interpreter (also versioned like tclsh8.6 or ksh93), runner alias (npm x, pnpx) and script runner "
       "(make, npm run, tox, cargo run) Bash allows are refused; a named command (publish, refresh, hash, "
       "makeindex, even one starting sh...) loads")


def test_config_row_runs_a_site_capability():
    with site_caps(CAPS) as caps:
        s = Site([row(capability="docs-writer"), row(id="tri", match={"label": "eph:tri"}, capability="triage")])
        try:
            entries = load_config_routes(s.path, s.prompts_dir, s.approved, site_caps=caps)
        finally:
            s.close()
        (_, _, docs, _), (_, _, tri, _) = entries
        assert (docs.capability, docs.site_capability, docs.read_only) == ("code", "docs-writer", False)
        assert docs.publish["code_repo_path"] == REPO and docs.test_runner == routing._CODE_ROW.test_runner
        assert (tri.capability, tri.site_capability, tri.read_only) == ("read", "triage", True)
        assert docs.with_model().site_capability == "docs-writer"
        refused([row(capability="docs-writer", repo="/srv/git/elsewhere")], "CODE_REPO_ALLOWLIST")   # base's check
        refused([row(capability="other")], "docs-writer")             # the error lists the site capabilities
        saved = routing.model_label_trusted_creators
        routing.model_label_trusted_creators = lambda: {"alice"}
        try:
            routing.apply_adapter_label(docs.with_model(), ["hermes"], "alice")
            raise AssertionError("hermes label accepted on a site capability")
        except ValueError as e:
            assert "cc harness only" in str(e), e
        finally:
            routing.model_label_trusted_creators = saved
    ok("a config row may name a site capability: it runs as the base (allowlist, plumbing) and carries the name "
       "to build_argv; hermes refuses it")


def test_bad_capabilities_section_stops_import():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "broker.json")
        with open(cfg, "w") as f:
            json.dump({"capabilities": {"x": {"base": "code", "allow": ["Bash"]}}}, f)
        r = subprocess.run([sys.executable, "-c", "import routing"], cwd=ROOT, capture_output=True, text=True,
                           env={**os.environ, "BROKER_CONFIG": cfg}, timeout=60)
    assert r.returncode != 0 and "CapabilitiesConfigError" in r.stderr, r.stderr
    ok("an invalid capabilities section makes `import routing` raise, so the broker does not start")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(tests)} routes.json self-tests...")
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
