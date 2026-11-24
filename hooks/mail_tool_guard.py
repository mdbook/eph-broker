#!/usr/bin/env python3
"""mail_tool_guard.py: narrow CC mail wrapper commands to the exact argv contract.

The wildcard in a CC read/archive permission cannot enforce argv cardinality. This hook does,
without granting anything itself. The wrapper fences the actual output, not a PostToolUse copy.
"""
import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adapters.hermes_gate.gate import Deny, split_command


def valid_mail_args(args, archive=False):
    """No options, extra args or control characters in opaque message IDs."""
    if args == ["list"]:
        return True
    verbs = ("read", "archive") if archive else ("read",)
    return (len(args) == 2 and args[0] in verbs and args[1] and not args[1].startswith("-")
            and not any(ord(c) < 32 or ord(c) == 127 for c in args[1]))


def mail_call(command, tool, archive=False):
    """None for unrelated commands; False for malformed mail calls; argv for the exact contract.

    Canonical shell quoting keeps opaque IDs possible without expansions, operators or extra args.
    No option-like IDs: the installed command must never interpret worker-selected flags.
    """
    if not isinstance(command, str):
        return False
    prefix = shlex.split(tool)
    target = prefix[-2] if len(prefix) > 1 else prefix[0]
    try:
        segments = split_command(command)
    except Deny:
        return False if target in command else None
    if not any(target in word for argv in segments for word in argv):
        return None
    if len(segments) != 1:
        return False
    argv = segments[0]
    if argv[:len(prefix)] != prefix or command != " ".join(shlex.quote(a) for a in argv):
        return False
    if valid_mail_args(argv[len(prefix):], archive):
        return argv
    return False


def decide(event, tool, archive=False):
    if event.get("hook_event_name") != "PreToolUse":
        return None
    call = mail_call((event.get("tool_input") or {}).get("command"), tool, archive)
    if call is None:
        return None
    if not call:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "mail_tool permits only standalone list/read <id>"
                + ("/archive <id>" if archive else " (archive posture disabled)")}}
    return None


if __name__ == "__main__":
    try:
        result = decide(json.load(sys.stdin), sys.argv[1], sys.argv[2] == "true")
    except Exception:
        # Exit 2 blocks the call at PreToolUse, rather than widening the grant on hook failure.
        print("mail_tool guard error (fail-closed)", file=sys.stderr)
        sys.exit(2)
    if result:
        print(json.dumps(result))
