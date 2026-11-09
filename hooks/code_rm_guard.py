#!/usr/bin/env python3
"""code_rm_guard.py - PreToolUse(Bash) hook for the "code" change-proposer capability.

WHY THIS EXISTS: a code worker commits inside a disposable worktree but has no way to DELETE a file -
`git rm`, `git revert --no-commit` and `git update-index --remove` are all plain Bash calls, and `git rm`
has no allowed-tools entry anywhere, so it is simply not allow-listed and is denied headless (the same
fate as any other unlisted Bash command). That turns a one-line cleanup (drop a scratch file a worker
accidentally committed) into a stuck worktree only an operator can fix by hand.

The CC Bash-pattern matcher (allowedTools/disallowedTools) is a literal prefix/glob match on the command
TEXT ONLY; it has no filesystem awareness, so it can never express "every path operand resolves inside the
worktree" or "no symlink out" - only a hook that actually resolves each operand against the real
filesystem can decide that. This hook is that check, and it is ALLOW-ONLY: unlike a deny-closing guard
(which narrows a broad allow-list grant), `git rm` carries NO allow-list entry at all, so when this hook
does not recognise the shape as safe it simply gives no decision and the call is denied exactly as it is
today (fail-closed; nothing here can make `git rm` MORE permissive than before).

DENY-FLOOR PRECEDENCE - NOT RELIED UPON: whether a `disallowedTools` deny outranks a PreToolUse hook's
allow answer is not re-verified here (a hook allow may well short-circuit the harness's permission
pipeline for that call, floor included - the exact precedence is the harness's, not this file's, to
define). So this hook does NOT claim the floor "still wins over it" (nor the reverse). It stays safe by
the exact SHAPE it allows instead: a bare `git rm` whose operands are literal, EXISTING, in-worktree
relative paths, with no `-`-leading operand and no flag but `-r`/`-q`/`--cached`/`--`. That shape carries
no deny-floor entry of its own (`git rm` is not on the code row's write floor), and the `-`-leading-operand
ban means the command text can never carry a floor-matched `--flag` substring (`--output`, `--no-index`,
`--paths`, ...) either, so a hook allow cannot smuggle past a floor entry even if the floor is checked
afterwards.

SAFE SHAPE (all of these must hold, or the hook stays silent):
  * the WHOLE raw command matches a strict character allowlist (see _RAW_CHARSET_RE) - no newline, carriage
    return, tab or any other control character (rejected BEFORE tokenizing, so shlex can never fold a
    `git rm f.py` + a second injected line into one segment), and none of the quote/backtick/`$`/glob/
    shell-metacharacter channels;
  * exactly one command, no chaining (`;`, `&&`, `|`, ...), no substitution (backticks, `$(...)`);
  * the command is literally `git rm` (no global git options, no wrapper like `bash -c` / `env` / an
    absolute git path) followed only by the flags `-r`, `-q`, `--cached` (any bundle of `-r`/`-q`, e.g.
    `-rq`), an optional bare `--`, and one or more PATH operands;
  * every path operand is a literal relative path: not absolute, not `-`-leading, no `..`/empty segment,
    no glob metacharacter (`*`, `?`, `[`, `]`);
  * every path operand names something that actually EXISTS on disk (a made-up name is not a real deletion
    target) and RESOLVES (following any symlink) to somewhere inside the worktree root - a syntactically
    clean relative path that walks through a symlinked directory out of the worktree is rejected just the
    same as `../x` would be.

Any other flag (`-f`/`--force`, `-n`/`--dry-run`, ...), any unresolvable/ambiguous shape, or a parse
failure gets NO decision (silently allows the existing, unchanged behaviour to apply).
"""
import json
import os
import re
import shlex
import sys

_OPERATOR_CHARS = set("();<>|&")
_SAFE_BUNDLE_RE = re.compile(r"^-[rq]+$")   # any bundle of -r / -q only (e.g. -r, -q, -rq, -qr)
_GLOB_CHARS = set("*?[]")

