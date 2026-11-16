#!/usr/bin/env python3
"""dm.py: shared fleet DM policy and single-plain-call guard.

Patterns match recipient words, never shell text. No credential file is read.
"""
import fnmatch
import json
import os
import re
import shlex


BUS_VERBS = frozenset(("dm", "say", "read", "listen", "fetch", "join", "react", "users",
                       "whoami", "ratelimit", "selftest-resolve"))


DEFAULT_DM_DENY = "*-driver-bot@*"
TRIAGE_SAY_PREFIXES = ()


def recipients(value, operator="", setting="AGENT_DM_RECIPIENTS"):
    """Comma-separated literal handles or * globs; unsafe permission-rule syntax fails closed."""
    patterns = [p.strip() for p in value.split(",") if p.strip()]
    if operator:
        patterns.append(operator)
    if any(not re.fullmatch(r"[A-Za-z0-9_.+*@-]+", p) or "@" not in p for p in patterns):
        raise ValueError(setting + " must contain comma-separated handles or * globs with @")
    return list(dict.fromkeys(patterns))


def dm_denied():
    return recipients(os.getenv("AGENT_DM_DENY", DEFAULT_DM_DENY), setting="AGENT_DM_DENY")


def bus_call(cmd, zbus_path=None, _depth=0):
    """Recognize bus executables, including whitespace-bearing command strings.

    Inspect shell segments and re-parse whitespace-bearing words regardless of their wrapper.
    Uncertain input fails closed on raw bus text; recursion is bounded even for substitutions.
    """
    if not isinstance(cmd, str):
        return False
    path = zbus_path or os.getenv("NOTIFY_ZBUS", "zbus")
    if _depth >= 4:
        return "zbus" in cmd or path in cmd

    def bus_word(word):
        return os.path.basename(word) == "zbus" or word == path

    # At this parse level, substitutions execute outside single quotes. Whitespace-bearing
    # words are conservatively re-parsed below even when originally quoted.
    normalized = []
    quote, i = None, 0
    while i < len(cmd):
        c = cmd[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif c == "\\":
            normalized.append(c)
            i += 1
            if i == len(cmd):
                break
            c = cmd[i]
        elif quote is None and c == "#" and (i == 0 or cmd[i - 1].isspace() or cmd[i - 1] in "();<>|&"):
            # Quotes in comments cannot hide the next command's newline boundary.
            while i < len(cmd) and cmd[i] not in "\n\r":
                i += 1
            continue
        elif c == "`" or cmd[i:i + 2] == "$(":
            start, close = (i + 1, "`") if c == "`" else (i + 2, ")")
            end = cmd.find(close, start)
            end = len(cmd) if end < 0 else end
            if bus_call(cmd[start:end], path, _depth + 1):
                return True
            # A name lookup does not itself invoke the bus. Model its result so
            # $(which zbus) in executable position is caught, but echoing it is not.
            try:
                lookup = shlex.split(cmd[start:end])
            except ValueError:
                lookup = []
            if (lookup and (os.path.basename(lookup[0]) == "which"
                            or lookup[:2] in (["command", "-v"], ["command", "-V"]))
                    and any(bus_word(t) for t in lookup[1:])):
                normalized.append("zbus")
            else:
                normalized.append(cmd[i:end + 1])
            i = end + 1
            continue
        elif quote is None and c in "\n\r":
            c = ";"  # shlex otherwise consumes command boundaries as whitespace.
        elif c == quote:
            quote = None
        elif quote is None and c in "\"'":
            quote = c
        normalized.append(c)
        i += 1
    lex = shlex.shlex("".join(normalized), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""  # Comments were removed above; a # inside a word is literal shell text.
    tokens = []
    malformed = False
    try:
        tokens.extend(lex)
    except ValueError:
        malformed = True
    if malformed and ("zbus" in cmd or path in cmd):
        return True

    # Split-string options and quoted shell programs can hide a command in any word,
    # not just an operand of a known wrapper. Use the same parser at every level.
    if any(any(c.isspace() for c in word) and bus_call(word, path, _depth + 1) for word in tokens):
        return True

    redirections = {"<", ">", "<<", "<<<", ">>", "<>", "<&", ">&", ">|", "&>", "&>>"}

    def segment_invokes(args):
        # Redirections belong to the command, not to a new executable segment. shlex
        # separates both attached filenames and fd operands (2>&1 -> 2, >&, 1).
        words, i = [], 0
        while i < len(args):
            if args[i] in redirections:
                if words and re.fullmatch(r"[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\}", words[-1]):
                    words.pop()
                i += 1
                if i == len(args) or args[i] in redirections:
                    # An uncertain parse must not let a bus invocation reach the judge.
                    return any(bus_word(t) or re.search(r"\bzbus\b", t) or path in t for t in args)
            else:
                words.append(args[i])
            i += 1
        return invokes(words)

    def invokes(args):
        while args and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", args[0], re.S):
            args = args[1:]
        if not args:
            return False
        prog = os.path.basename(args[0])
        if bus_word(args[0]):
            return True
        if prog in ("env", "command", "exec"):
            rest = args[1:]
            if prog == "command" and rest and rest[0].startswith("-") and any(c in rest[0] for c in "vV"):
                return False  # command -v/-V describes a name; it does not invoke it.
            while rest and (rest[0].startswith("-") or "=" in rest[0]):
                split = re.match(r"(?:-S|--split-string=)(.+)", rest[0], re.S) if prog == "env" else None
                if split:
                    # env -S'zbus dm ...' attaches the command string to the option.
                    try:
                        return invokes(shlex.split(split.group(1)) + rest[1:])
                    except ValueError:
                        return True
                if prog == "env" and rest[0] in ("-u", "--unset", "-C", "--chdir"):
                    rest = rest[2:]
                else:
                    rest = rest[1:]
            return invokes(rest)
        # These wrappers can execute later words. Whitespace-bearing words were scanned above.
        # Detection is deliberately conservative; only a bare plain call can be granted.
        if prog in ("timeout", "nohup", "nice", "time", "xargs", "eval", "sudo",
                    "sh", "bash", "zsh", "dash", "{", "if", "then", "elif", "else",
                    "while", "until", "do", "!"):
            return any(bus_word(t) for t in args[1:])
        # Any other program may still execute a later word (stdbuf, setsid, ionice ...).
        # A bus word followed by a bus verb is an invocation; a bare mention is not.
        return any(bus_word(a) and b in BUS_VERBS for a, b in zip(args[1:], args[2:]))

    seg = []
    # shlex groups adjacent punctuation, including mixed runs such as >| or ;>.
    # Split those into shell operators before distinguishing redirections from boundaries.
    operators = r"&>>|&>|<<<|<<|>>|<>|<&|>&|>\||[<>]|&&|\|\||;;|;&|[();|&]"
    shell_tokens = []
    for token in tokens:
        shell_tokens.extend(re.findall(operators, token) if token and set(token) <= set("();<>|&") else [token])
    for token in shell_tokens + [";"]:
        if token not in redirections and token and set(token) <= set("();|&"):
            if segment_invokes(seg):
                return True
            seg = []
        else:
            seg.append(token)
    return False


def _plain_args(cmd):
    if "`" in cmd or "$(" in cmd or "\n" in cmd or "\r" in cmd:
        return None
    # shlex removes quotes but does not model shell expansion. Reject active dollars before
    # tokenizing, while preserving single-quoted and backslash-escaped literal text.
    quote, escaped = None, False
    for c in cmd:
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if c == "'":
                quote = None
            continue
        if c == "\\":
            escaped = True
        elif c == "$":
            return None
        elif quote is None and c in "{}[]*?~":
            return None
        elif c == quote:
            quote = None
        elif quote is None and c in "\"'":
            quote = c
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        args = list(lex)
    except ValueError:
        return None
    if any(t and set(t) <= set("();<>|&") for t in args):
        return None
    return args


def dm_ok(cmd, patterns, text_only=False, denied=None):
    """One bare zbus DM only. Deny patterns win over every recipient grant."""
    if isinstance(patterns, str):
        patterns = [patterns] if patterns else []
    args = _plain_args(cmd)
    if args is None:
        return False
    if len(args) < 4 or args[:2] != ["zbus", "dm"]:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+", args[2]):
        return False
    # Zulip resolves email addresses case-insensitively. Require canonical recipients
    # and normalize both policy lists so capitalization cannot evade a deny.
    if args[2] != args[2].lower():
        return False
    if not any(fnmatch.fnmatchcase(args[2], p.lower()) for p in patterns):
        return False
    if any(fnmatch.fnmatchcase(args[2], p.lower()) for p in (dm_denied() if denied is None else denied)):
        return False
    if any(t.startswith("-") for t in args[3:]):
        return not text_only and args[3:] == ["--stdin"]
    return True


def triage_say_ok(cmd):
    args = _plain_args(cmd)
    return bool(args and len(args) >= 5 and " ".join(args[:4]) in TRIAGE_SAY_PREFIXES
                and not any(t.startswith("-") for t in args[4:]))


def decide(raw, patterns, denied=None, triage_say=False, zbus_path=None):
    cmd = None
    try:
        cmd = (json.loads(raw).get("tool_input") or {}).get("command", "")
        if not bus_call(cmd, zbus_path):
            return None
        if dm_ok(cmd.strip(), patterns, denied=denied) or (triage_say and triage_say_ok(cmd.strip())):
            return None
    except (ValueError, TypeError, AttributeError):
        if not bus_call(cmd, zbus_path) and not bus_call(raw or "", zbus_path):
            return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "Bus calls require one plain allowed DM or an authorized triage post."}}
