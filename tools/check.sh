#!/bin/sh
# check.sh: the full test gate, CI-host neutral. Any CI (or a human) runs: sh tools/check.sh
set -eu
cd "$(dirname "$0")/.."
python3 coalescer.py --selftest
python3 tests/test_broker.py
python3 tests/test_hermes.py
python3 tests/test_hermes_gate.py
python3 tests/test_routes.py
python3 tests/test_tickets.py
python3 tests/test_trackers.py
python3 tests/test_notify.py
python3 tests/test_events.py
python3 tests/test_config.py
python3 tools/render-prompts.py --check
# the n8n nodes are JavaScript; skip them on a host without node
if command -v node >/dev/null 2>&1; then node --test integrations/n8n-nodes-eph-broker/test/*.test.js; fi
