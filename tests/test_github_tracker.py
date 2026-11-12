"""Native GitHub Issues adapter; synthetic payloads and HTTP doubles, never real GitHub."""
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["BROKER_SITE_ENV"] = str(HERE / "fixtures" / "site.test.env")
os.environ["BROKER_CONFIG"] = str(HERE / "fixtures" / "broker.test.json")
os.environ["BROKER_PROVENANCE_PATH"] = str(HERE / "fixtures" / "provenance.test.json")

import coalescer
import routing
from trackers import Registry
import trackers.github as GH


def _payload():
    return {"action": "opened", "repository": {"full_name": "Owner/Repo"},
            "issue": {"number": 9042, "title": "Fix parsing", "body": "Details", "state": "open",
                      "user": {"login": "not-the-actor"}},
            "sender": {"login": "maintainer", "type": "User"}}


def _signature(raw):
    return "sha256=" + hmac.new(b"test-only", raw, hashlib.sha256).hexdigest()


def test_verify_fails_closed_and_signs_exact_bytes():
    t, raw = GH.GitHubTracker(), b'{"text":"hello"}'
    with patch.dict(os.environ, {}, clear=True):
        assert not t.verify({"X-Hub-Signature-256": _signature(raw)}, raw)
        os.environ[GH.GITHUB_WEBHOOK_SECRET_ENV] = "test-only"
        assert t.verify({"X-Hub-Signature-256": _signature(raw)}, raw)
        for sig in (None, "", "sha1=bad", "sha256=wrong", "non-ascii-\u2603"):
            assert not t.verify({"X-Hub-Signature-256": sig}, raw)
        assert not t.verify({"X-Hub-Signature-256": _signature(raw)}, raw + b" ")


def test_opened_issue_uses_actor_and_existing_routing_and_brief():
    t = GH.GitHubTracker()
    evt, = t.normalize({"X-GitHub-Event": "issues"}, _payload())
    assert evt["ref"] == coalescer.normalize(evt) == "github:owner/repo#9042"
    assert evt["doer"] == "maintainer"
    assert evt["web_url"] == "https://github.com/owner/repo/issues/9042"
    assert coalescer.collapse([evt])["issue"] == {
        "source": "github", "repo_ident": "owner/repo", "issue_n": 9042,
        "title": "Fix parsing", "body": "Details", "web_url": evt["web_url"]}
    with patch.object(routing, "ISSUE_AUTHOR_ALLOWLIST", set()):
        assert routing.Router().classify(evt["ref"], evt) is None
    with patch.object(routing, "ISSUE_AUTHOR_ALLOWLIST", {"maintainer"}):
        route = routing.Router().classify(evt["ref"], evt)
        assert route == "code"
    with patch.object(routing, "ISSUE_REPO_MAP", {}):
        try:
            routing.resolve_issue_repo("github", evt["repo"])
        except ValueError:
            pass
        else:
            raise AssertionError("unmapped repository must fail closed")
    with patch.object(routing, "ISSUE_REPO_MAP", {"github:owner/repo": "/srv/git/mirror"}):
        assert routing.resolve_issue_repo("github", evt["repo"]) == "/srv/git/mirror"
    assert Registry([t]).get(evt["ref"]) is t


def test_unsupported_malformed_and_bot_payloads_are_ignored():
    t, headers = GH.GitHubTracker(), {"X-GitHub-Event": "issues"}
    for event in ("ping", "issue_comment", "pull_request", "pull_request_review",
                  "pull_request_review_comment"):
        assert t.normalize({"X-GitHub-Event": event}, _payload()) == []
    for payload in (None, [], "bad", {}, {"action": "opened", "issue": []}):
        assert t.normalize(headers, payload) == []
    for section, key, value in (
        ("issue", "number", True), ("issue", "number", "9042"), ("issue", "number", 0),
        ("issue", "title", []), ("issue", "body", {}), ("issue", "state", "closed"),
        ("issue", "pull_request", {}), ("repository", "full_name", "../repo"),
        ("repository", "full_name", "owner/repo?x"), ("repository", "full_name", "x" * 201),
        ("sender", "login", None), ("sender", "login", "a\nb"), ("sender", "type", "Bot")):
        p = _payload()
        p[section][key] = value
        assert t.normalize(headers, p) == [], (section, key, value)
    p = _payload()
    for action in ("edited", "labeled", "closed", "reopened", None):
        p["action"] = action
        assert t.normalize(headers, p) == []
    with patch.dict(os.environ, {"GITHUB_BOT_LOGIN": "MAINTAINER"}):
        assert t.normalize(headers, _payload()) == []
    p = _payload()
    p["issue"]["body"] = "x" * (GH.MAX_TICKET_TEXT + 1)
    assert len(t.normalize(headers, p)[0]["body"]) == GH.MAX_TICKET_TEXT


