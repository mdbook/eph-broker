"""Private, synchronous journal for drain admission and scheduler checkpoints.

SQLite transactions cover queue -> pending handoff. A committed dispatch intent
precedes external side effects; interrupted intents are held, never replayed.
Only the broker process may open this state file (one service instance per path).
"""
import contextlib
import fcntl
import json
import os
import sqlite3
import threading
import time


class StateError(RuntimeError):
    """Durable acceptance could not be proved; callers must fail closed."""


_SCHEMA = (
    "CREATE TABLE state (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE intake (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "kind TEXT NOT NULL, payload TEXT NOT NULL, phase TEXT NOT NULL, arrived REAL NOT NULL)",
)


class BrokerState:
    def __init__(self, path, drain_on_create=False):
        self.lock = threading.RLock()
        self.error = None
        self.db = None
        self.owner_fd = None
        if type(drain_on_create) is not bool:
            raise StateError("initial drain must be a boolean")
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
            self.owner_fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self.owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
            self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self.db.execute("PRAGMA synchronous=FULL")
            # Classify before any DDL, in the SAME transaction as initialization.
            # A crash must never commit a new schema without its requested flag.
            self.db.execute("BEGIN IMMEDIATE")
            existing = self._schema(self.db)
            if not existing:
                for statement in _SCHEMA:
                    self.db.execute(statement)
                self.db.execute("INSERT INTO state VALUES (?,?)", ("draining", json.dumps(drain_on_create)))
            else:
                self._validate_schema(existing)
            self.get("draining", False)  # validate persisted JSON type; absent is legacy false
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                self._bad_database()
            self.db.execute("COMMIT")
        except BaseException as exc:
            # Includes interruption and uncertain commit: never return a usable store.
            if self.db is not None:
                try:
                    if self.db.in_transaction:
                        self.db.execute("ROLLBACK")
                except (OSError, sqlite3.Error):
                    pass
                try:
                    self.db.close()
                except (OSError, sqlite3.Error):
                    pass
            if self.owner_fd is not None:
                os.close(self.owner_fd)
            if isinstance(exc, (OSError, sqlite3.Error)):
                raise StateError("broker journal could not be opened") from exc
            raise

    @staticmethod
    def _schema(db):
        """Full known DDL plus column/index/FK layout, including generated objects."""
        objects = {}
        for kind, name, table, sql in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master"):
            layout = foreign = ()
            if kind == "table":
                layout = tuple(db.execute("SELECT * FROM pragma_table_xinfo(?)", (name,)))
                foreign = tuple(db.execute("SELECT * FROM pragma_foreign_key_list(?)", (name,)))
            elif kind == "index":
                layout = tuple(db.execute("SELECT * FROM pragma_index_xinfo(?)", (name,)))
            objects[(kind, name)] = (kind, table, "".join(sql.split()).lower() if sql else None, layout, foreign)
        return objects

    @classmethod
    def _validate_schema(cls, actual):
        # The reference uses this SQLite build's own automatic index/sequence DDL.
        # ANALYZE tables are optional, not an excuse to allow arbitrary sqlite_*.
        reference = sqlite3.connect(":memory:")
        try:
            for statement in _SCHEMA:
                reference.execute(statement)
            required = cls._schema(reference)
            reference.execute("ANALYZE")
            allowed = cls._schema(reference)
            if not required.keys() <= actual.keys() <= allowed.keys() or any(
                    definition != allowed[identity] for identity, definition in actual.items()):
                raise StateError("broker journal schema is incompatible")
        finally:
            reference.close()

    def close(self):
        self.db.close()
        os.close(self.owner_fd)

    @staticmethod
    def _bad_database():
        raise StateError("broker journal integrity check failed")

    @contextlib.contextmanager
    def atomic(self):
        with self.lock:
            nested = self.db.in_transaction
            try:
                if not nested:
                    self.db.execute("BEGIN IMMEDIATE")
                yield
                if not nested:
                    self.db.execute("COMMIT")
            except (sqlite3.Error, OSError) as exc:
                if not nested and self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                self.error = "journal I/O failed"
                raise StateError(self.error) from exc
            except BaseException:
                if not nested and self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    def get(self, name, default=None):
        with self.atomic():
            row = self.db.execute("SELECT value FROM state WHERE name=?", (name,)).fetchone()
            try:
                value = json.loads(row[0]) if row else default
                if row and name == "draining" and type(value) is not bool:
                    raise ValueError("drain is not a JSON boolean")
                return value
            except (ValueError, TypeError) as exc:
                self.error = "invalid broker journal state"
                raise StateError("invalid broker journal state") from exc

    def put(self, name, value):
        encoded = self._encode(value)
        with self.atomic():
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (name, encoded))

    def enqueue(self, kind, payload):
        encoded = self._encode(payload)
        with self.atomic():
            cur = self.db.execute("INSERT INTO intake(kind,payload,phase,arrived) VALUES (?,?,?,?)",
                                  (kind, encoded, "queued", time.time()))
            return cur.lastrowid

    def _encode(self, value):
        try:
            return json.dumps(value, allow_nan=False)
        except (ValueError, TypeError) as exc:
            self.error = "journal serialization failed"
            raise StateError(self.error) from exc

    def head(self):
        with self.atomic():
            row = self.db.execute("SELECT id,kind,payload,phase FROM intake ORDER BY id LIMIT 1").fetchone()
            return (row[0], row[1], json.loads(row[2]), row[3]) if row else None

    def claim(self, item_id):
        with self.atomic():
            cur = self.db.execute("UPDATE intake SET phase='dispatching' WHERE id=? AND phase='queued'", (item_id,))
            if cur.rowcount != 1:
                raise StateError("intake is already dispatching; reconciliation required")

    def finish(self, item_id):
        with self.atomic():
            self.db.execute("DELETE FROM intake WHERE id=?", (item_id,))

    def queued(self, limit=50):
        """Oldest-first queued intake rows (id, kind, payload, arrived), at most `limit`. Read-only, for the dash."""
        with self.atomic():
            rows = self.db.execute("SELECT id,kind,payload,arrived FROM intake WHERE phase='queued' ORDER BY id LIMIT ?",
                                   (int(limit),)).fetchall()
        return [(r[0], r[1], json.loads(r[2]), r[3]) for r in rows]

    def status(self):
        with self.atomic():
            counts = dict(self.db.execute("SELECT phase,count(*) FROM intake GROUP BY phase"))
            return {"enabled": bool(self.get("draining", False)), "durable": True,
                    "queued": counts.get("queued", 0), "dispatching": counts.get("dispatching", 0),
                    "dispatching_ids": [row[0] for row in self.db.execute(
                        "SELECT id FROM intake WHERE phase='dispatching' ORDER BY id")],
                    "error": self.error}
