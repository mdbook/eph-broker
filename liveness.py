#!/usr/bin/env python3
"""liveness.py: broker-owned island liveness.

This code COUPLES the coalescer's advertised `state` handshake to the real CC worker turn lifecycle, and
force-closes wedged workers. It does NOT rewrite the lifted coalescer island
(on_event/tick/normalize/collapse/_append_raw); it drives it through the coalescer's lock-guarded
transition helpers.

Invariants honoured here:
  * `state` (in coalescer, under self.lock) stays the SOLE synchronization token for forward-vs-drain.
    Every transition goes through a coalescer helper that writes `state` under the lock. This module never
    writes live[key]["state"] directly and never uses pid-existence as a liveness/"is-the-operation-alive"
    check; that would reintroduce the dead-client != dead-operation race.
  * The open_running -> open_idle transition is DRIVEN by the worker's TURN-COMPLETION signal (the worker
    thread calls coalescer.mark_idle() at each turn boundary; a turn is "complete" when a result
    envelope has been parsed). This driver then, at that idle boundary, delivers buffered forwards or closes.
  * pid is used ONLY to (a) count the global concurrency cap (pid_alive, below) and (b) kill and reclaim a
    slot on force-close (adapter.teardown), NEVER for the per-subject forward-vs-drain decision.

Transition summary (per live key, evaluated each sweep):
  open_running -> force-close if the turn is silent past progress_timeout, or the worker thread died.
  open_idle    -> buffered forwards? deliver ONE collapsed --resume turn (take_forwards flips running).
               -> else window-quiet? begin_close (state=closing UNDER LOCK), then teardown LOCK-FREE,
                       then finish_close (closed + pop UNDER LOCK).
               -> else stay open_idle.
  closing/closed -> teardown in flight / popped: nothing (on_event routes racing events to pending).
"""
import os, threading, time, logging

log = logging.getLogger("eph-broker.liveness")

# The force-close backstop must sit ABOVE a turn's own wall-clock hard-kill (adapters/cc._one_turn) so the adapter kills the
# runaway subprocess first and the backstop only catches a genuinely silent/wedged worker. Headroom is the WHOLE
# point of the backstop, so it must scale with the per-capability wall-clock, not a stale absolute.
_LIVENESS_HEADROOM = 300.0   # seconds the backstop stays above the turn's own hard-kill


def resolve_progress_timeout(cfg_progress, wall_clock_s, wall_clock_by_capability=None, hard_kill_headroom=None):
    """The force-close progress_timeout_s: an explicit cfg value wins, but is FLOORED at the max turn hard-kill
    (wall_clock_by_capability values override wall_clock_s) + headroom. Without this floor, raising
    wall_clock_s (say 600 to 1800) silently leaves the backstop BELOW the hard-kill and it force-closes healthy
    long turns (a 'torn down mid-turn' SIGKILL, rc=-9, was seen in production). Pure."""
    headroom = _LIVENESS_HEADROOM if hard_kill_headroom is None else float(hard_kill_headroom)
    per_cap = [float(v) for v in (wall_clock_by_capability or {}).values()]
    max_hard_kill = max([float(wall_clock_s)] + per_cap)
    floor = max_hard_kill + headroom
    return max(float(cfg_progress), floor)