# ---- strict raw-character allowlist (fail closed by construction) -----------------------------------
# shlex is not a shell parser: it treats a newline as mere whitespace, so `git rm f.py\nsh evil.sh` is ONE
# segment to it and would otherwise be judged on the harmless `git rm f.py` prefix while the shell would
# run a second command. Rather than blacklist the newline alone, require the WHOLE raw command to be made
# of a conservative character set with single plain spaces between words. Once that holds, there is no
# quote, backslash, `$`, backtick, glob, brace, tilde or control character left for the shell to act on, so
# shlex's tokens are provably identical to the shell's own argv for that text. A leading/trailing space is
# stripped first (`.strip(" ")`, never a bare `.strip()` - the latter also eats tab/NEL/... which is exactly
# the kind of byte this charset exists to catch).
_RAW_CHARSET_RE = re.compile(r"^[A-Za-z0-9_./:=,+@-]+(?: [A-Za-z0-9_./:=,+@-]+)*$")
# C0 controls (newline, CR, tab, NUL, ...) plus DEL, rejected before any tokenizing happens.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _tokens(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def _segments(tokens):
    """Split on shell operator tokens; more than one segment means chaining/piping."""
    seg = []
    for t in tokens:
        if t and set(t) <= _OPERATOR_CHARS:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg


def _safe_path(p, root):
    """True iff `p` is a literal relative path with no glob/`-`-leading/`..`/absolute component, NAMES
    something that exists on disk, AND resolves (following symlinks) to somewhere inside `root`."""
    if not p or os.path.isabs(p) or p.startswith("-") or any(c in p for c in _GLOB_CHARS):
        return False
    parts = p.split("/")
    if any(part in ("", "..", ".") for part in parts):
        return False
    full = os.path.join(root, p)
    if not os.path.lexists(full):
        return False                       # a made-up/nonexistent name is not a real deletion target
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(full)
    return real_path == real_root or real_path.startswith(real_root + os.sep)


def _rm_paths(args):
    """-> the path operands in `args` (the words after `git rm`) iff every flag before them is one of
    the safe forms (`--cached`, a bundle of `-r`/`-q`) and at least one path operand follows; None
    otherwise (an unrecognised flag, a `-`-leading operand, or no path at all). A `-`-leading operand is
    rejected even AFTER a bare `--`: no real deletion target starts with a dash, and refusing it keeps a
    floor-matched `--flag` substring out of every command this hook can allow."""
    paths = []
    seen_dashdash = False
    for t in args:
        if not seen_dashdash and t == "--":
            seen_dashdash = True
            continue
        if not seen_dashdash and t.startswith("-") and t != "-":
            if t == "--cached" or _SAFE_BUNDLE_RE.match(t):
                continue
            return None
        if t.startswith("-"):
            return None
        paths.append(t)
    return paths or None


def _git_rm_safe(cmd, root):
    if _CTRL_RE.search(cmd) or "`" in cmd or "$(" in cmd:
        return False
    stripped = cmd.strip(" ")
    if not _RAW_CHARSET_RE.match(stripped):
        return False
    try:
        toks = _tokens(stripped)
    except ValueError:
        return False
    segs = list(_segments(toks))
    if len(segs) != 1:
        return False                               # chained/piped: never pre-allowed
    seg = segs[0]
    if len(seg) < 3 or seg[0] != "git" or seg[1] != "rm":
        return False                                # not a bare `git rm` (no -C/-c, no wrapper)
    paths = _rm_paths(seg[2:])
    return bool(paths) and all(_safe_path(p, root) for p in paths)


_ALLOW = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": (
            "code-rm guard: a bare `git rm` whose whole command matches a strict character allowlist and "
            "whose every path operand is a literal relative path that exists and resolves inside the "
            "worktree (no globs, no .., no absolute or -leading operand, no symlink out)."
        ),
    }
}


def decide(raw):
    """None = no decision (the call falls through to whatever would happen without this hook); else
    the allow payload."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    cmd = (payload.get("tool_input") or {}).get("command")
    if not isinstance(cmd, str):
        return None
    root = payload.get("cwd") or os.getcwd()
    return _ALLOW if _git_rm_safe(cmd, root) else None


def main():
    try:
        out = decide(sys.stdin.read())
    except Exception:
        out = None          # a hook bug must never manufacture an allow it did not earn
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
