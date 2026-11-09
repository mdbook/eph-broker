#!/usr/bin/env python3
"""Active CC deny for companion edits, including on adjudicated turns where hook allows bypass denies."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from adapters.hermes_gate import gate
from adapters.codex_gate import _changes_dir


def reason(event, root, test_allow=()):
    policy = gate.Policy({"cwd": root, "capability": "code", "test_allow": list(test_allow)})
    tool = event.get("tool_name")
    args = event.get("tool_input") or {}
    if tool == "Bash":
        command = args.get("command")
        try:
            segments = gate.split_command(command)
        except gate.Deny:
            # Aliases and computed paths can reach companions without naming the reserved tree.
            # An adjudicator allow must not bypass this floor when operands cannot be inspected.
            return "companion command cannot be safely parsed"
        for argv in segments:
            if _changes_dir(argv):
                return "directory changes are not allowed on companion-enabled turns; use literal paths"
            skip = gate._git_hard(argv)[1] if argv[0] == "git" else ()
            why = gate._companion_command(policy, argv, root, skip)
            if why:
                return why
        return None
    path = args.get("file_path") or args.get("notebook_path")
    if not isinstance(path, str) or not path:
        return "missing edit path"
    if gate._companion_path(policy, *gate._resolve(path, root)):
        return "companion sources are read-only"
    return None


def main():
    try:
        why = reason(json.load(sys.stdin), sys.argv[1], json.loads(sys.argv[2]))
    except Exception:
        why = "companion guard error (fail-closed)"
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                "permissionDecisionReason": why}}))


if __name__ == "__main__":
    main()
