"""Dispatch ordering and non-preemptive repository/turn-slot admission."""
import os
import threading
import time


def reviews_first():
    policy = os.getenv("DISPATCH_PRIORITY", "reviews-first").strip().lower()
    if policy not in ("reviews-first", "fifo"):
        raise ValueError("DISPATCH_PRIORITY must be reviews-first or fifo")
    return policy == "reviews-first"


class PrioritySlots:
    """Bounded FIFO turn slots with one operator-selected next subject.

    A pending spawn can reserve the next slot while doing setup. Repository waits
    defer that reservation (never strand a slot waiter holding the needed repo).
    Promotion is one-shot, non-preemptive and process-local; the latest selection wins.
    Cancellation checks run OUTSIDE the condition: they take the scheduler lock.
    """
    def __init__(self, value):
        if value < 0:
            raise ValueError("semaphore initial value must be >= 0")
        self._condition = threading.Condition()
        self._value = self._limit = value
        self._waiters = []
        self._front = None
        self._reserved = False

    @property
    def front(self):
        with self._condition:
            return self._front

    def promote(self, key, *, pending=False, reserve=False):
        with self._condition:
            if not pending and not any(w[0] == key for w in self._waiters):
                return False  # already acquired/ended: never boost a future run by mistake
            self._front, self._reserved = key, reserve
            self._condition.notify_all()
            return True

    def defer(self, key):
        with self._condition:
            if self._front == key:
                self._reserved = False
                self._condition.notify_all()

    def discard(self, key):
        with self._condition:
            if self._front == key:
                self._front, self._reserved = None, False
                self._condition.notify_all()

    def acquire(self, blocking=True, timeout=-1, *, key=None, cancel=None):
        if not blocking and timeout != -1:
            raise ValueError("can't specify a timeout for a non-blocking call")
        deadline = time.monotonic() + timeout if timeout >= 0 else None
        waiter = (key, object())
        with self._condition:
            self._waiters.append(waiter)
        try:
            while True:
                if cancel is not None:
                    cancel()
                with self._condition:
                    preferred = next((w for w in self._waiters if w[0] == self._front), None) \
                        if self._front is not None else None
                    first = preferred or (None if self._reserved else self._waiters[0])
                    if self._value and first is waiter:
                        self._value -= 1
                        if key == self._front:
                            self._front, self._reserved = None, False
                        # Remove under the SAME lock as the grant: an operator cannot
                        # select this waiter after it already owns a slot.
                        self._waiters.remove(waiter)
                        self._condition.notify_all()
                        return True
                    if not blocking:
                        return False
                    remaining = deadline - time.monotonic() if deadline is not None else None
                    if remaining is not None and remaining <= 0:
                        return False
                    if cancel is not None:
                        remaining = min(remaining, 0.1) if remaining is not None else 0.1
                    self._condition.wait(remaining)
        finally:
            with self._condition:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                self._condition.notify_all()

    def release(self):
        with self._condition:
            if self._value == self._limit:
                raise ValueError("slots released too many times")
            self._value += 1
            self._condition.notify_all()


class RepoLock:
    """Non-preemptive lock: queued reviews/fixes precede queued new work, FIFO within a class.

    Each repo owns its own lock; priority never holds a different repo's checkout.
    The condition protects admission only, not the operation performed by the holder.
    """
    def __init__(self):
        self._condition = threading.Condition()
        self._held = False
        self._waiters = []

    def acquire(self, blocking=True, timeout=-1, *, followup=False, cancel=None, on_wait=None):
        """Keep one queue position until admission, timeout, or a raising cancel check.

        cancel runs before admission and at most 0.1s apart while waiting, so drain
        can park setup attempts without removing and re-registering their waiters.
        """
        if not blocking and timeout != -1:
            raise ValueError("can't specify a timeout for a non-blocking call")
        deadline = time.monotonic() + timeout if timeout >= 0 else None
        with self._condition:
            waiter = (bool(followup), object())
            self._waiters.append(waiter)
            try:
                while True:
                    if cancel is not None:
                        cancel()
                    first = next((w for w in self._waiters if w[0]), self._waiters[0]) \
                        if reviews_first() else self._waiters[0]
                    if not self._held and first is waiter:
                        self._held = True
                        return True
                    if not blocking:
                        return False
                    remaining = deadline - time.monotonic() if deadline is not None else None
                    if remaining is not None and remaining <= 0:
                        return False
                    if cancel is not None:
                        remaining = min(remaining, 0.1) if remaining is not None else 0.1
                    if on_wait is not None:
                        on_wait()
                    self._condition.wait(remaining)
            finally:
                self._waiters.remove(waiter)
                self._condition.notify_all()

    def release(self):
        with self._condition:
            if not self._held:
                raise RuntimeError("release unlocked repo lock")
            self._held = False
            self._condition.notify_all()

    def locked(self):
        with self._condition:
            return self._held
