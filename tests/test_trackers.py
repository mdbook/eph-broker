#!/usr/bin/env python3
"""Tracker self-tests (trackers/, docs/trackers.md step 2): the base Tracker fails closed, the registry routes
by ref, the Vikunja tracker's mcp.sh argv is IDENTICAL to calling poster.py / ticket_fetch.py with the int id
(a stub BROKER_MCP_SH records it), Ticket.done equals the old done predicate, and a ref that isn't a Vikunja
ticket writes nothing.

stdlib assert only, same runner shape as test_tickets.py.
"""
import json, os, stat, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["BROKER_SITE_ENV"] = os.path.join(HERE, "fixtures", "site.test.env")
os.environ["BROKER_CONFIG"] = os.path.join(HERE, "fixtures", "broker.test.json")
os.environ["BROKER_PROVENANCE_PATH"] = os.path.join(HERE, "fixtures", "provenance.test.json")
os.environ["ROUTES_PATH"] = "/nonexistent/eph-test/routes.json"

# A stub mcp.sh: appends its argv to a log and answers like the bridge. Set BEFORE site_env loads (setdefault)
# and before poster / ticket_fetch bind their mcp_sh default, so the real defaults exec the stub.
_TMP = tempfile.TemporaryDirectory()
STUB, STUB_LOG = os.path.join(_TMP.name, "mcp.sh"), os.path.join(_TMP.name, "argv.log")
with open(STUB, "w") as f:
    f.write(f"""#!{sys.executable}
import json, sys
with open({STUB_LOG!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
call = json.loads(sys.argv[3])
text = ""
if call["name"] == "vikunja_task_crud":
    tid = call["arguments"]["id"]
    text = ("**id:** %d\\n**title:** fix the thing\\n**description:** <p>it is broken</p>\\n**done:** false\\n"
            '**assignees:** [\\n  {{\\n    "username": "dev"\\n  }}\\n]\\n'
            '**labels:** [\\n  {{\\n    "title": "eph:code-change"\\n  }}\\n]\\n**project_id:** 912\\n'
            '**created_by:** {{\\n  "username": "svc-account"\\n}}\\n') % tid
print(json.dumps({{"result": {{"content": [{{"type": "text", "text": text}}] if text else []}}}}))
""")
os.chmod(STUB, os.stat(STUB).st_mode | stat.S_IXUSR)
os.environ["BROKER_MCP_SH"] = STUB
import site_env  # noqa: F401,E402

import poster, ticket_fetch, tickets  # noqa: E402
from trackers import Registry, Ticket, Tracker  # noqa: E402
from trackers.vikunja import VikunjaTracker  # noqa: E402
import trackers.gitlab as GL  # noqa: E402
from adapters.cc import CCAdapter, _ticket_text_is_done  # noqa: E402

PASS = []


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


def _argv():
    """The argv lines the stub recorded since the last call, then clear the log."""
    try:
        with open(STUB_LOG) as f:
            lines = [json.loads(ln) for ln in f]
    except FileNotFoundError:
        return []
    os.unlink(STUB_LOG)
    return lines


def _adapter(**kw):
    return CCAdapter(router=None, worktree_mgr=None, prompt_loader=None, envelope_parser=None, **kw)


def test_base_tracker_fails_closed():
    t = Tracker()
    for ref in ("vikunja:task:1", "github:o/r#1", None, 42, ""):
        assert t.verify({}, b"") is False and t.normalize({}, {}) == []
        assert t.fetch(ref) is None and t.url(ref) == ""
        assert t.comment(ref, "<p>x</p>", "r") is False and t.label(ref, "parked") is False
        assert t.assign(ref, 1) is False and t.close(ref) is False and t.is_self({"doer": "x"}) is True
        assert t.link_mr(ref, "u", "t", "r") is False and t.review(ref, "<p>x</p>", "r") is False
    assert t.link_mr("r", None, None, "r") is False and t.link_mr("r", 5, 6, "r") is False   # odd input, no raise

    class Rec(Tracker):
        def __init__(self): self.seen = []
        def comment(self, ref, html_body, run_id): self.seen.append((ref, html_body, run_id)); return True
    r = Rec()
    assert r.review("x:1", "<p>v</p>", "run") is True and r.seen[-1] == ("x:1", "<p>v</p>", "run")
    assert r.link_mr("x:1", "https://h/g/a/-/merge_requests/3", "<b>t</b>", "run") is True
    assert "merge_requests/3" in r.seen[-1][1] and "&lt;b&gt;t&lt;/b&gt;" in r.seen[-1][1]
    ok("base Tracker: every method fails closed and never raises; review and link_mr default to comment")


