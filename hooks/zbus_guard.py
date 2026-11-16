#!/usr/bin/env python3
"""zbus_guard.py: apply the shared fleet DM policy as a CC Bash hook."""
import json
import argparse
import os
import sys

# Resolve only the broker's installed code, never the worker cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adapters.hermes_gate.dm import bus_call, decide, dm_ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("patterns", nargs="*")
    parser.add_argument("--deny", action="append", default=None)
    parser.add_argument("--triage-say", action="store_true")
    parser.add_argument("--zbus-path")
    args = parser.parse_args()
    result = decide(sys.stdin.read(), args.patterns, denied=args.deny, triage_say=args.triage_say,
                    zbus_path=args.zbus_path)
    if result:
        print(json.dumps(result))
