#!/usr/bin/env python3
"""mail_tool_fence.py: fence the actual stdout and stderr of a configured mail command.

Only this broker-owned wrapper is granted to workers. Use only the broker-resolved per-run environment,
never sandbox-hidden config. Capture success, failure and setup errors before emitting one fence.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(args):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = 2
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            from hooks.mail_tool_guard import valid_mail_args
            account = os.environ.get("BROKER_MAIL_ACCOUNT")
            tool = os.environ.get("BROKER_MAIL_TOOL")
            archive = os.environ.get("BROKER_MAIL_ARCHIVE") == "1"
            if not args or not account or args[0] != account:
                raise ValueError("mail wrapper requires the run's configured account")
            if not tool or not valid_mail_args(args[1:], archive):
                raise ValueError("mail wrapper permits only list/read <id> and posture-enabled archive <id>")
            result = subprocess.run([tool] + args[1:], stdin=subprocess.DEVNULL, capture_output=True)
            stdout.write(result.stdout.decode("utf-8", errors="replace"))
            stderr.write(result.stderr.decode("utf-8", errors="replace"))
            code = result.returncode if result.returncode >= 0 else 128 - result.returncode
        except Exception as exc:
            print("mail wrapper refused: " + str(exc), file=sys.stderr)
    # Normalize look-alikes before neutralizing brackets. JSON escapes terminal control characters.
    def safe(value):
        return unicodedata.normalize("NFKC", value).replace("<", "‹").replace(">", "›")
    print("<<<BEGIN UNTRUSTED MAIL>>>")
    print(json.dumps({"stdout": safe(stdout.getvalue()), "stderr": safe(stderr.getvalue()),
                      "exit_code": code}, ensure_ascii=False))
    print("<<<END UNTRUSTED MAIL>>>")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