def test_registry_routes_by_tracker():
    v = VikunjaTracker()
    reg = Registry([v])
    assert reg.get("vikunja:task:42") is v and reg.get("vikunja:task:0") is v
    for ref in ("gitlab:g/a#7", "github:o/r#45", "mr:g/a!3", "zulip:bot:9", "gmail:me", "vikunja:task:?",
                "vikunja:task:", None, 42, ""):
        assert reg.get(ref) is None, ref
    assert Registry().get("vikunja:task:42") is None
    ok("Registry.get routes vikunja refs to the Vikunja tracker; gitlab/github/non-ticket keys get None")


def test_vikunja_argv_parity():
    _argv()
    assert poster.post_comment(42, "<p>hi</p>", "run-1") is True
    assert poster.apply_label(42) is True
    assert poster.assign_user(42, 7) is True
    text = ticket_fetch.fetch_ticket(42)
    direct = _argv()
    assert len(direct) == 4 and text, direct
    v, ref = VikunjaTracker(), "vikunja:task:42"
    assert v.comment(ref, "<p>hi</p>", "run-1") is True
    assert v.label(ref, "parked") is True
    assert v.assign(ref, 7) is True
    t = v.fetch(ref)
    assert _argv() == direct, "the Vikunja tracker execs exactly what the poster / fetcher did"
    assert direct[0][:2] == ["vikunja-bridge", "tools/call"] and json.loads(direct[0][2]) == {
        "name": "vikunja_task_comments", "arguments": {
            "operation": "comment", "id": 42, "comment": "<p>hi</p>\n<!-- broker-run:run-1 -->"}}
    assert isinstance(t, Ticket) and t.text == text and t.ref == ref
    assert (t.title, t.done, t.labels, t.assignees, t.creator, t.project) == (
        "fix the thing", False, ["eph:code-change"], ["dev"], "svc-account", "912"), t
    assert t.url == v.url(ref) == site_env.ticket_url(42) and t.is_pr is False
    assert v.fetch_text(ref) == text and len(_argv()) == 1
    # the wiring app.py builds: CCAdapter over the real sinks, through its late-bound registry
    a = _adapter(comment_poster=poster.post_comment, ticket_fetcher=ticket_fetch.fetch_ticket,
                 label_poster=poster.apply_label)
    assert a._tracker_comment(ref, "<p>hi</p>", "run-1") is True and a._tracker_label(ref, "parked") is True
    assert a.tracker(ref).assign(ref, 7) is True and a._tracker_fetch(ref).text == text
    assert _argv() == direct, "CCAdapter's tracker execs the same argv"
    # a numeric STRING id (the old rerun path) and its ref land on the same int
    assert poster.post_comment("42", "<p>hi</p>", "run-1") is True and _argv() == direct[:1]
    ok("VikunjaTracker comment/label/assign/fetch exec argv IDENTICAL to poster / ticket_fetch with the int id "
       "(stub mcp.sh), directly and through CCAdapter's registry")


def test_ticket_done_matches_old_predicate():
    lst = "### 1. **t** (ID: 9)\n- **Status:** {}\n- **Description:** <p>x</p>\n**done:** true\n"
    real = ("**id:** 9\n**title:** t\n**description:** <p>it is broken</p>\n**done:** {}\n"
            '**created_by:** {{\n  "username": "svc-account"\n}}\n')
    corpus = ["**id:** 1\n**done:** true\n**due_date:** x", "**id:** 1\n**done:** false\n", "**id:** 1\n**title:** t\n",
              "", "**description:** <p>mark done: true when finished</p>", "**description:** <p>**done:** true</p>",
              "**Done:** true", "**done:** True", "**DONE:** TRUE", "**id:** 1\n**Done:** True\n**due:** x",
              "see: **done:** true inline", "<p>**Done:** true</p>", "**Done:** false",
              real.format("true"), real.format("false"),
              real.format("false").replace("<p>it is broken</p>", "<p>x</p>\n**done:** true\n<p>y</p>"),
              real.format("true") + "x" * ticket_fetch.MAX_TICKET_TEXT,
              ticket_fetch._reformat_list_task(lst.format("✅ Done"), 9),
              ticket_fetch._reformat_list_task(lst.format("❌ Not Done"), 9)]
    seen = set()
    for text in corpus:
        t = VikunjaTracker(fetch_text=lambda tid, s=text: s).fetch("vikunja:task:9")
        assert t is not None and t.done is _ticket_text_is_done(text), text
        seen.add(t.done)
    assert seen == {True, False}
    assert VikunjaTracker(fetch_text=lambda tid: None).fetch("vikunja:task:9") is None
    assert _ticket_text_is_done(None) is False
    ok("Ticket.done equals the old _ticket_text_is_done on every rendered shape (fake, truncated, list fallback)")


