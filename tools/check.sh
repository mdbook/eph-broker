#!/bin/sh
# check.sh: the full test gate, CI-host neutral. Any CI (or a human) runs: sh tools/check.sh
set -eu
cd "$(dirname "$0")/.."
python3 coalescer.py --selftest
python3 tests/test_drain.py
python3 tests/test_drain_boundaries.py
python3 -m unittest discover -s tests -p test_dispatch_priority.py
python3 -m unittest discover -s tests -p test_move_to_front.py
python3 tests/test_actions.py
python3 tests/test_steer.py
python3 tests/test_broker.py
python3 tests/test_companions.py
python3 tests/test_hermes.py
python3 tests/test_hermes_gate.py
python3 tests/test_codex.py
python3 tests/test_sandbox.py
# codex OFFLINE real-binary probe: skips (and passes) with no codex binary on PATH / CODEX_BIN, so it runs safely
# here with nothing installed; ops / a CI host with the binary present gets it exercised for real.
python3 tests/test_codex_probe.py
python3 tests/test_gate_providers.py
python3 tests/test_routes.py
python3 tests/test_routing_overrides.py
python3 tests/test_tickets.py
python3 tests/test_trackers.py
python3 tests/test_github_tracker.py
python3 tests/test_vikunja_bridge.py
python3 tests/test_notify.py
python3 tests/test_events.py
python3 tests/test_config.py
python3 tests/test_quickstart.py
python3 tools/render-prompts.py --check
# the n8n nodes are JavaScript; skip them on a host without node
if command -v node >/dev/null 2>&1; then node --test integrations/n8n-nodes-eph-broker/test/*.test.js; fi