def test_comment_is_scoped_capped_stamped_and_errors_are_false():
    t, ref = GH.GitHubTracker(), "github:owner/repo#9042"
    sent = []

    class Response:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *args): pass

    class Opener:
        def open(self, req, timeout):
            sent.append(req)
            assert timeout == 15
            return Response()

    with patch.dict(os.environ, {}, clear=True), \
            patch.object(GH.urllib.request, "build_opener", return_value=Opener()) as opener:
        assert not t.comment(ref, "x", "run") and not sent
        os.environ[GH.GITHUB_TOKEN_ENV] = "test-only"
        for bad in (None, 9042, "github:o/r#01", "github:o/r#-1", "github:o/r#1?x",
                    "github:o/../r#1", "github:../r#1", "github:o/r%2Fx#1", "gitlab:o/r#1"):
            assert not t.comment(bad, "x", "run")
            assert t.url(bad) == ""
        assert not sent
        assert t.comment(ref, "<p>Fixed @someone, see #2</p>", "r1")
        req = sent[-1]
        assert req.full_url == "https://api.github.com/repos/owner/repo/issues/9042/comments"
        assert req.method == "POST" and req.get_header("Authorization") == "Bearer test-only"
        body = json.loads(req.data)["body"]
        assert body == "<p>Fixed @\u200bsomeone, see #\u200b2</p>\n<!-- broker-run:r1 -->"
        assert isinstance(opener.call_args[0][0], GH._NoRedirect)
        assert opener.call_args[0][0].redirect_request(None, None, 302, "", {}, "https://evil.test") is None
        # Re-delivery of this write cannot start a worker, even before comment intake is implemented.
        p = _payload()
        p["comment"] = {"body": body}
        assert t.normalize({"X-GitHub-Event": "issue_comment"}, p) == []
        assert t.comment(ref, "x" * (GH.MAX_TICKET_TEXT + 1), "r2")
        assert len(json.loads(sent[-1].data)["body"].split("\n<!--")[0]) == GH.MAX_TICKET_TEXT
        Response.status = 200
        assert not t.comment(ref, "x", "r3")
        opener.side_effect = OSError("offline")
        assert not t.comment(ref, "x", "r4")
    assert t.close(ref) is False and t.label(ref, "parked") is False and t.assign(ref, "bot") is False


def test_native_endpoint_auth_json_author_gates_and_size_limit():
    import app
    seen = []
    h = object.__new__(app.Handler)
    h.path = "/webhooks/github"
    replies = []
    h._reply = lambda code, body: replies.append((code, body))
    h.headers = {"X-GitHub-Event": "issues"}
    raw = json.dumps(_payload()).encode()
    h._body = lambda: raw
    broker = SimpleNamespace(ingest=lambda evt: seen.append(evt) or coalescer.normalize(evt))
    with patch.dict(os.environ, {GH.GITHUB_WEBHOOK_SECRET_ENV: "test-only"}), \
            patch.object(app, "BROKER", broker), \
            patch.object(routing, "ISSUE_AUTHOR_ALLOWLIST", {"maintainer"}):
        h._post()
        assert replies[-1][0] == 401 and not seen
        h.headers["X-Hub-Signature-256"] = _signature(raw)
        h._post()
        assert replies[-1] == (200, {"ok": True, "subject_keys": ["github:owner/repo#9042"]})
        assert len(seen) == 1
        with patch.object(routing, "ISSUE_AUTHOR_ALLOWLIST", set()):
            h._post()
            assert replies[-1][1]["subject_keys"] == [] and len(seen) == 1
        raw = b"bad json"
        h.headers["X-Hub-Signature-256"] = _signature(raw)
        h._post()
        assert replies[-1][0] == 400
        raw = b"[]"
        h.headers["X-Hub-Signature-256"] = _signature(raw)
        h._post()
        assert replies[-1] == (200, {"ok": True, "subject_keys": []})
        # _body already replied 413: the adapter must not parse or ingest anything further.
        h._body = lambda: None
        count = len(replies)
        h._post()
        assert len(replies) == count and len(seen) == 1


def test_worker_summaries_reach_github_registry_sink():
    from adapters.cc import CCAdapter, scrub_worker_env
    a = CCAdapter(router=None, worktree_mgr=None, prompt_loader=None, envelope_parser=None)
    tracker = a.tracker("github:owner/repo#9042")
    assert isinstance(tracker, GH.GitHubTracker)
    worker = SimpleNamespace(key="github:owner/repo#9042", write_grant_path=None,
                             turn_capped=False, last_summary=None)
    with patch.object(tracker, "comment", return_value=True) as comment:
        a._post_envelope({"n_events": 1}, "run", {"status": "done", "summary": "<p>fixed</p>"}, worker)
        comment.assert_called_once_with(worker.key, "<p>fixed</p>", "run")
    env = scrub_worker_env({"PATH": "/usr/bin", "GITHUB_TOKEN": "test-only",
                            "GITHUB_WEBHOOK_SECRET": "test-only"})
    assert "GITHUB_TOKEN" not in env and "GITHUB_WEBHOOK_SECRET" not in env


if __name__ == "__main__":
    failures = []
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failures.append(test.__name__)
            print(f"  FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - len(failures)} passed, {len(failures)} failed")
    sys.exit(1 if failures else 0)