def test_non_vikunja_ref_writes_nothing():
    _argv()
    v = VikunjaTracker()
    for ref in (None, "gitlab:g/a#7", "github:o/r#45", "mr:g/a!3", "vikunja:task:x", 42):
        assert v.comment(ref, "<p>x</p>", "r") is False and v.label(ref, "parked") is False
        assert v.assign(ref, 7) is False and v.fetch(ref) is None and v.fetch_text(ref) is None
        assert v.url(ref) == ""
    assert v.label("vikunja:task:42", "blocked") is False, "only the parked label is wired"
    assert _argv() == [], "nothing reached mcp.sh"
    calls = []
    a = _adapter(comment_poster=lambda *x: calls.append(x), label_poster=lambda *x: calls.append(x),
                 ticket_fetcher=lambda *x: calls.append(x))
    for ref in (None, "github:o/r#45"):
        assert a._tracker_comment(ref, "<p>x</p>", "r") is False and a._tracker_label(ref, "parked") is False
        assert a._tracker_fetch(ref) is None and a._ticket_link(ref) == "" and a.tracker(ref) is None
    a._post_ticket_note("github:o/r#45", "<p>x</p>", "r", parked=True)
    a._post_ticket_note("zulip:bot:9", "<p>x</p>", "r", parked=True)
    assert calls == []
    a._ticket_fetcher = lambda tid: calls.append(tid) or "late"          # swapped after construction
    assert a._tracker_fetch("vikunja:task:5").text == "late" and calls == [5]
    ok("a None or non-Vikunja ref makes no call; the adapter's sinks are late-bound (swap after construction works)")


class _Resp:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self.data


def _fake_gitlab(fn):
    """Run fn(sent) with gitlab_mr's urlopen recording (method, url, json body, token) and GITLAB_API set; both
    token vars are cleared before and restored after."""
    import gitlab_mr as G
    sent = []
    def urlopen(req, timeout=None):
        sent.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None,
                     req.get_header("Private-token")))
        return _Resp(b'{"id": 1}')
    names = (GL.GITLAB_ISSUE_TOKEN_ENV, G.GITLAB_MR_TOKEN_ENV)
    saved = {n: os.environ.pop(n, None) for n in names}
    orig_open, orig_api = G.urllib.request.urlopen, G.GITLAB_API
    G.urllib.request.urlopen, G.GITLAB_API = urlopen, "https://gitlab.example.test/api/v4"
    try:
        fn(sent)
    finally:
        G.urllib.request.urlopen, G.GITLAB_API = orig_open, orig_api
        for n, v in saved.items():
            os.environ.pop(n, None)
            if v is not None:
                os.environ[n] = v


