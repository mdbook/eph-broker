#!/usr/bin/env python3
"""routing_overrides.py self-tests: hot-reloadable routing overrides.

stdlib assert only, same runner shape as test_routes.py/test_notify.py. Fixtures only: no harness or
notification subprocess runs here.
"""
import json
import os
import pathlib
import sys
import tempfile
import threading
from dataclasses import replace
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["BROKER_SITE_ENV"] = os.path.join(HERE, "fixtures", "site.test.env")
os.environ["BROKER_CONFIG"] = os.path.join(HERE, "fixtures", "broker.test.json")
os.environ["BROKER_PROVENANCE_PATH"] = os.path.join(HERE, "fixtures", "provenance.test.json")
import site_env  # noqa: F401

import coalescer
import notify
import routing
import routing_overrides
from adapters import cc

PASS = []


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


def save(path, data):
    # Atomic replacement also exercises inode detection, without sleeps/mtime assumptions.
    next_path = path.with_suffix(".next")
    next_path.write_text(json.dumps(data))
    next_path.replace(path)


class _Overrides:
    """Fresh RoutingOverrides backed by a tmp file, patched module-wide for one test block (the same
    substitutions the old pytest `overrides` fixture made): restores routing_overrides.OVERRIDES,
    routing.resolve_model and cc._backend_entry on exit. Use as `with _Overrides() as (path, loader):`."""
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "routing.json"
        self.loader = routing_overrides.RoutingOverrides(str(self.path))
        self._old = (routing_overrides.OVERRIDES, routing.resolve_model, cc._backend_entry)
        routing_overrides.OVERRIDES = self.loader
        routing.resolve_model = lambda agent: "claude-test"
        cc._backend_entry = lambda name: {
            "kind": "cloud", "model": "test", "base_url": "https://example.com"
        } if name == "test-backend" else None
        return self.path, self.loader

    def __exit__(self, *a):
        routing_overrides.OVERRIDES, routing.resolve_model, cc._backend_entry = self._old
        self._tmp.cleanup()


class _Env:
    """Set env vars for one block, restore (or unset) after."""
    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kw}
        os.environ.update(self.kw)

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_shipped_example_is_valid():
    with _Overrides() as (_, loader):
        example = os.path.join(ROOT, "config", "routing-overrides.example.json")
        with open(example) as f:
            data = json.load(f)
        assert loader._validate(data) == data
    ok("the shipped config/routing-overrides.example.json passes RoutingOverrides._validate unchanged")


def test_next_spawn_and_existing_route():
    with _Overrides() as (path, loader):
        template = routing._CODE_ROW
        router = routing.Router(rows=[("test:", lambda e: True, lambda e: True, template)])
        before = router.route("test:one", {})
        save(path, {"rows": {template.row_key: {
            "adapter": "codex", "backend": "test-backend", "model_pin": "opus", "repo_lock_scope": "run"}}})
        after = router.route("test:two", {})
        assert (after.adapter, after.backend, after.model) == ("codex", "test-backend", "opus")
        assert before.adapter == template.adapter
        assert before.backend == template.backend
        assert before.model == "claude-test"
        assert before.repo_lock_scope == "phases" and after.repo_lock_scope == "run"
        assert template.adapter != "codex"
    ok("a routing-overrides.json write lands on the NEXT spawn only; an already-routed/building row is untouched")


