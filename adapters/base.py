#!/usr/bin/env python3
"""Adapter ABC: the interface the coalescer's spawn seam and the liveness layer target.

Four primitives:
  spawn(key, brief)     -> start ONE ephemeral worker for `key` seeded with `brief`.
                           CONTRACT: MUST return IMMEDIATELY (dispatch the subprocess to a
                           thread/executor) because it is called UNDER coalescer.lock inside tick().
  forward(key, brief)   -> deliver the COLLAPSED buffered forwards into the live warm worker as ONE
                           --resume turn. NON-BLOCKING (enqueue and return); the LivenessDriver calls it at
                           a turn boundary and must not block the sweep.
  liveness(key) -> bool -> is the worker for `key` currently IN a turn? Used ONLY by the force-close
                           backstop, never for the forward-vs-drain decision (that is `state`, in the
                           coalescer, under the lock).
  teardown(key)         -> hard-stop and clean up the worker/worktree for `key`. Runs LOCK-FREE.

The liveness layer also uses three optional hooks (default-safe here so any Adapter works with LivenessDriver):
  turn_age(key)         -> seconds the current turn has run, or None (idle/gone), for force-close timing.
  worker_alive(key)     -> is the worker's loop thread alive? (detects open_running-but-dead).
  set_coalescer(c)      -> receive a back-reference so the worker can advertise turn transitions under the lock.
"""
import abc


class Adapter(abc.ABC):
    @abc.abstractmethod
    def spawn(self, key, brief):
        """Start a worker for `key`. Dispatch async and return immediately (called under lock)."""
        raise NotImplementedError

    @abc.abstractmethod
    def forward(self, key, brief):
        """Enqueue ONE collapsed --resume turn into the live warm worker (non-blocking)."""
        raise NotImplementedError

    @abc.abstractmethod
    def liveness(self, key):
        """-> bool: is the worker for `key` currently in an active turn? (force-close backstop only)."""
        raise NotImplementedError

    @abc.abstractmethod
    def teardown(self, key):
        """Hard-stop the worker for `key` and remove its worktree. Runs lock-free."""
        raise NotImplementedError

    # ---- optional hooks (default-safe) ----
    def turn_age(self, key):
        """Seconds the current turn has been running, or None. Used for force-close timing."""
        return None

    def worker_alive(self, key):
        """-> bool: is the worker's own loop thread still alive?"""
        return False

    def set_coalescer(self, coalescer):
        """Receive a back-ref to the coalescer so the worker can advertise turn transitions."""
        pass