def test_gitlab_tracker_comment_and_url():
    import gitlab_mr as G
    t = GL.GitLabTracker()
    assert GL.parse_ref("gitlab:grp/sub/app#7") == ("grp/sub/app", 7)
    for bad in (None, 7, "gitlab:grp/app", "gitlab:grp/app#x", "github:o/r#4", "vikunja:task:4", "gitlab:#7"):
        assert GL.parse_ref(bad) is None, bad
    assert t.url("gitlab:grp/sub/app#7") == "https://gitlab.example.test/grp/sub/app/-/issues/7"
    assert t.url("github:o/r#4") == "" and t.label("gitlab:g/a#7", "parked") is False and t.fetch("gitlab:g/a#7") is None

    def run(sent):
        assert t.comment("gitlab:grp/sub/app#7", "<p>x</p>", "r0") is False and sent == [], "no token, no call"
        os.environ[G.GITLAB_MR_TOKEN_ENV] = "mr-tok-test-only"
        assert t.comment("gitlab:grp/sub/app#7", "<p>done</p>\n/close\n  /label ~x", "run-1") is True
        m, url, body, tok = sent[-1]
        assert m == "POST" and url.endswith("/projects/grp%2Fsub%2Fapp/issues/7/notes"), url
        assert tok == "mr-tok-test-only", "falls back to the MR token"
        assert body == {"body": "<p>done</p>\n\u200b/close\n  \u200b/label ~x\n<!-- broker-run:run-1 -->"}, body
        os.environ[GL.GITLAB_ISSUE_TOKEN_ENV] = "issue-tok-test-only"
        assert t.comment("gitlab:g/a#8", "<p>y @all, see #3 and !4</p>", "run-2") is True
        assert sent[-1][3] == "issue-tok-test-only"
        assert sent[-1][2]["body"].startswith("<p>y @\u200ball, see #\u200b3 and !\u200b4</p>"), sent[-1][2]
        n = len(sent)
        assert t.comment("github:o/r#4", "<p>y</p>", "r") is False and len(sent) == n, "not a GitLab ref"
    _fake_gitlab(run)

    def raising(sent):
        os.environ[G.GITLAB_MR_TOKEN_ENV] = "mr-tok-test-only"
        G.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        assert t.comment("gitlab:g/a#7", "<p>x</p>", "r") is False, "an API failure is False, never raises"
    _fake_gitlab(raising)
    ok("GitLab tracker: issue note at /projects/<quoted>/issues/<iid>/notes with the run marker and quick actions "
       "defused; issue token, else MR token, else no call; url from GITLAB_WEB_URL; bad refs and errors are False")


def test_gitlab_issue_writeback_in_adapter():
    """A GitLab-issue subject (no task_id) gets the envelope summary as an issue note, a blocked result is posted
    but never labelled, a Vikunja subject still uses its own sink, and an unmapped project's spawn-time reject
    says so on the issue."""
    import routing
    from envelope import EnvelopeParser
    gl, vik = [], []
    a = _adapter(comment_poster=lambda *x: vik.append(x), label_poster=lambda *x: vik.append(("label",) + x))
    a._trackers.get("gitlab:g/a#7").comment = lambda ref, body, rid: gl.append((ref, body, rid)) or True

    class _W:
        key, write_grant_path, turn_capped, last_summary = "gitlab:g/a#7", None, False, None
    w = _W()
    a._post_envelope({"n_events": 1}, "run-1", {"status": "done", "summary": "<p>fixed</p>"}, w)
    a._post_envelope({"n_events": 1}, "run-2", {"status": "blocked", "summary": "<p>stuck</p>",
                                                "needs_operator": True}, w)
    assert gl == [("gitlab:g/a#7", "<p>fixed</p>", "run-1"), ("gitlab:g/a#7", "<p>stuck</p>", "run-2")], gl
    assert w.last_summary == "<p>stuck</p>" and vik == [], vik
    w.key = "vikunja:task:42"
    a._post_envelope({"task_id": 42, "n_events": 1}, "run-3", {"status": "done", "summary": "<p>v</p>"}, w)
    assert vik == [(42, "<p>v</p>", "run-3")] and len(gl) == 2, (vik, gl)

    class _WT:
        made = 0
        def make(self, *a, **k):
            self.made += 1
            raise RuntimeError("stop")
        def remove(self, key): pass
    alerts, wt = [], _WT()
    b = CCAdapter(router=routing.Router(), worktree_mgr=wt, prompt_loader=None, envelope_parser=EnvelopeParser(),
                  alert_ops=alerts.append)
    b._trackers.get("gitlab:g/a#7").comment = lambda ref, body, rid: gl.append((ref, body, rid)) or True
    b._run_worker("gitlab:g/unmapped#7", {"n_events": 1, "source": "gitlab", "issue": {
        "source": "gitlab", "repo_ident": "g/unmapped", "issue_n": 7, "title": "t", "body": "b"}}, "run-rej")
    assert wt.made == 0 and len(alerts) == 1 and "ISSUE_REPO_MAP" in alerts[0], alerts
    assert gl[-1][0] == "gitlab:g/unmapped#7" and "Not started" in gl[-1][1] and gl[-1][2] == "run-rej", gl
    ok("GitLab issue subject: summary and blocked results posted on the issue (no label), Vikunja unchanged, "
       "an unmapped-project reject comments on the issue")