def test_backend_defaults_labels_and_explicit_pins():
    with _Overrides() as (path, loader):
        entries = {
            "sol": {"kind": "codex", "codex": {"model": "test-codex"}},
            "cloud": {"kind": "cloud", "model": "test", "base_url": "https://llm.example.com"},
            "hermes-cloud": {"kind": "cloud", "model": "test", "base_url": "https://llm.example.com",
                             "adapter": "hermes"},
        }
        with patch.object(cc, "_backend_entry", entries.get):
            # Migration: the existing backend+adapter shape loads as a default, without rewriting the file.
            data = {"rows": {"code": {"backend": "sol", "adapter": "codex"}}, "trusted_creators": ["operator"]}
            save(path, data)
            defaulted = routing._CODE_ROW.with_model()
            assert (defaulted.backend, defaulted.adapter, defaulted.backend_pinned) == ("sol", "codex", False)
            assert not routing.row_pinned("code") and loader.reload() == data
            view = next(r for r in loader.view()["rows"] if r["row"] == "code")
            assert not view["pin"] and not view["pinned"]
            for alias in ("opus", "sonnet", "haiku"):
                for label in (alias, "model:" + alias):
                    picked = routing.apply_model_label(defaulted, [label], "guest", routing_overrides.backend_ok)
                    assert (picked.backend, picked.model, picked.adapter) == ("claude", alias, "cc")
            for name, adapter in (("sol", "codex"), ("cloud", "cc"), ("hermes-cloud", "hermes")):
                picked = routing.apply_model_label(defaulted, ["model:" + name], "Operator", routing_overrides.backend_ok)
                assert (picked.backend, picked.adapter) == (name, adapter)
                try:
                    routing.apply_model_label(defaulted, ["model:" + name], "guest", routing_overrides.backend_ok)
                    raise AssertionError("an untrusted creator must not pick a registry backend")
                except ValueError as e:
                    assert "not in MODEL_LABEL_TRUSTED_CREATORS" in str(e)
            assert routing.apply_model_label(defaulted, ["deepseek"], "guest", lambda n: True) is defaulted
            assert (defaulted.backend, defaulted.adapter) == ("sol", "codex"), "choices never mutate the default"
            # An env-backed template has exactly the same semantics; explicit pin opts back into the old policy.
            template = replace(routing._GENERAL_ROW, backend="sol", adapter="codex")
            assert not template.with_model().backend_pinned
            with _Env(GENERAL_PIN="true"):
                template = replace(template, pin=routing._row_pin("GENERAL_PIN"))
            assert template.with_model().backend_pinned
            assert loader.edit(rows={"code": {"pin": True}}) == ["code.pin:True"]
            pinned = routing._CODE_ROW.with_model()
            assert pinned.backend_pinned and routing.row_pinned("code")
            assert routing.apply_model_label(pinned, ["opus"], "guest", lambda n: False) is pinned
            assert routing.apply_model_label(pinned, ["model:missing"], "guest", lambda n: False) is pinned
            view = next(r for r in loader.view()["rows"] if r["row"] == "code")
            assert view["pin"] and view["pinned"] and not view["default_pin"]
            loader.edit(rows={"code": {"pin": False}})
            assert not routing.row_pinned("code")
            loader.edit(rows={"code": {"pin": None, "model_pin": "opus"}})
            assert routing.row_pinned("code"), "model_pin remains a hard pin even without pin:true"
    ok("backend/adapter are defaults; native and trusted registry labels select their harness; only pin/model_pin block labels")


def test_model_choices_preserve_site_capability_rules():
    with _Overrides() as (path, _):
        save(path, {"trusted_creators": ["operator"]})
        entries = {name: {"kind": "cloud", "model": "test", "adapter": adapter,
                          "base_url": "https://llm.example.com"}
                   for name, adapter in (("hermes-cloud", "hermes"), ("codex-cloud", "codex"))}
        with patch.object(cc, "_backend_entry", entries.get):
            route = replace(routing._CODE_ROW.with_model(), backend="cloud", adapter="cc",
                            site_capability="docs-writer")
            for name, entry in entries.items():
                for choose in (lambda r: routing.apply_model_label(r, ["model:" + name], "operator",
                                                                  routing_overrides.backend_ok),
                               lambda r: routing.apply_model_choice(r, name, True, routing_overrides.backend_ok),
                               lambda r: routing.apply_adapter_label(r, [entry["adapter"]], "operator")):
                    before = dict(route.__dict__)
                    try:
                        choose(route)
                        raise AssertionError("accepted a non-cc harness on a site capability")
                    except ValueError as e:
                        assert "site capability 'docs-writer' runs on the cc harness only" in str(e), e
                    assert route.__dict__ == before, "refusal must not change the adapter/backend or other fields"
                    unrestricted = replace(route, site_capability="")
                    picked = choose(unrestricted)
                    assert picked.adapter == entry["adapter"]
            native = routing.apply_model_choice(route, "opus", True, routing_overrides.backend_ok)
            assert (native.adapter, native.backend, native.model) == ("cc", "claude", "opus")
    ok("model labels, dashboard choices and harness labels refuse non-cc site capabilities; "
       "the same choices without a site capability still switch")