def pid_alive(pid):
    """Coarse liveness gauge for the CONCURRENCY CAP ONLY (never per-subject forward-vs-drain)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists but not ours; still counts toward the cap
    except OSError:
        return False


class LivenessDriver:
    """Broker-owned periodic sweep that couples the coalescer handshake to the worker turn-lifecycle and
    force-closes wedged workers. Call sweep() from a periodic loop (start()) or directly (tests)."""

    def __init__(self, coalescer, adapter, *, alert_ops=None, ticket_comment=None,
                 progress_timeout=900.0, period=1.0):
        self.coalescer = coalescer
        self.adapter = adapter
        self.alert_ops = alert_ops or (lambda msg: log.warning("ALERT-OPS: %s", msg))
        self.ticket_comment = ticket_comment or (lambda route, text: log.info("TICKET-COMMENT: %s", text))
        # progress_timeout is the BROKER backstop for a silent/wedged turn. It should sit ABOVE the adapter's
        # own per-turn wall-clock hard-kill (default 600s): the adapter kills a runaway subprocess; this
        # catches a worker that advertised running but made no progress / whose thread died.
        self.progress_timeout = progress_timeout
        self.period = period
        self._stop = threading.Event()
        self._thread = None
        self._dispatch = []   # dispatched close threads (tests join these)

    # ---- lifecycle ----
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="r3-liveness", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.wait(self.period):
            try:
                self.sweep()
            except Exception as e:                     # a sweep bug must never wedge the driver
                log.exception("liveness sweep error: %s", e)

    # ---- the sweep ----
    def sweep(self, now=None):
        from coalescer import collapse                 # local import: avoid a module import cycle
        now = now if now is not None else time.time()
        # Reads of live[key]["state"] here are advisory; every COMMIT goes through a lock-guarded coalescer
        # helper that re-checks the phase, so `state` stays the sole (atomic) sync token.
        for key in list(self.coalescer.live):
            w = self.coalescer.live.get(key)
            if not w:
                continue
            state = w["state"]

            if state == "open_running":
                # Force-close backstop. pid is used ONLY to kill/reclaim (in _force_close); the DECISION
                # is "no advertised turn progress", not "pid gone".
                age = self.adapter.turn_age(key)
                # A per-capability wall-clock above the global one (code=1200s) extends this
                # backstop by the same amount, so it still sits ABOVE that turn's own hard-kill.
                _extra = getattr(self.adapter, "turn_deadline_extra", None)
                limit = self.progress_timeout + ((_extra(key) or 0) if _extra else 0)
                if age is not None and age > limit:
                    self._force_close(key, now, reason=f"turn silent > {limit:.0f}s (age={age:.0f}s)")
                elif age is None and not self.adapter.worker_alive(key):
                    self._force_close(key, now, reason="worker thread gone while state=open_running")
                continue

            if state == "open_idle":
                fwds = self.coalescer.take_forwards(key)    # atomic drain + flip to open_running
                if fwds:
                    self.adapter.forward(key, collapse(fwds))   # deliver ONE collapsed --resume turn
                    continue
                if self.coalescer.close_due(key, now):     # idle + empty buffer + receive-window quiet
                    if self.coalescer.begin_close(key):    # state=closing UNDER LOCK (arms the respawn block)
                        self._dispatch_close(key, now)     # SLOW teardown runs LOCK-FREE, then pop
                # else: stay open_idle
            # state in (closing, closed): teardown in flight / popped -> nothing (racing events go to pending)

    # ---- normal close (state=closing under lock, teardown lock-free, then closed + pop under lock) ----
    def _dispatch_close(self, key, now):
        t = threading.Thread(target=self._do_close, args=(key, now), name=f"r3-close-{key}", daemon=True)
        self._dispatch.append(t)
        t.start()

    def _do_close(self, key, now):
        # We are OUTSIDE the coalescer lock here (begin_close already set state=closing under it). Run the
        # SLOW teardown lock-free, THEN re-acquire (in finish_close) only to flip closed + pop.
        try:
            self.adapter.teardown(key)                  # slow: kill/session teardown + worktree rm (lock-free)
        except Exception as e:
            log.warning("teardown key=%s failed: %s", key, e)
        popped = self.coalescer.finish_close(key)       # closed + pop UNDER LOCK
        # A racing forward can't enter a closing worker (on_event routes it to pending), so leftover should
        # be empty; guard anyway so no buffered work is ever silently dropped.
        # rearm_pending drops (and logs) forward-only comments; only re-armed events are alerted.
        if popped and popped.get("fwd_buffer"):
            n = self.coalescer.rearm_pending(key, popped["fwd_buffer"], now)
            if n:
                self.alert_ops(f"close key={key}: {n} late buffered event(s) re-armed as a fresh spawn")

    # ---- force close (also re-arms a non-empty buffer) ----
    def _force_close(self, key, now, reason):
        try:
            self.adapter.teardown(key)                  # pid used ONLY here: kill + reclaim the cap slot
        except Exception as e:
            log.warning("force-close teardown key=%s failed: %s", key, e)
        popped = self.coalescer.force_pop(key)
        self.alert_ops(f"FORCE-CLOSE key={key}: {reason}")
        if popped and popped.get("fwd_buffer"):
            # Orphaned buffered events must be logged, alerted and re-armed (never lost).
            # Forward-only comments are dropped (and logged) by rearm_pending, not re-armed.
            n = self.coalescer.rearm_pending(key, popped["fwd_buffer"], now)
            if n:
                log.warning("FORCE-CLOSE key=%s ORPHANED %d buffered event(s) -> re-armed a fresh spawn", key, n)
                self.alert_ops(f"FORCE-CLOSE key={key}: {n} ORPHANED buffered event(s) re-armed as a fresh spawn")
