#!/usr/bin/env python3
"""StubAdapter: records calls and spawns nothing. Lets the coalescer seam be exercised in
tests and `--selftest` without launching a real `claude` subprocess.

Like the real adapter, spawn() returns immediately and records; it does no blocking work, so the
coalescer's under-lock call contract holds in tests as it does in production.
"""
import threading, time
from .base import Adapter


class StubAdapter(Adapter):
    def __init__(self):
        self.spawns = []      # list of (key, brief)
        self.forwards = []    # list of (key, evt)
        self.teardowns = []   # list of key
        self._alive = {}      # key -> bool
        self._lock = threading.Lock()

    def spawn(self, key, brief):
        # No blocking work: record, mark alive and return immediately.
        with self._lock:
            self.spawns.append((key, dict(brief)))
            self._alive[key] = True

    def forward(self, key, evt):
        with self._lock:
            self.forwards.append((key, dict(evt)))

    def liveness(self, key):
        with self._lock:
            return self._alive.get(key, False)

    def teardown(self, key):
        with self._lock:
            self.teardowns.append(key)
            self._alive[key] = False
