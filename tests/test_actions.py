"""Dashboard write path (actions.py): auth gate, each verb, audit. No worker, tracker, bus or production state."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_drain as T   # sets the fixture env before app imports, and gives a durable Broker harness
import actions
import app
import coalescer as C
import routing_overrides as RO

SECRET = "synthetic-proxy-secret"
ON = {"BROKER_ACTIONS_ENABLED": "1", "BROKER_ACTIONS_GROUP": "ops", "BROKER_ACTIONS_PROXY_SECRET": SECRET,
      "BROKER_STREAM_USER_HEADER": "X-Auth-Username", "BROKER_STREAM_GROUPS_HEADER": "X-Auth-Groups",
      "BROKER_ACTIONS_AUDIT_HANDLE": ""}
GOOD = {"X-Broker-Proxy-Secret": SECRET, "X-Auth-Username": "alice", "X-Auth-Groups": "users|ops",
        "X-Broker-Action": "1", "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}


class ActionTests(T.DrainTests):
    def post(self, b, verb, body, headers=GOOD):
        return self.handler(b, "/actions/" + verb, body, headers).do_POST()

    def live_worker(self, b, tid=5):
        key = b.ingest(T.event(tid))
        self.pump(b)
        assert b.coalescer.live[key]["state"] == "open_running"
        return key

    def test_off_by_default_404_and_dash_sees_disabled(self):
        b = self.broker()
        with patch.dict(os.environ, {**ON, "BROKER_ACTIONS_ENABLED": ""}):
            assert self.post(b, "drain", {"enabled": True})[0] == 404
            assert self.handler(b, "/actions").do_GET() == (200, {"enabled": False, "allowed": False})
        assert not b.drain_status()["enabled"]

    def test_every_gate_fails_closed(self):
        b = self.broker()
        bad = [{**GOOD, "X-Broker-Proxy-Secret": "wrong"},
               {k: v for k, v in GOOD.items() if k != "X-Broker-Proxy-Secret"},
               {k: v for k, v in GOOD.items() if k != "X-Auth-Username"},
               {**GOOD, "X-Auth-Groups": "users|opsx"},
               {k: v for k, v in GOOD.items() if k != "X-Broker-Action"},
               {**GOOD, "Content-Type": "text/plain"},
               {**GOOD, "Sec-Fetch-Site": "cross-site"}]
        with patch.dict(os.environ, ON):
            for h in bad:
                assert self.post(b, "drain", {"enabled": True}, h)[0] == 403, h
            assert not b.drain_status()["enabled"]
            assert self.handler(b, "/actions", headers={**GOOD, "X-Auth-Groups": "users"}).do_GET()[1]["allowed"] is False
            assert self.handler(b, "/actions", headers=GOOD).do_GET()[1] == {"enabled": True, "allowed": True}
        for unset in ("BROKER_ACTIONS_PROXY_SECRET", "BROKER_ACTIONS_GROUP", "BROKER_STREAM_USER_HEADER"):
            with patch.dict(os.environ, {**ON, unset: ""}):
                assert self.post(b, "drain", {"enabled": True})[0] == 403, unset
        with patch.dict(os.environ, {**ON, "BROKER_STREAM_USER_HEADER": "X-Forwarded-User"}):
            assert self.post(b, "drain", {"enabled": True}, {**GOOD, "X-Forwarded-User": "alice"})[0] == 403

    def test_drain_and_audit_trail(self):
        b = self.broker()
        with patch.dict(os.environ, {**ON, "BROKER_ACTIONS_AUDIT_HANDLE": "audit-bot"}), \
                patch.object(actions.events, "emit") as emit, patch.object(actions.notify, "dm") as dm, \
                patch.object(actions.threading, "Thread", lambda target, args, daemon: type(
                    "T", (), {"start": lambda self: target(*args)})()):
            code, body = self.post(b, "drain", {"enabled": True})
            assert code == 200 and body["drain"]["enabled"]
            assert self.post(b, "drain", {"enabled": "yes"})[0] == 400
        assert emit.call_args_list[0].kwargs == {"subject_key": None, "user": "alice", "verb": "drain", "result": "ok"}
        assert emit.call_args_list[1].kwargs["result"] == "refused-400"
        assert dm.call_args_list[0].args == ("audit-bot", "WEB-ACTION user=alice verb=drain target=- result=ok")

    def test_cancel_stops_marks_completed_and_drops_buffer(self):
        b = self.broker()
        key = self.live_worker(b)
        torn = []
        b.adapter.teardown = torn.append
        b.ingest(T.event(5, event_name="task.updated"))         # buffered update: must NOT re-arm after cancel
        with patch.dict(os.environ, ON):
            code, body = self.post(b, "cancel", {"subject_key": key})
            assert (code, body["dropped_buffered"]) == (200, 1)
            for t in b.liveness._dispatch:
                t.join(5)
            assert torn == [key] and key not in b.coalescer.live and key in b.coalescer.completed
            assert key not in b.coalescer.pending
            assert self.post(b, "cancel", {"subject_key": key})[0] == 409
            assert self.post(b, "cancel", {})[0] == 400

    def test_cancel_refused_while_the_run_is_still_being_set_up(self):
        b = self.broker()
        key = self.live_worker(b)
        gate = T.threading.Event()
        setup = T.threading.Thread(target=gate.wait)          # a setup thread blocked on a repo lock, no Worker yet
        setup.start()
        b.adapter._threads[key] = setup
        try:
            with patch.dict(os.environ, ON):
                code, body = self.post(b, "cancel", {"subject_key": key})
            assert code == 409 and "still starting" in body["error"]
            assert b.coalescer.live[key]["state"] == "open_running"
        finally:
            gate.set(); setup.join(5)

    def test_message_rides_the_next_turn_as_untrusted_comment(self):
        b = self.broker()
        key = self.live_worker(b)
        with patch.dict(os.environ, ON):
            assert self.post(b, "message", {"subject_key": key, "text": "  "})[0] == 400
            assert self.post(b, "message", {"subject_key": key, "text": "x" * 4001})[0] == 400
            assert self.post(b, "message", {"subject_key": "vikunja:task:1", "text": "hi"})[0] == 409
            assert self.post(b, "message", {"subject_key": key, "text": "use the <b>v2</b> layout"})[0] == 200
        buf = b.coalescer.live[key]["fwd_buffer"]
        assert buf[-1]["doer"] == "web:alice" and buf[-1]["task_id"] == 9005
        brief = C.collapse(buf)
        assert brief["comments"][0]["text"] == f"{C._COMMENT_BEGIN}\nuse the v2 layout\n{C._COMMENT_END}"
        assert "UNTRUSTED" in brief["comments_notice"]

    def test_rerun_and_reconcile_reuse_the_operator_paths(self):
        b = self.broker()
        with patch.dict(os.environ, ON):
            assert self.post(b, "rerun", {"task_id": "nope"})[0] == 400
            assert self.post(b, "reconcile", {"subject_key": "vikunja:task:1", "decision": "retry"})[0] == 409
            assert self.post(b, "reconcile", {"subject_key": "vikunja:task:1", "decision": "retry", "note": 3})[0] == 400
            assert self.post(b, "nuke", {})[0] == 404

    def test_more_gates_duplicates_and_short_secret(self):
        import email.message
        b = self.broker()
        with patch.dict(os.environ, ON):
            for h in ({k: v for k, v in GOOD.items() if k != "Content-Type"}, {**GOOD, "Sec-Fetch-Site": "same-site"}):
                assert self.post(b, "drain", {"enabled": True}, h)[0] == 403, h
            for dup in ("X-Auth-Groups", "X-Auth-Username", "X-Broker-Proxy-Secret"):
                m = email.message.Message()
                for k, v in GOOD.items():
                    if k == dup:
                        m[k] = "ops" if k == "X-Auth-Groups" else v    # a client copy the proxy appended to
                    m[k] = v
                assert actions.check(m)[2] == "duplicate-header", dup
            m = email.message.Message()
            for k, v in GOOD.items():
                m[k] = v
            assert actions.check(m)[0] is True
        with patch.dict(os.environ, {**ON, "BROKER_ACTIONS_PROXY_SECRET": "short"}):
            assert self.post(b, "drain", {"enabled": True}, {**GOOD, "X-Broker-Proxy-Secret": "short"})[0] == 403
        assert not b.drain_status()["enabled"]

    def test_refusals_reach_the_sink_rate_limited_and_throws_are_audited(self):
        b = self.broker()
        actions._last_reject[0] = float("-inf")
        with patch.dict(os.environ, ON), patch.object(actions.events, "emit") as emit:
            bad = {**GOOD, "X-Auth-Groups": "users"}
            assert self.post(b, "drain", {"enabled": True}, bad)[0] == 403
            assert self.post(b, "drain", {"enabled": True}, bad)[0] == 403
            assert [c.kwargs["result"] for c in emit.call_args_list] == ["rejected-identity"]   # 2nd one held
            emit.reset_mock()
            with patch.object(b, "set_drain", side_effect=app.StateError("no journal")):
                assert self.post(b, "drain", {"enabled": True})[0] == 503     # do_POST still answers it
            assert emit.call_args_list[-1].kwargs["result"] == "error-StateError"

    def test_web_rerun_credits_the_web_user(self):
        b = self.broker()
        b.set_drain(True)
        with patch.dict(os.environ, ON):
            assert self.post(b, "rerun", {"task_id": 9610, "note": "resolution"})[0] == 202
        b.set_drain(False)
        b.tick()
        text = b.adapter._pop_comment_respawn("vikunja:task:9610")
        assert "resolution" in text and "web:alice" in text, text

    def test_web_rerun_picks_the_model_for_one_run(self):
        b = self.broker()
        with patch.dict(os.environ, ON):
            assert self.post(b, "rerun", {"task_id": 9610, "model": "gpt-nope"})[0] == 400
            assert self.post(b, "rerun", {"task_id": 9610, "model": ""})[0] == 400
            b.set_drain(True)
            assert self.post(b, "rerun", {"task_id": 9610, "model": " opus "})[0] == 202
            assert b.state.head()[2]["model"] == "opus"
            b.set_drain(False)
            b.tick()
        assert b.adapter._pop_model_choice("vikunja:task:9610") == "opus"
        assert b.adapter._pop_model_choice("vikunja:task:9610") is None          # one run only

    def test_web_rerun_model_refused_on_a_pinned_row(self):
        b = self.broker()
        with patch.dict(os.environ, ON), patch("routing.row_pinned", return_value=True):
            code, out = self.post(b, "rerun", {"task_id": 9611, "model": "sonnet"})
        assert code == 409 and "pins" in out["error"], out
        assert b.adapter._pop_model_choice("vikunja:task:9611") is None

    def settings_file(self):
        path = os.path.join(self.tmp.name, "routing-overrides.json")
        self.patchers.append(patch.object(RO.OVERRIDES, "path", path))
        self.patchers[-1].start()
        RO.OVERRIDES.reload(force=True)
        return path

    def test_settings_edit_validates_writes_and_audits(self):
        b = self.broker()
        path = self.settings_file()
        with patch.dict(os.environ, ON), patch.object(actions.events, "emit") as emit:
            assert self.handler(b, "/actions/settings", headers={**GOOD, "X-Auth-Groups": "users"}).do_GET()[0] == 403
            code, view = self.handler(b, "/actions/settings", headers=GOOD).do_GET()
            assert code == 200 and view["writable"] and "opus" in view["models"], view
            assert any(r["row"] == "code" for r in view["rows"]) and any(g["group"] == "code" for g in view["groups"])
            code, out = self.post(b, "settings", {"rows": {"code": {"model_pin": "opus"}}, "row_concurrency": {"code": 1}})
            assert code == 200 and out["ok"], out
            with open(path) as f:
                assert json.load(f) == {"rows": {"code": {"model_pin": "opus"}}, "row_concurrency": {"code": 1}}
            assert C.row_concurrency()["code"] == 1
            assert RO.OVERRIDES.apply(RO.templates()["code"]).model_pin == "opus"
            row = next(r for r in out["settings"]["rows"] if r["row"] == "code")
            assert row["model_pin"] == "opus" and row["overridden"] == ["model_pin"]
            assert emit.call_args_list[-1].kwargs["change"] == "code.model_pin:opus,cap.code:1"
            for bad in ({"rows": {"code": {"adapter": "codex"}}}, {"rows": {"nope": {"backend": ""}}},
                        {"rows": {"code": {"model_pin": "gpt9"}}}, {"rows": {"code": {"backend": "gpt9"}}},
                        {"rows": {"code": {"model_pin": 3}}}, {"row_concurrency": {"code": -1}},
                        {"row_concurrency": {"code": True}}, {"row_concurrency": {"Code": 1}}, {}, {"rows": []}):
                assert self.post(b, "settings", bad)[0] == 400, bad
            with open(path) as f:
                assert json.load(f)["rows"] == {"code": {"model_pin": "opus"}}, "a refused edit writes nothing"
            assert self.post(b, "settings", {"rows": {"code": {"model_pin": None}}, "row_concurrency": {"code": None}})[0] == 200
            with open(path) as f:
                assert json.load(f) == {}, "null restores the default"
            assert RO.OVERRIDES.apply(RO.templates()["code"]).model_pin == ""
            with open(path, "w") as f:
                f.write("{not json")
            code, out = self.post(b, "settings", {"row_concurrency": {"code": 2}})
            assert code == 400 and "by hand" in out["error"], out

    def test_settings_audit_names_the_effective_change(self):
        b = self.broker()
        path = self.settings_file()
        with patch.dict(os.environ, ON), patch.object(actions.events, "emit") as emit:
            masked = {f"a{i:02}": {"backend": None} for i in range(30)}
            masked["code"] = {"model_pin": "opus"}
            code, out = self.post(b, "settings", {"rows": masked})
            assert code == 400 and "unknown row" in out["error"], out
            assert not os.path.exists(path), "nothing written"
            other = next(r for r in RO.templates() if r != "code")
            code, out = self.post(b, "settings", {"rows": {other: {"backend": None}, "code": {"model_pin": "OPUS"}}})
            assert code == 200 and out["changes"] == ["code.model_pin:opus"], out
            assert emit.call_args_list[-1].kwargs["change"] == "code.model_pin:opus", "no-op nulls never reach the audit"
            assert self.post(b, "settings", {"rows": {"code": {"model_pin": "opus"}}})[1]["changes"] == []
            assert emit.call_args_list[-1].kwargs["change"] == "no-change"
            with patch.object(RO, "CHANGE_AUDIT_MAX", 20):
                code, out = self.post(b, "settings", {"rows": {"code": {"model_pin": "sonnet"}}, "row_concurrency": {"code": 1}})
            assert code == 400 and "fewer" in out["error"], out

    def test_settings_refuses_values_the_pin_path_cannot_run(self):
        b = self.broker()
        path = self.settings_file()
        with patch.dict(os.environ, ON):
            for bad in ("local", "claude", "claude-a b", "claude-", " ", "Claude-Opus"):
                assert self.post(b, "settings", {"rows": {"code": {"model_pin": bad}}})[0] == 400, bad
            code, out = self.post(b, "settings", {"rows": {"code": {"backend": ""}}})
            assert code == 400 and "null" in out["error"], out
            assert not os.path.exists(path)
            assert self.post(b, "settings", {"rows": {"code": {"model_pin": "claude-opus-5-5"}}})[0] == 200

    def test_settings_file_guards(self):
        b = self.broker()
        path = self.settings_file()
        real = os.path.join(self.tmp.name, "managed.json")
        with open(real, "w") as f:
            f.write("{}")
        os.symlink(real, path)
        RO.OVERRIDES.reload(force=True)
        with patch.dict(os.environ, ON):
            assert self.post(b, "settings", {"row_concurrency": {"code": 2}})[0] == 200
            assert os.path.islink(path), "a symlinked file stays a symlink"
            with open(real) as f:
                assert json.load(f) == {"row_concurrency": {"code": 2}}, "the edit landed in the real target"
            os.chmod(real, 0o444)
            try:
                if not os.access(real, os.W_OK):     # root ignores the mode bits
                    assert self.post(b, "settings", {"row_concurrency": {"code": 3}})[0] == 409
            finally:
                os.chmod(real, 0o644)
            orig = RO.os.fdopen
            def hand_edit(*a, **kw):                  # a hand edit lands while the dashboard edit is in flight
                with open(real, "w") as f:
                    f.write('{"row_concurrency": {"code": 7}}\n')
                return orig(*a, **kw)
            with patch.object(RO.os, "fdopen", side_effect=hand_edit):
                assert self.post(b, "settings", {"row_concurrency": {"code": 4}})[0] == 409
            with open(real) as f:
                assert json.load(f) == {"row_concurrency": {"code": 7}}, "the hand edit survives"
            import email.message
            m = email.message.Message()
            for k, v in GOOD.items():
                if k == "X-Auth-Groups":
                    m[k] = "ops"                      # a client copy the proxy appended to
                m[k] = v
            for path in ("/actions/settings", "/actions"):
                h = self.handler(b, path)
                h.headers = m                         # keep get_all, which the dict harness drops
                code, out = h.do_GET()
                assert code == 403 or out == {"enabled": True, "allowed": False}, (path, code, out)

    def test_settings_unwritable_file_is_409(self):
        b = self.broker()
        self.settings_file()
        with patch.dict(os.environ, ON), patch.object(RO.os, "replace", side_effect=PermissionError):
            code, out = self.post(b, "settings", {"row_concurrency": {"code": 2}})
        assert code == 409 and "cannot write" in out["error"], out
        assert [n for n in os.listdir(self.tmp.name) if n.startswith(".routing-overrides.")] == [], "temp file cleaned"

    def test_message_to_a_closing_worker_is_409(self):
        b = self.broker()
        key = self.live_worker(b)
        b.coalescer.live[key]["state"] = "closing"
        with patch.dict(os.environ, ON):
            assert self.post(b, "message", {"subject_key": key, "text": "hi"})[0] == 409
            assert self.post(b, "cancel", {"subject_key": key})[0] == 409

    def test_dash_and_json_replies_refuse_framing(self):
        import io
        sent = []
        h = object.__new__(app.Handler)
        h.path, h.headers, h.wfile = "/dash.html", {}, io.BytesIO()
        h.send_response = lambda code: sent.append(("status", code))
        h.send_header = lambda k, v: sent.append((k, v))
        h.end_headers = lambda: None
        h.do_GET()
        h._reply(200, {"ok": True})
        for pair in (("X-Frame-Options", "DENY"), ("Content-Security-Policy", "frame-ancestors 'none'")):
            assert sent.count(pair) == 2, sent


if __name__ == "__main__":
    failures = []
    names = sorted(n for n in ActionTests.__dict__ if n.startswith("test_"))
    for name in names:
        fixture = ActionTests()
        fixture.setUp()
        try:
            getattr(fixture, name)()
            print("PASS " + name)
        except BaseException:
            failures.append(name)
            import traceback
            traceback.print_exc()
            print("FAIL " + name)
        finally:
            fixture.tearDown()
    print(f"{len(names) - len(failures)} passed, {len(failures)} failed")
    sys.exit(1 if failures else 0)
