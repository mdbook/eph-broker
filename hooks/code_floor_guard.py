#!/usr/bin/env python3
"""PreToolUse(Bash) hard-floor guard for an ADJUDICATED code turn.

A PreToolUse hook's "allow" (e.g. Portcullis's) bypasses the harness's own --disallowedTools, so the code
lane's deny-floor must not depend on the adjudicator agreeing. This hook ACTIVELY answers permissionDecision
"deny" for any command matching the floor (adapters/cc.py _CODE_ADJUDICATED_DENIED, JSON on argv[1]).
Claude Code runs the matching PreToolUse hooks in PARALLEL, so there is no ordering between this hook and
Portcullis; the protection comes from a hook deny beating a hook allow. It gives no answer for anything else.

The match is deliberately NOT shell-aware (shell syntax cannot bypass it); it over-matches and a false deny
is the accepted cost:
  * the command (whitespace collapsed, quotes/backslashes also stripped as a second form) is cut into pieces
    on ; & | newline ( ) { } ` and `$(`; each piece has leading `! ( { $(`/backtick openers stripped and is
    matched against every floor pattern (so `true & rm -rf x`, `(rm -rf x)`, `{ git reset --hard; }` hit);
  * for every floor pattern of the plain "<command> *" shape, that command text appearing ANYWHERE in the
    command at a word boundary denies (`echo $(git push origin HEAD)`, `bash -c 'rm -rf src'`,
    `xargs rm`); `git rm` is exempt for the bare-word `rm` token (the code lane's own git-rm guard owns it);
  * QUOTED TEXT is data: unless the command contains an interpreter construct (`$(`, backtick, `<(`, eval,
    xargs, exec, sh/bash/zsh -c, python/perl/..., a command wrapper such as env/nohup/time), the token-anywhere
    scan ignores quoted strings that are not in command position, so `git commit -m "drop sudo usage"` and
    `grep -rn "rm" src` are not denied; with an interpreter construct the whole raw text is scanned as above.
    (Piece-start matching still sees quoted words, so `'rm' -rf x` is denied.);
  * git global options: `git -c ...`, `-C`, `--git-dir`, `--work-tree` (any `--x`) and a GIT_DIR=/GIT_WORK_TREE=
    assignment anywhere deny; harmless globals (--no-pager, -P, --bare, ...) are skipped before matching the
    floored subcommand, so `git --no-pager push origin HEAD` denies;
  * a command with a dynamic construct (`$(`, backtick, `<(`, `-c`, `eval`) is further denied when ALL the
    literal parts of ANY floor pattern appear in order anywhere in it (covers the mid-star patterns).

FAILS CLOSED: any internal error (import failure, bad argv, parse error, non-string command) answers deny.
Reuses the pattern matcher of code_test_shaped_guard.
"""
import importlib.util
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _shaped():
    spec = importlib.util.spec_from_file_location("_code_test_shaped_guard",
                                                  os.path.join(_HERE, "code_test_shaped_guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PIECE_SPLIT = re.compile(r"&&|\|\||\$\(|[;&|\n(){}`]")
_OPENERS = re.compile(r"^[\s!({]+")
_DYNAMIC = re.compile(r"\$\(|`|<\(|(?<!\S)-[A-Za-z]*c(?!\S)|\beval\b")


def _forms(cmd):
    """The command as written, whitespace-collapsed, and with quotes/backslashes removed (`r''m`, `\\rm`)."""
    out = []
    for c in (cmd, re.sub(r"[\"'\\]", "", cmd)):
        c = re.sub(r"\s+", " ", c).strip()
        if c not in out:
            out.append(c)
    return out


def _patterns(floor):
    for entry in floor:
        if isinstance(entry, str) and entry.startswith("Bash(") and entry.endswith(")"):
            yield entry[len("Bash("):-1].replace(":*", "*")


def _token_regex(inner):
    """Regex for a trailing-star-only pattern's command text at a word boundary anywhere; else None."""
    parts = inner.split("*")
    if len(parts) != 2 or parts[1] or not parts[0].strip():
        return None
    tok = re.sub(r"\s+", " ", parts[0]).strip()
    rx = r"(?<![\w.-])" + ("(?<!git )" if not tok.startswith("git ") else "") + re.escape(tok)
    if tok[-1].isalnum() or tok[-1] == "_":
        rx += r"(?![\w-])"
    return re.compile(rx)


def _subseq_regex(inner):
    lits = [re.sub(r"\s+", " ", p).strip() for p in inner.split("*")]
    lits = [p for p in lits if p]
    return re.compile(".*".join(re.escape(p) for p in lits), re.DOTALL) if lits else None


# Constructs that run text the guard cannot see through (command substitution, eval, a shell/interpreter given
# code, xargs/exec, command wrappers). A command containing one is matched on its WHOLE raw text, quoted parts
# included; only a command free of them gets its quoted strings ignored by the token-anywhere match.
_INTERP = re.compile(
    r"\$\(|`|<\(|\beval\b|\bxargs\b|\bexec\b"
    r"|\b(?:env|nohup|command|time|nice|ionice|setsid|timeout|stdbuf|builtin|doas|busybox|flock|watch|parallel)\b"
    r"|\b(?:python[\d.]*|perl|ruby|node|php|lua|awk|gawk|sed)\b"
    r"|\b(?:ba|z|da|k|c|tc|fi)?sh\b[^;&|\n]*?\s-[A-Za-z]*c\b")
_QUOTED = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")
_ASSIGN_TAIL = re.compile(r"(?:^|\s)\w+=(?:'[^']*'|\"[^\"]*\"|[^\s'\"]*)$")
_GIT_ENV = re.compile(r"(?<![\w.-])GIT_(?:DIR|WORK_TREE)=")
_GIT_WORD = re.compile(r"(?<![\w.-])git(?=\s)")
# git global options that are harmless to skip when looking for the subcommand; -c/-C/--git-dir/--work-tree
# (and the other path/config-redirecting ones) are denied outright instead.
_GIT_SKIP = frozenset({"--no-pager", "-P", "-p", "--paginate", "--bare", "--no-replace-objects",
                       "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs",
                       "--no-optional-locks", "--no-advice"})


def _in_command_position(prefix):
    """True iff the next word after `prefix` would be a command word (start, after a separator or `!`, after
    leading VAR=value assignments)."""
    prefix = prefix.rstrip()
    while True:
        m = _ASSIGN_TAIL.search(prefix)
        if not m:
            break
        prefix = prefix[:m.start()].rstrip()
    return not prefix or prefix[-1] in ";&|(){}`\n!"


def _strip_quoted(text):
    """Remove standalone quoted strings (a whole word, or a `=value`) that are NOT in command position:
    `-m "drop sudo usage"`, `grep -rn "rm" src`. A quoted word in command position (`'rm' -rf x`) and quotes
    glued into a word (`r'm'`) are kept, so the caller's unquoted form still sees them."""
    out, pos = [], 0
    for m in _QUOTED.finditer(text):
        before = text[m.start() - 1] if m.start() else " "
        after = text[m.end()] if m.end() < len(text) else " "
        if (before.isspace() or before == "=") and (after.isspace() or after in ";&|)}") \
                and not _in_command_position(text[:m.start()]):
            out.append(text[pos:m.start()])
            pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _git_normalized(text):
    """For text starting with `git`: (True, None) when it uses a path/config-redirecting global option
    (-c, -C, --git-dir, --work-tree, --namespace, ...), else (False, 'git <subcommand> ...') with the harmless
    global options skipped. (False, None) when it is not a git command."""
    toks = text.split()
    while toks and re.match(r"^\w+=", toks[0]):
        toks = toks[1:]
    if not toks or toks[0] != "git":
        return False, None
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        t = toks[i]
        if t in _GIT_SKIP:
            i += 1
        elif t in ("--version", "--help", "-h", "-v"):
            break
        elif re.match(r"^-[cC]", t) or t.startswith("--"):
            return True, None   # -c/-C (any attached form), --git-dir/--work-tree/--namespace/... (any `--x[=y]`)
        else:
            break
    return False, " ".join(["git"] + toks[i:])


def _git_denied(text, floor):
    denies = _shaped()._floor_denies
    for m in _GIT_WORD.finditer(text):
        redirect, norm = _git_normalized(text[m.start():])
        if redirect or (norm and denies(norm, floor)):
            return True
    return False


def violates(cmd, floor):
    """True iff `cmd` hits the floor under the over-matching rules in the module docstring."""
    denies = _shaped()._floor_denies
    pats = list(_patterns(floor))
    forms = _forms(cmd)
    interp = any(_INTERP.search(f) for f in forms)
    # token-anywhere scan texts: both forms of the raw text, except a plain command (no interpreter construct)
    # is scanned with its non-command-position quoted strings removed first (they are data, not commands).
    scans = forms if interp else [_forms(_strip_quoted(forms[0]))[-1]]
    for scan in scans:
        if _GIT_ENV.search(scan) or _git_denied(scan, floor):
            return True
        for inner in pats:
            rx = _token_regex(inner)
            if rx and rx.search(scan):
                return True
    for form in forms:
        pieces = [form] + [_OPENERS.sub("", p).strip() for p in _PIECE_SPLIT.split(form)]
        for p in pieces:
            if not p:
                continue
            redirect, norm = _git_normalized(p)   # piece-start git: global options skipped / denied
            if denies(p, floor) or redirect or (norm and denies(norm, floor)):
                return True
        if _DYNAMIC.search(form):
            for inner in pats:
                rx = _subseq_regex(inner)
                if rx and rx.search(form):
                    return True
    return False


def _deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def decide(raw, floor_patterns):
    """None = no answer; else the deny payload. Raises on anything unparseable (main() turns that into deny)."""
    if not isinstance(floor_patterns, list) or not floor_patterns:
        raise ValueError("floor patterns missing")
    cmd = (json.loads(raw).get("tool_input") or {}).get("command")
    if not isinstance(cmd, str):
        raise ValueError("no command string")
    if violates(cmd, floor_patterns):
        return _deny("code worker hard floor: this command matches the deny-floor (not adjudicable by Portcullis).")
    return None


def main(argv):
    raw = sys.stdin.read()
    try:
        out = decide(raw, json.loads(argv[0]) if argv else None)
    except Exception:
        out = _deny("code worker hard floor: guard error, failing closed.")
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        print(json.dumps(_deny("code worker hard floor: guard error, failing closed.")))