def test_gitlab_issue_ops_notices_and_reject_over_api():
    """Over the real gitlab_mr._api (fake urlopen): push-refused names hosts and settings, so on a GitLab issue
    it makes no call (Vikunja still gets it); the unmapped-project reject does reach the issue notes endpoint."""
    import gitlab_mr as G
    import routing
    from adapters.cc import _ops_ref
    from envelope import EnvelopeParser
    assert _ops_ref("gitlab:g/a#7") is None and _ops_ref("github:o/r#4") is None
    assert _ops_ref("vikunja:task:4") == "vikunja:task:4"

    class _WT:
        def make(self, *a, **k): raise RuntimeError("stop")
        def remove(self, key): pass

    def run(sent):
        os.environ[G.GITLAB_MR_TOKEN_ENV] = "mr-tok-test-only"
        vik = []
        a = _adapter(comment_poster=lambda *x: vik.append(x))
        a._nag_push_refused("gitlab:g/a#7", "r1", "push host /srv/x not in BROKER_PUSH_HOSTS")
        assert sent == [] and vik == [], (sent, vik)
        a._nag_push_refused("vikunja:task:4", "r2", "push host not allowed")
        assert len(vik) == 1 and vik[0][0] == 4 and sent == [], vik
        b = CCAdapter(router=routing.Router(), worktree_mgr=_WT(), prompt_loader=None,
                      envelope_parser=EnvelopeParser(), alert_ops=lambda *x, **k: None)
        b._run_worker("gitlab:g/unmapped#7", {"n_events": 1, "source": "gitlab", "issue": {
            "source": "gitlab", "repo_ident": "g/unmapped", "issue_n": 7, "title": "t", "body": "b"}}, "run-rej")
        assert len(sent) == 1 and sent[0][0] == "POST", sent
        assert sent[0][1].endswith("/projects/g%2Funmapped/issues/7/notes"), sent[0][1]
        assert "Not started" in sent[0][2]["body"] and sent[0][2]["body"].endswith("<!-- broker-run:run-rej -->")
    _fake_gitlab(run)
    ok("GitLab issue: push-refused (host paths) never posts there, Vikunja still does; the unmapped-project "
       "reject posts to the issue notes endpoint through _api")


def test_gitlab_issue_mr_description():
    from adapters.cc import _mr_description
    assert _mr_description("vikunja:task:4", "b", "main", "<p>x</p>") is None, "other subjects keep the default"
    assert _mr_description("github:o/r#4", "b", "main", "<p>x</p>") is None
    d = _mr_description("gitlab:grp/app#7", "eph/issue-x", "main",
                        "<p>Fixed the parser.</p><p>Closes #3, fixes grp/other#4 and "
                        "https://gitlab.example.test/grp/app/-/issues/9, /-/work_items/10 and "
                        "/-/issues/incident/11</p>\n/merge")
    assert d.startswith("Automated code-change proposal from the ephemeral code worker (key=gitlab:grp/app#7).")
    assert "\n\nCloses grp/app#7\n\n" in d, d
    summary = d.split("not checked by the broker):\n\n", 1)[1]
    assert summary == ("> Fixed the parser.\n> Closes #\u200b3, fixes grp/other#\u200b4 and "
                       "https://gitlab.example.test/grp/app/-/issues/\u200b9, /-/work_items/\u200b10 and "
                       "/-/issues/incident/\u200b11\n> \n> /merge"), repr(summary)
    import re as _re
    assert _re.findall(r"#[0-9]+", d) == ["#7", "#7"], "only the broker's own reference survives"
    assert _mr_description("gitlab:g/a#1", "b", "main", None).endswith("> (no summary)")
    ok("GitLab-issue MR description: default text + Closes <project>#<iid> + the worker summary quoted with its "
       "issue references broken; other subjects keep open_mr's default")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(tests)} tracker self-tests...")
    FAILURES = []
    for t in tests:
        try:
            t()
        except BaseException as e:   # noqa: BLE001
            FAILURES.append((t.__name__, e))
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    _TMP.cleanup()
    if FAILURES:
        print(f"\n{len(FAILURES)} of {len(tests)} CHECKS FAILED ({len(PASS)} passed):")
        for name, e in FAILURES:
            print(f"  - {name}: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"\nALL {len(PASS)} CHECKS PASSED")
