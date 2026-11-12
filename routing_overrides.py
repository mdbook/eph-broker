"""Atomic, last-good routing overrides; environment-backed Route templates remain defaults."""
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import replace

log = logging.getLogger("eph-broker.routing-overrides")


def templates():
    """{row_key: Route template} for every built-in and routes.json row: the rows overrides may name."""
    import routing
    rows = {r.row_key: r for r in vars(routing).values() if isinstance(r, routing.Route)}
    rows.update({r.row_key: r for _, _, _, r in routing.Router().rows})
    return rows


def backend_ok(name):
    """True when `name` is a usable spawn-registry backend. Registry metadata only: never opens a backend's
    credential file, so it is safe on a reload or a request thread."""
    from adapters.cc import _backend_entry
    entry = _backend_entry(name)
    return isinstance(entry, dict) and bool(
        isinstance(entry.get("codex"), dict) and entry["codex"].get("model") or
        entry.get("kind") == "local" and entry.get("env") or
        entry.get("kind") == "cloud" and entry.get("model") and entry.get("base_url"))


def model_choices():
    """Names an operator may pick: the native aliases, then every usable spawn-registry backend (names only)."""
    import routing
    from adapters import cc
    try:
        with open(cc.SPAWN_MODELS_JSON) as f:
            names = sorted(n for n in json.load(f).get("backends", {}) if backend_ok(n))
    except (OSError, ValueError, AttributeError):
        names = []
    return sorted(routing.NATIVE_MODEL_ALIASES) + names


# The dashboard may change these and nothing else. adapter and repo_lock_scope change what a row's worker can
# do or how it locks; trusted_creators widens who may pick a non-Anthropic backend. Those stay file-only.
EDITABLE_ROW_FIELDS = ("backend", "model_pin", "pin")
CHANGE_AUDIT_MAX = 240                 # actions.audit's target width; a bigger change list is refused, not truncated
_CLAUDE_ID = re.compile(r"claude-[a-z0-9][a-z0-9.\-\[\]]*")