def test_bad_file_is_atomic_and_alerted_once():
    bad_cases = (
        {"rows": {"code": {"repo_lock_scope": "publish"}}},
        {"rows": {"wiki": {"repo_lock_scope": ""}}},
        {"rows": {"code": {"adapter": "typo"}}},
        {"rows": {"code": {"pin": "true"}}},
        {"rows": {"code": {"pin": 1}}},
        {"rows": {"code": {"backend": "missing"}}},
        {"rows": {"unknown-row": {"adapter": "cc"}}},
        {"rows": {"code": {"capability": "general"}}},
        {"trusted_creators": "operator"},
        {"row_concurrency": {"code": True}},
        {"row_concurrency": {"code": -1}},
        [],
        "invalid json",
    )
    for bad in bad_cases:
        with _Overrides() as (path, loader):
            alerts = []
            sent = threading.Event()

            def alert(text):
                alerts.append(text)
                sent.set()
            old_wake_ops = notify.wake_ops
            notify.wake_ops = alert
            try:
                good = {"rows": {routing._CODE_ROW.row_key: {"model_pin": "opus"}},
                        "trusted_creators": ["operator"], "row_concurrency": {"code": 1}}
                save(path, good)
                assert loader.reload() == good
                save(path, bad)
                assert loader.reload() == good
                assert sent.wait(2), bad
                assert loader.reload(force=True) == good
                assert len(alerts) == 1, (bad, alerts)
                assert routing.model_label_trusted_creators() == {"operator"}
                assert coalescer.row_concurrency()["code"] == 1
            finally:
                notify.wake_ops = old_wake_ops
    ok("a malformed routing-overrides.json keeps the last-good config, atomically, and alerts ops exactly once")


def test_env_defaults_and_removal():
    with _Overrides() as (path, loader):
        with _Env(MODEL_LABEL_TRUSTED_CREATORS="EnvUser", ROW_CONCURRENCY="code=3,general=2"):
            assert loader.reload() == {}
            assert routing.model_label_trusted_creators() == {"envuser"}
            assert coalescer.row_concurrency() == {"code": 3, "general": 2}
            template = routing._CODE_ROW
            assert template.with_model().adapter == template.adapter
            save(path, {"trusted_creators": [], "row_concurrency": {"code": 0}})
            assert routing.model_label_trusted_creators() == set()
            assert coalescer.row_concurrency() == {"code": 0, "general": 2}
            path.unlink()
            assert routing.model_label_trusted_creators() == {"envuser"}
            assert coalescer.row_concurrency()["code"] == 3
    ok("with no overrides file, MODEL_LABEL_TRUSTED_CREATORS/ROW_CONCURRENCY env defaults apply; a removed "
       "override file falls back to them row by row")


def test_hup_forces_reload_and_defers_when_busy():
    with _Overrides() as (path, loader):
        save(path, {"trusted_creators": ["first"]})
        loader.reload()
        st = path.stat()
        path.write_text(json.dumps({"trusted_creators": ["other"]}))
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert loader.reload()["trusted_creators"] == ["first"]
        assert loader.reload(force=True, blocking=False)["trusted_creators"] == ["other"]
        held = threading.Event()
        release = threading.Event()

        def hold():
            with loader._lock:
                held.set()
                release.wait(2)
        thread = threading.Thread(target=hold)
        thread.start()
        assert held.wait(2)
        try:
            assert loader.reload(force=True, blocking=False)["trusted_creators"] == ["other"]
            assert loader._force
        finally:
            release.set()
            thread.join()
        loader.reload()
        assert not loader._force
    ok("SIGHUP (force=True) reloads even with an unchanged mtime/size/inode stamp; a reload under an active "
       "lock defers instead of blocking a spawn, and is picked up on the next unlocked reload")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(tests)} routing-overrides self-tests...")
    FAILURES = []
    for t in tests:
        try:
            t()
        except BaseException as e:   # noqa: BLE001 (keep going past one failing test, like test_broker.py)
            FAILURES.append((t.__name__, e))
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    if FAILURES:
        print(f"\n{len(FAILURES)} of {len(tests)} CHECKS FAILED ({len(PASS)} passed):")
        for name, e in FAILURES:
            print(f"  - {name}: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"\nALL {len(PASS)} CHECKS PASSED")