class RoutingOverrides:
    def __init__(self, path=None):
        self.path = path or os.getenv("ROUTING_OVERRIDES_FILE", os.path.join(
            os.getenv("CONFIG_DIR", os.path.join(os.path.dirname(__file__), "config")),
            "routing-overrides.json"))
        self._lock = threading.Lock()
        self._edit_lock = threading.Lock()      # one dashboard edit at a time (read, merge, write, reload)
        self._stamp = None
        self._bad_stamp = None
        self._force = False
        self._data = {}

    @staticmethod
    def _validate(data):
        if not isinstance(data, dict) or set(data) - {"rows", "trusted_creators", "row_concurrency"}:
            raise ValueError("expected rows, trusted_creators and/or row_concurrency")
        rows = data.get("rows", {})
        if not isinstance(rows, dict):
            raise ValueError("rows must be an object")
        rows_by_key = templates()
        for name, fields in rows.items():
            if name not in rows_by_key:
                raise ValueError("unknown row")
            if (not isinstance(fields, dict) or set(fields) - {"backend", "adapter", "model_pin", "repo_lock_scope", "pin"}
                    or any(type(v) is not bool if k == "pin" else not isinstance(v, str)
                           for k, v in fields.items())):
                raise ValueError("row overrides accept backend, adapter, model_pin, repo_lock_scope strings and pin boolean")
            route = replace(rows_by_key[name], **fields)  # Route validates the harness.
            if route.backend and route.backend != "claude" and not backend_ok(route.backend):
                raise ValueError("unresolvable backend")
        creators = data.get("trusted_creators", [])
        if not isinstance(creators, list) or any(not isinstance(c, str) for c in creators):
            raise ValueError("trusted_creators must be a list of strings")
        caps = data.get("row_concurrency", {})
        if not isinstance(caps, dict) or any(
                not k or k != k.strip().lower() or type(v) is not int or v < 0
                for k, v in caps.items()):
            raise ValueError("row_concurrency must map lowercase groups to nonnegative integers")
        return data

    def reload(self, force=False, blocking=True):
        # A signal must not wait for a worker holding this lock; defer to the next spawn instead.
        if not self._lock.acquire(blocking=blocking):
            self._force = True
            return self._data
        alert = False
        try:
            try:
                st = os.stat(self.path)
                stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
            except FileNotFoundError:
                stamp = ("missing",)
            except OSError as exc:
                stamp = ("stat-error", exc.errno)
            if stamp == self._stamp and not (force or self._force):
                return self._data
            self._force = False
            self._stamp = stamp
            try:
                if stamp == ("missing",):
                    candidate = {}
                else:
                    with open(self.path) as f:
                        candidate = self._validate(json.load(f))
            except (OSError, ValueError, TypeError):
                # Do not echo file contents or parser errors into logs/alerts.
                log.error("routing overrides rejected; keeping last good config")
                alert = self._bad_stamp != stamp
                self._bad_stamp = stamp
            else:
                changed = sorted(k for k in set(candidate) | set(self._data)
                                 if candidate.get(k) != self._data.get(k))
                row_changes = sorted(k for k in set(candidate.get("rows", {})) | set(self._data.get("rows", {}))
                                     if candidate.get("rows", {}).get(k) != self._data.get("rows", {}).get(k))
                if changed:
                    log.info("routing overrides reloaded: keys=%s rows=%s", ",".join(changed), ",".join(row_changes))
                self._data = candidate
                self._bad_stamp = None
            return self._data
        finally:
            self._lock.release()
            if alert:
                import notify
                threading.Thread(target=notify.wake_ops, args=(
                    "Routing overrides rejected; keeping last good config. Check the routing overrides file.",),
                    daemon=True).start()

    def _read_file(self, path=None):
        """(the file's current JSON validated or {} when missing, its stat or None). ValueError when it is
        unreadable or invalid."""
        path = path or self.path
        try:
            with open(path) as f:
                return self._validate(json.load(f)), os.fstat(f.fileno())
        except FileNotFoundError:
            return {}, None
        except (OSError, ValueError, TypeError):
            raise ValueError("the overrides file is unreadable or invalid; fix it by hand first") from None

    @staticmethod
    def _pin_name(value):
        """The model_pin as with_model will read it, or ValueError. Exactly what the pin path interprets: a native
        alias (lowercased), a usable spawn-registry backend, or a full claude-* model id. `local`/`claude` are
        label shorthands the pin path does not map, so they are refused rather than passed to --model."""
        import routing
        if value.lower() in routing.NATIVE_MODEL_ALIASES:
            return value.lower()
        if backend_ok(value) or _CLAUDE_ID.fullmatch(value):
            return value
        raise ValueError("model_pin must be opus, sonnet, haiku, a claude-* model id or a spawn-registry backend")

    @staticmethod
    def _changes(before, after):
        """The effective change between two override dicts, as audit parts: code.model_pin:opus, cap.code:1,
        code.backend:default (removed). Only fields the dashboard may edit; no-ops never appear."""
        parts = []
        b_rows, a_rows = before.get("rows", {}), after.get("rows", {})
        for row in sorted(set(b_rows) | set(a_rows)):
            for field in EDITABLE_ROW_FIELDS:
                old, new = b_rows.get(row, {}).get(field), a_rows.get(row, {}).get(field)
                if old != new:
                    parts.append(f"{row}.{field}:{'default' if new is None else new}")
        b_caps, a_caps = before.get("row_concurrency", {}), after.get("row_concurrency", {})
        for group in sorted(set(b_caps) | set(a_caps)):
            if b_caps.get(group) != a_caps.get(group):
                parts.append(f"cap.{group}:{'default' if a_caps.get(group) is None else a_caps[group]}")
        return parts

    def edit(self, rows=None, row_concurrency=None):
        """Merge a dashboard edit into the file and publish it. `rows` = {row_key: {field: str/bool or None}} for
        EDITABLE_ROW_FIELDS; `row_concurrency` = {group: int or None}. None removes the entry (back to the
        environment default). The whole result goes through _validate, is written to a temp file next to the
        real target (a symlink is followed, never replaced) and renamed over it, then reloaded. Returns the
        effective change list (see _changes; [] = nothing changed, nothing written). ValueError = bad input
        (nothing written); OSError = could not write, or the file changed by hand during the edit."""
        with self._edit_lock:
            target = os.path.realpath(self.path)
            data, st = self._read_file(target)
            known = templates()
            new_rows = {k: dict(v) for k, v in data.get("rows", {}).items()}
            for name, fields in (rows or {}).items():
                if name not in known:
                    raise ValueError(f"unknown row {str(name)[:64]!r}")
                if not isinstance(fields, dict) or set(fields) - set(EDITABLE_ROW_FIELDS):
                    raise ValueError("only " + " and ".join(EDITABLE_ROW_FIELDS) + " are editable here")
                for field, value in fields.items():
                    if value is None:
                        new_rows.get(name, {}).pop(field, None)
                        continue
                    if field == "pin":
                        if type(value) is not bool:
                            raise ValueError("pin must be a boolean or null")
                        new_rows.setdefault(name, {})[field] = value
                        continue
                    if not isinstance(value, str):
                        raise ValueError(f"{field} must be a string or null")
                    value = value.strip()
                    if not value:
                        raise ValueError(f"{field} must not be empty; send null to go back to the default")
                    if field == "model_pin":
                        value = self._pin_name(value)
                    new_rows.setdefault(name, {})[field] = value
            caps = dict(data.get("row_concurrency", {}))
            for group, cap in (row_concurrency or {}).items():
                if cap is None:
                    caps.pop(group, None)
                elif type(cap) is not int:
                    raise ValueError("a concurrency cap must be a nonnegative integer or null")
                else:
                    caps[group] = cap
            out = {k: v for k, v in data.items() if k not in ("rows", "row_concurrency")}
            if any(new_rows.values()):
                out["rows"] = {k: v for k, v in new_rows.items() if v}
            if caps:
                out["row_concurrency"] = caps
            self._validate(out)
            changes = self._changes(data, out)
            if not changes:
                return changes
            if len(",".join(changes)) > CHANGE_AUDIT_MAX:     # the audit line must name every change, never a cut one
                raise ValueError("too many changes in one save; save fewer at a time")
            if st is not None and not os.access(target, os.W_OK):
                raise OSError("the overrides file is read-only")
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".routing-overrides.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(out, f, indent=2, sort_keys=True)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, st.st_mode & 0o777 if st is not None else 0o644)
                # A hand edit between our read and now would be silently overwritten: refuse instead.
                # shortcut: stat-compare, not a lock; a hand edit inside this last window still loses.
                try:
                    now = os.stat(target)
                except FileNotFoundError:
                    now = None
                if (st is None) != (now is None) or st is not None and (
                        (st.st_mtime_ns, st.st_size, st.st_ino) != (now.st_mtime_ns, now.st_size, now.st_ino)):
                    raise OSError("the overrides file changed during the edit")
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            dfd = os.open(os.path.dirname(target), os.O_RDONLY)
            try:
                os.fsync(dfd)               # make the rename itself durable
            finally:
                os.close(dfd)
            if self.reload(force=True) != out:  # written; something newer replaced it already. Not a failed save.
                log.warning("routing overrides changed again right after a dashboard edit")
            return changes

    def view(self):
        """What the dashboard's Settings card shows: each row's harness, backend default and explicit pins
        (template default plus overrides), the per-group concurrency caps, the model names it may pick and
        whether an edit can be saved. Names and values only; never credential paths."""
        import coalescer
        data = self.reload()
        overridden = data.get("rows", {})
        rows = []
        for key, t in sorted(templates().items()):
            eff = self.apply(t)
            rows.append({"row": key, "adapter": eff.adapter, "backend": eff.backend, "model_pin": eff.model_pin,
                         "pin": eff.pin, "pinned": eff.pin or bool(eff.model_pin), "default_pin": t.pin,
                         "default_backend": t.backend, "default_model_pin": t.model_pin,
                         "overridden": sorted(f for f in overridden.get(key, {}) if f in EDITABLE_ROW_FIELDS)})
        groups = sorted({r["row"].split("/")[0].lower() for r in rows})
        caps, own = coalescer.row_concurrency(), data.get("row_concurrency", {})
        directory = os.path.dirname(os.path.abspath(self.path))
        return {"rows": rows,
                "groups": [{"group": g, "cap": caps.get(g), "overridden": g in own} for g in groups],
                "models": model_choices(),
                "writable": os.access(directory, os.W_OK) and (
                    not os.path.exists(self.path) or os.access(self.path, os.W_OK)),
                "rejected": self._bad_stamp is not None}

    def apply(self, route):
        fields = self.reload().get("rows", {}).get(route.row_key, {})
        return replace(route, **fields) if fields else route


OVERRIDES = RoutingOverrides()
