#!/usr/bin/env python3
"""code_test_shaped_guard.py - PreToolUse(Bash) hook, the deterministic test-shaped-command allow rule for
a judged code worker.

Wired via adapters/cc.py build_argv's `--settings` ONLY for a judged (judged_bash=True) code turn, matcher
`Bash`, as `python3 -I code_test_shaped_guard.py <json-encoded code-row deny-floor patterns> <worktree root>`
(see _code_judged_hook_settings in adapters/cc.py; `-I` isolates the interpreter from the worker's PYTHON*
env + cwd on sys.path, and the worktree root on argv is the realpath-CONFINEMENT BOUNDARY - never the
worker-settable payload cwd; fix round 7 / F4, F5). The PreToolUse payload's `cwd` field is still read,
but only as the RESOLUTION BASE for a relative positional: that is the worker's persistent Bash cwd, the
directory the runner actually resolves a relative token against (`cd sub; pytest link` loads `sub/link`,
not `<root>/link`), and it is trusted ONLY once its realpath is verified inside the argv boundary. A
payload with no `cwd`, or a `cwd` whose realpath falls outside the boundary, fails CLOSED for any
path-like positional (fix round 8 / F5 regression: confining against the root alone let `cd sub; pytest
link` through when `sub/link` was a symlink out of the worktree).

WHY THIS EXISTS: under judged mode a code worker's Bash is classifier-judged (same `--permission-mode auto`
the general row uses) instead of a fixed test-runner menu, so an UNDECLARED or merely-INFERRED repo (no
CODE_REPO_RUNNERS/seed entry) has no pre-allowed test command at all - every test invocation has to cross
the classifier, turn after turn. This hook adds the ONE deterministic, judge-free fast path the review
asked for: a FIXED table of known test-runner invocations (data below, not code) that, when the whole Bash
call is a single simple command, gets answered "allow" straight away - for ANY repo, declared or not. The
existing allow-list (declared-runner fast path) is completely untouched; this hook only ever ADDS an allow
answer, it never denies.

FLOOR-WINS, CHECKED HERE (not just assumed): a PreToolUse hook's "allow" answer is NOT just another vote
alongside `--disallowedTools` - it short-circuits the harness's own permission pipeline for that tool call,
deny-floor included. So this hook cannot just trust "no table entry's program name overlaps the floor today"
and move on; it is handed the code row's ACTUAL deny-floor patterns (argv[1], JSON-encoded, from
adapters/cc.py's _CODE_DENIED) and checks a command against them itself, BEFORE ever answering "allow". A
command that is otherwise test-shaped but also matches a floor pattern gets NO answer from this hook (never
an active "deny" either - this hook still only ever adds an allow, never removes one), so it falls straight
through to the harness's normal pipeline, where the deny-floor enforces itself exactly as it would for any
other command.

A command is "test-shaped" when ALL hold:
  1. it is a single simple command: the raw text carries none of `; && || | > < $( \\`` or a newline, and
     every env-var prefix in front of the program (`NAME=value ...`) is decided on the RAW token text (not
     a quote-stripped one - see _strip_env_prefix below) against a small fixed allowlist of inert names;
  2. the WHOLE raw text matches a strict character allowlist - see _RAW_CHARSET_RE below - which closes
     shell word-expansion by construction: that alphabet carries no quote, no `$ { } * ? [ ] ~ ! \\ ( ) ; & |
     < >`, no backslash and no whitespace beyond a single ASCII space between words, so for that text
     shlex's word-splitting agrees with the shell's on every word shape the alphabet can express (round 4
     found shlex leaves all those bytes unexpanded, so a flag allowlist checked on shlex's OUTPUT is
     bypassable wholesale by anything the shell itself would expand into a denied flag). The worker's shell
     is zsh (SHELL=/bin/zsh), not bash, but the alphabet already excludes zsh's `~`/backtick/glob forms;
     the ONE zsh channel it cannot exclude is a word that BEGINS with `=` (`=pytest` -> the absolute path
     of `pytest`), because `=` must stay in the alphabet to spell `--flag=value` and the NAME=value prefix
     - so a token beginning with `=` fails is_test_shaped outright (checked after shlex.split below);
  3. its argv (after stripping that env prefix) matches one row of _TEST_SHAPED_TABLE below, including that
     row's own ALLOWLIST of further flags (a closed set of plain selection/verbosity/output flags; anything
     not on it never matches - see "Runner flags" below) and its own positional policy (a bare positional
     starting with `@` - a pytest/fromfile-style argfile - never matches ANY row; beyond that, pytest/
     `python -m` positionals must stay inside the worktree, go accepts only `.`/`./`-prefixed import paths
     (see "Runner flags"), tox accepts only a bare `run` positional, and nox accepts no bare positional at
     all - see "Runner flags" below);
  4. it does not ALSO match one of the code row's deny-floor patterns (checked at decide() time, not baked
     into the table - see FLOOR-WINS above).

Runner flags, ALLOWLIST not blocklist: pytest / go test / cargo test / tox / nox each get a short, fixed
allowlist of plain selection/verbosity/output flags (e.g. `-q -v -x -k EXPR`, `-run`, `-count`, `--lib`,
`-e ENV`) - see _PYTEST_ALLOWED etc below. argv is walked sequentially after the row's fixed prefix: a
token starting with `-` must be EXACTLY one of that row's allowed flags (an attached `--flag=value` form
only counts when `--flag` itself is on the list AND takes a value) or the row does not match at all. A
value-taking flag consumes the next token as its value - that value must not itself start with `-` (a
flag with a variable-length value, like nox's `-s`/`--sessions`, gets at most ONE non-dash value this way;
zero values, or a dash-leading "value", fails the row instead of letting the next token slip through as a
different flag). A token NOT starting with `-` is a positional; `@...` (a pytest/fromfile-style argfile
that reads arbitrary flags from a file the worker can Write) fails the row for every runner, and each row
may additionally supply a `positional_ok` predicate that every other bare positional must satisfy -
_is_worktree_confined_path (pytest / `python -m pytest|unittest`: no absolute/`~`/`..`-component path, so a
positional can't point the run at a tree outside the worktree or outside this repo's conftest/ini),
_is_go_import_path (go test: only `.` or a `./`-prefixed relative path, because a BARE import path like
`m/link` or a meta-package like `all`/`std`/`net/http` is resolved through the module's own go.mod import
space - a worker-writable go.mod can alias a module name to a directory outside the worktree, so those
names are not path-text-confined at all - see fix round 7 / F1), tox's "only a bare `run`" predicate
(every other tox 4 subcommand - devenv, quickstart, exec, ... - takes an arbitrary path or writes outside
the worktree), or nox's "never" predicate (session selection only through `-s`, never a second free
positional); a row with no predicate (cargo test, whose positional is a plain test-name substring filter)
accepts any other bare positional unchanged, as before.
This flag table used to be a BLOCKLIST of specific open-ended flags (ones that load/execute other code or
move the run root); blocklists keep missing flags as runners grow new ones, so this hook now enumerates
the small set of SAFE flags instead and lets everything else fall through.

A command that merely LOOKS test-shaped - a chained/redirected/substituted command, a quoted/escaped
env-prefix word, any character outside the strict raw-text allowlist (quotes, `$ { } * ? [ ] ~ ! \\` ( ) ;
& | < >`, backslash - the shell-expansion channels item 2 above closes), a token beginning with `=` (zsh's
`=cmd` -> absolute-path filename expansion), a runner flag not on that row's allowlist, a value-taking
flag's value starting with `-`, an `@argfile` positional, an out-of-worktree/traversal positional for
pytest/`python -m`, a go test positional that is not `.`/`./`-prefixed, any non-`run` tox subcommand or any
nox positional,
a program named by a path instead of a bare name, or a `sh`/`bash` call naming a script outside
`tests/`/`scripts/` (absolute, `~`, or any `..` component) or whose filename isn't `test*.sh`/`*_test.sh` -
never matches and gets NO answer at all (no stdout, exit 0), so it falls straight through to whatever
would already handle it today: the deny-floor if it's on that, the classifier otherwise. "No answer" is
the ONLY output this hook ever gives for anything it doesn't recognize or doesn't fully clear; it is never
the thing that turns a borderline command into a deny - that job stays with the floor/classifier.

Extending the table: add one row to _TEST_SHAPED_TABLE (and, if it takes further flags, a small allowed-
flags dict for it); no other code in this file needs to change.
"""
import json
import os
import re
import shlex
import sys

# ---- "single simple command" check -----------------------------------------------------------------
# Textual, on the WHOLE raw command - deliberately simpler than a real shell parser: a command carrying any
# of these, anywhere (including inside quotes), is never treated as test-shaped, full stop. False negatives
# here just mean a legitimate command gets no fast path and goes to the classifier as it would today; that
# is always the safe direction. `&` alone (not just `&&`) is included: a lone `&` backgrounds the first
# command and keeps parsing the rest on the same line, the same "two commands on one Bash call" shape `&&`
# is denied for.
_UNSAFE_CHARS = set(";|><&`")


def _is_single_simple_command(raw):
    if not raw or "\n" in raw or "$(" in raw:
        return False
    return not any(c in _UNSAFE_CHARS for c in raw)


# ---- strict raw-character allowlist, closing shell expansion "by construction" (round 4 / B1) ---------
# shlex.split() is not a shell parser: it does not model bash word expansion at all, so it leaves `$'..'`,
# `$".."`, `${..}`, brace expansion `{a,b}`, globs `* ? [..]`, `~`, and similar syntax completely unexpanded
# as plain token text - a downstream check on shlex's OUTPUT (the per-row flag allowlist, _tail_allowed
# below) cannot tell "this token is a literal string" from "this token is something bash will expand into an
# entirely different, possibly denied, flag before the program ever sees it" (e.g. `pytest $'-p' os` is one
# harmless-looking shlex token `$'-p'` that bash actually passes as the flag `-p`). _is_single_simple_command
# above only blocks a handful of individual operator characters; this closes the channel by construction
# instead: the text must be made ENTIRELY of this conservative character set, with single plain spaces
# between words and nothing else. Once that holds, the shell has no quote, expansion, substitution or glob
# syntax left to act on for every word shape the alphabet can express, so shlex.split's tokens agree with
# the shell's own argv for that text - the allowlist checks below then check what actually runs, not a
# shlex-only approximation of it. (The worker's shell is zsh, not bash; the alphabet excludes zsh's `~`,
# backtick and glob forms too. The ONE zsh channel `=` cannot be excluded for - a word BEGINNING with `=`
# expands to an absolute path (`=pytest` -> `/usr/bin/pytest`) - is rejected explicitly after shlex.split,
# since `=` must remain usable to spell `--flag=value` and the `NAME=value` env prefix; see _matches_table.)
#
# FIX ROUND 5 / B1: checked on the WHOLE raw command, BEFORE _strip_env_prefix ever runs (is_test_shaped
# below), not just on the text left over after stripping an env-var prefix. Round 4 checked it only on that
# leftover text, which left a gap: the env-prefix stripper's OWN word-break regex decided where the prefix
# ended using Python `\s`, a far wider class than the literal-space-only separator this charset models (see
# the delta review, B1) - a prefix built with e.g. a vertical tab, NEL or NBSP between the assignment and
# the next word was accepted as a "boundary" by `\s`, the "program" word got swallowed into the env VALUE
# (which this charset's check never saw, since it ran on the post-strip remainder), and the REAL first
# word bash would actually run (e.g. `git`) became the hook's positional, sailing through confinement
# checks meant for pytest/go-test node-ids. Checking this charset against the ENTIRE raw text up front
# closes that by construction: none of those exotic whitespace bytes are IN this charset, so a command
# built to exploit the gap simply fails here outright, before any env-prefix logic ever runs, and gets no
# answer at all.
_RAW_CHARSET_RE = re.compile(r"^[A-Za-z0-9_./:=,+@-]+(?: [A-Za-z0-9_./:=,+@-]+)*$")


# ---- env-var prefix: decided on the RAW token text, never a quote-stripped one -----------------------
# bash only recognises `NAME=value` as a leading assignment word when `NAME=` appears as LITERAL, unquoted,
# unescaped text at the start of that word; a quote or backslash ANYWHERE in the word means bash parses the
# whole thing as the COMMAND itself, not an assignment prefix (`"FOO=bar" pytest` runs a program literally
# named `FOO=bar`, it does not set FOO). shlex.split() already strips quoting before a regex ever sees the
# token, so matching on its OUTPUT cannot tell a real assignment from a quoted/escaped look-alike - this
# scans the RAW string instead, word by word, before any shlex processing happens.
# Only a small, fixed allowlist of INERT names may be pre-allowed this way (never widened ad hoc): these
# change terminal/bytecode behaviour only, never what runs or which flags a runner sees. Anything else -
# including flag-injecting vars like PYTEST_ADDOPTS, loader/search-path vars (PATH, PYTHONPATH, LD_*,
# NODE_PATH, GEM_PATH, MAKEFILES, npm_config_*) - is NOT inert and must never be added here.
# FIX ROUND 5 / B1: the separator between the assignment word and whatever follows is a literal single
# ASCII space ONLY - never `\s`/`str.split()` semantics (those are wider than bash's own blank-splitting,
# which is exactly what let the B1 bypass through in round 4; see the _RAW_CHARSET_RE comment above). The
# VALUE group is now also restricted to the SAME conservative charset as the rest of the command (not
# `\S*`, which matches anything-but-whitespace, including every byte this hook is supposed to be excluding)
# - redundant with the whole-command charset check in is_test_shaped below, but this function is the one
# actually deciding where the env prefix ends, so it draws that line itself rather than trusting a caller.
_INERT_ENV_NAMES = frozenset({"CI", "NO_COLOR", "TERM", "PYTHONDONTWRITEBYTECODE"})
_RAW_ENV_WORD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=([A-Za-z0-9_./:=,+@-]*)( |$)")


def _strip_env_prefix(raw):
    """Consume leading `NAME=value` assignment words straight off the RAW command text (not a shlex token).
    A word is accepted ONLY when: its raw text carries no quote (`'`/`"`) or backslash anywhere (those mean
    bash would not parse it as an assignment at all); its value carries no `$`/backtick (no expansion
    smuggled in - both checks are now also structurally impossible, since _RAW_ENV_WORD_RE's value group is
    itself charset-restricted, but kept here as belt-and-suspenders for a caller that hands this function
    text which never passed that whole-command charset check); and its NAME is in the fixed inert allowlist
    above. The word boundary itself is a literal single space (see _RAW_ENV_WORD_RE above - never `\\s`).
    Returns the remaining raw text (the program + its args), unconsumed, the moment a word fails any of
    that."""
    rest = raw
    while True:
        m = _RAW_ENV_WORD_RE.match(rest)
        if not m:
            return rest
        word = rest[:m.end()].strip()
        if any(c in word for c in "'\"\\"):
            return rest
        name, value = m.group(1), m.group(2)
        if name not in _INERT_ENV_NAMES or "$" in value or "`" in value:
            return rest
        rest = rest[m.end():]


# ---- realpath confinement, on top of the textual check (round 5 / B2, rounds 6-8) -------------------
# An earlier review round's B2 item asked for positionals that are "realpath-confined to the worktree", not just textually
# confined: a symlink that LIVES inside the worktree (committed, or planted by an earlier judged `ln -s`)
# but RESOLVES outside it defeats a purely textual "no /, ~, .. component" check - `pytest symlink-name`
# looks perfectly worktree-relative while loading an arbitrary tree's conftest.py/ini and writing its
# __pycache__ there (verified in the delta review with `wt/link -> outside/`). This resolves the token with
# os.path.realpath and requires the result to stay inside (or be exactly) the realpath of the worktree root.
#
# FIX ROUND 6 (opus delta review of !210, blocking B2): round 5 short-circuited `return True` the moment
# `os.path.exists(candidate)` was False, reasoning "a token that does not exist has nothing to resolve".
# That fails OPEN for a path that only LOOKS nonexistent because of a wildcard/selector suffix: go's `...`
# wildcard never exists on disk (`go test link/...`, `go test ./link/...` - `link` a symlink inside the
# worktree to an outside dir) and a not-yet-created pytest file under a symlinked dir (`pytest link/x.py`)
# both hit that `return True`, so the run was allowed and go/pytest then WALKED THE OUTSIDE TREE. The fix:
# drop the selector/wildcard components (`...`, and the empty/"." components that a `./x/...` split leaves)
# so the token reduces to the real path prefix it names, then resolve THAT up to its DEEPEST EXISTING
# ANCESTOR with realpath (the first component of the path that actually exists - `link`, here - which is
# exactly where a symlink escape is still visible), and require that ancestor's realpath to be inside the
# boundary. A fully nonexistent path whose deepest existing ancestor is a real in-tree directory (e.g.
# `pytest tests/does_not_exist_yet.py`, `go test ./pkg/...`) still resolves to that directory and stays
# allowed; a pytest node-id (`path::test`) resolves on the part before "::" only, as before.
#
# FIX ROUND 8 (ops Full review of round 7 = BLOCK, F5 regression): rounds 6-7 resolved the token against
# the worktree ROOT, but the command actually runs in the worker's PERSISTENT Bash cwd, which a `cd` moves
# (`cd sub; pytest link` loads `sub/link`, not `<root>/link`). Confining against the root alone therefore
# failed OPEN: `cd sub; pytest link` with `wt/sub/link -> /outside` was allowed. The two directories are now
# separated: `root` (the broker's worktree root, on the hook's argv) is the realpath BOUNDARY, while the
# PreToolUse payload's `cwd` is the RESOLUTION BASE - the directory a relative token is actually resolved
# against. The payload cwd is trusted ONLY once its own realpath is verified inside the boundary, so a
# `cd /outside` + `pytest .` (or any positional) does not widen the reach; then the token is resolved
# against that cwd up to its deepest existing ancestor and the result must stay inside the SAME boundary.
#
# FIX ROUND 6 (item 3) / ROUND 7 (F5) / ROUND 8: a MISSING anchor OR a MISSING/out-of-boundary payload cwd
# fails CLOSED for any path-like positional rather than skipping the realpath half - there is nothing to
# confine a path against, or nothing trustworthy to resolve it from, and silently allowing a path-like
# positional there was the same fail-open shape in a different guise. Commands whose row has NO path-like
# positional (bare pytest/tox/cargo test/nox, a runner flag with no path value, ...) never reach this
# function, so they are unaffected. Never raises: a path that cannot be resolved (permission error, loop,
# ...) fails CLOSED (not confined), the same safe direction every other check in this file takes.
class _Confine:
    """The two directories a path-like positional is judged against. `root` is the broker's worktree root -
    the realpath BOUNDARY, threaded in on this hook's argv. `cwd` is the PreToolUse payload's `cwd` - the
    RESOLUTION BASE, the worker's own persistent shell cwd, where a relative token is actually resolved.
    Either may be None (no boundary on argv, or a payload without a `cwd`): that fails CLOSED for any
    path-like positional - there is nothing to confine against, or nothing trustworthy to resolve from."""
    __slots__ = ("root", "cwd")

    def __init__(self, root, cwd):
        self.root = root
        self.cwd = cwd


def _within(path, root):
    return path == root or path.startswith(root + os.sep)


def _realpath_confined(tok, confine):
    if confine is None or not confine.root or not confine.cwd:
        return False
    fs_part = tok.split("::", 1)[0]          # pytest node-id: only the part before "::" is a filesystem path
    # Drop the selector/wildcard components that never name a real directory on their own: go's "..." walks
    # the package tree (never a path that exists), and a leading "./" or a trailing "/" leaves empty/"."
    # parts. What remains is the longest real, on-disk path prefix the token names, which is where a
    # symlink escape (link -> outside/) is still observable via realpath.
    parts = [p for p in fs_part.split("/") if p not in ("", ".", "...")]
    try:
        real_root = os.path.realpath(confine.root)
        # The resolution base is trusted only once its OWN realpath is inside the boundary: a payload cwd
        # that a `cd` moved outside the worktree (or that is itself a symlink out) must not become a base
        # that can resolve a relative token anywhere.
        real_cwd = os.path.realpath(confine.cwd)
        if not _within(real_cwd, real_root):
            return False
        # Round 8 / F5: resolve against the payload cwd (the execution dir), NOT the root - the runner
        # resolves `link` relative to where it runs.
        candidate = os.path.join(confine.cwd, *parts) if parts else confine.cwd
        # Resolve up to the deepest EXISTING ancestor: os.path.realpath on the full candidate would resolve
        # a nonexistent tail lexically without ever touching the symlinked component we need to catch, so
        # walk up to the first ancestor that exists (always returns a real path for that ancestor).
        probe = candidate
        while not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:              # reached the filesystem root without finding an ancestor
                break
            probe = parent
        real_candidate = os.path.realpath(probe)
    except OSError:
        return False
    return _within(real_candidate, real_root)


# ---- the repo-relative test-script path check (sh/bash <path>) -------------------------------------
# Repo-relative ONLY (no leading "/" or "~", no ".."/"."/"" component anywhere in the path - a textual
# normalize-and-reject check first), under tests/ or scripts/, filename ending in test*.sh or *_test.sh,
# THEN realpath-confined to the worktree (round 5 / B2 - see _realpath_confined above: a symlink under
# tests/ can still point outside the worktree even though its own path text looks repo-relative).
_TEST_SCRIPT_NAME_RE = re.compile(r"^(test.*\.sh|.*_test\.sh)$")


def _is_repo_test_script(path, confine):
    if not isinstance(path, str) or not path or path.startswith(("-", "/", "~")):
        return False
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if len(parts) < 2 or parts[0] not in ("tests", "scripts"):
        return False
    if not _TEST_SCRIPT_NAME_RE.match(parts[-1]):
        return False
    return _realpath_confined(path, confine)


# ---- generic positional-path confinement for pytest / python -m / go test (round 4 / B4, round 5 / B2) --
# Same idea as _is_repo_test_script above (textual normalize-and-reject FIRST), but for a bare RUNNER
# positional (a file/dir/node-id for pytest, a package pattern for go test) rather than a `sh`/`bash`
# script argument: these positionals are not confined to a `tests/`/`scripts/` subtree or a fixed filename
# shape, just to the worktree itself. A leading "." (plain relative) or go test's "..." wildcard component
# is fine - only an absolute path, a `~`-leading path, or an exact ".." component walks out of the worktree
# textually (and, for pytest, into an arbitrary tree's own conftest.py/ini; for go test, into an arbitrary
# tree's own go.mod/package); THEN realpath-confined (round 5 / B2 - a symlink inside the worktree can
# still resolve outside it even when its own path text has none of those).
def _is_worktree_confined_path(tok, confine, _i):
    if not tok or tok.startswith(("/", "~")):
        return False
    if any(part == ".." for part in tok.split("/")):
        return False
    return _realpath_confined(tok, confine)


# go test's positional is a PACKAGE IMPORT PATH, not a filesystem path (fix round 7 / F1): `go test ./pkg`,
# `go test .`, `go test ./...`. A BARE relative import path (`go test m/link`) or a meta-package (`all`,
# `std`, `cmd`, `net/http`) is resolved by go through the module's own go.mod import space - a worker-writable
# go.mod can alias a module name to a directory OUTSIDE the worktree (`module m` + `replace`/a symlinked dir),
# so the import path names an arbitrary tree whose path text is not `..`/`/`/`~` at all - the plain
# _is_worktree_confined_path check above cannot see it. Only a `.`/`./`-prefixed RELATIVE FILESYSTEM path is
# actually confined by the path text, so require exactly that first, then the same realpath check the pytest
# row uses (a `./link/...` whose `link` is a symlink to an outside dir still resolves outside and fails).
def _is_go_import_path(tok, confine, _i):
    if tok != "." and not tok.startswith("./"):
        return False
    return _is_worktree_confined_path(tok, confine, _i)


# ---- tox / nox / unittest positional policy (round 4 / B4, round 5 non-blocking item 3) ----------------
# tox: the ONLY bare positional ever accepted is the explicit "run" subcommand, and only as the very FIRST
# tail token (`_i == 0`) - the default-run form (no subcommand token at all) and "run"/"-e ENV" are the
# forms this row is meant to cover; every OTHER tox 4 subcommand (devenv, quickstart, exec, legacy, list,
# ...) builds/writes at an arbitrary path or runs arbitrary commands, so it must fall through to the judge
# instead. Pinning "run" to position 0 (round 5, non-blocking note N2 from the delta review) is tidier, not
# a close call either way: tox itself only ever takes "run" as its leading subcommand, so a LATER "run"-
# shaped token (`tox -e py39 run`, `tox run run`) adds nothing a judge should not instead see.
def _is_tox_run_positional(tok, confine, _i):
    return _i == 0 and tok == "run"


# nox: no bare positional is ever accepted at all - session selection only through the -s/--sessions flag,
# whose own value is already confined to at most one non-dash token (see _tail_allowed's value-flag check),
# never a second free-standing value.
def _no_bare_positionals(tok, confine, _i):
    return False


# unittest (round 5, non-blocking item 3 / N3 from the delta review): its positional is a DOTTED MODULE or
# TEST name (`pkg.mod.TestCase.test_x`), never a filesystem path, so _is_worktree_confined_path's "no /, ~,
# .." check is meaningless against it - a dotted name has no such components by construction, yet merely
# IMPORTING an arbitrary top-level module by name can have side effects (the review's PoC:
# `python -m unittest venv.__main__ tests` imports stdlib `venv/__main__.py`, unguarded, creating venvs on
# import). So this row accepts NO bare module/test positional at all - only its own literal "discover"
# subcommand, and only as the very first tail token; discover's own further options (-s/-p/-t) are flags,
# which this row's allowed-flags dict (_PYTEST_ALLOWED, reused for -q/-v/-k) does not include, so they fall
# straight through to the judge regardless of this predicate.
def _is_unittest_positional_ok(tok, confine, _i):
    return _i == 0 and tok == "discover"


# ---- the fixed test-shaped table (data, not code - add a row to extend) ----------------------------
# Each row: (program basenames, fixed args after the program, exact, allowed-flags, positional_ok).
#   - a fixed-arg element is a literal string, a frozenset of literal alternatives, or a callable
#     predicate (tok, confine) -> bool, checked positionally against argv right after the program name;
#   - exact=True means argv must have EXACTLY the fixed args and nothing after them; exact=False means any
#     number of further args ("[args]") are accepted after the fixed ones, subject to allowed-flags below;
#   - allowed-flags is a dict {flag_name: takes_value} - a closed ALLOWLIST of that runner's plain
#     selection/verbosity/output flags (e.g. `-q`/`-v`/`-x`/`-k EXPR`, `-run`, `-count`, `--lib`, `-e ENV`).
#     Every further arg (beyond the fixed ones) that STARTS WITH `-` must be exactly one of these flags (an
#     attached `--flag=value` form counts only when `--flag` is on the list AND takes_value) or the row does
#     not match at all; a value-taking flag's value must not itself start with `-`. This is an ALLOWLIST,
#     not a blocklist, on purpose: a blocklist of specific open-ended flags (ones that load/execute other
#     code or move the run root) keeps missing newly-discovered ones, so instead this enumerates the small
#     set of flags known SAFE and lets the judge handle everything else, case by case;
#   - positional_ok is None (any other bare positional passes through unchecked, e.g. cargo test's plain
#     test-name filter) or a three-arg predicate (tok, confine, i) -> bool that every OTHER bare positional
#     (beyond the fixed ones) must satisfy - `i` is that positional's index into the tail (so a predicate
#     can require "only as the first tail token", e.g. tox's "run"/unittest's "discover") and `confine` is
#     the _Confine (worktree-root boundary + payload-cwd resolution base), threaded through for realpath
#     confinement - see _is_worktree_confined_path / _is_tox_run_positional / _no_bare_positionals /
#     _is_unittest_positional_ok above. A bare positional starting with `@` (a pytest/fromfile-style argfile)
#     never passes, for any row, regardless of positional_ok - see _tail_allowed below.
def _is_npm_test_script(tok, _confine):
    return tok == "test" or tok.startswith("test:")


# pytest: plain selection/verbosity/output only. Notably absent (and therefore judge-only): `-p` (loads an
# arbitrary plugin module), `-o`/`--override-ini`, `--rootdir`, `-c`/`--config-file`, `--confcutdir`,
# `--import-mode` (all move the run root, point at an arbitrary alternate config, or change how test
# modules are imported).
_PYTEST_ALLOWED = {"-q": False, "-v": False, "-x": False, "-k": True}
# go test: `-run <regexp>` selects which tests run, `-count <n>` repeats them - both plain selection, no
# code/path implications. Notably absent: `-exec`/`-toolexec <wrapper>` (run the test binary through an
# arbitrary wrapper program), `-overlay`/`-modfile`/`-C` (arbitrary alternate build inputs or run root).
_GO_ALLOWED = {"-run": True, "-count": True}
# cargo test: `--lib`/`--all` just select which targets run. Notably absent: `--config` (arbitrary inline
# cargo config, including a `target.*.runner` exec-wrapper equivalent), `--manifest-path`/`-C` (arbitrary
# alternate Cargo.toml / run root).
_CARGO_ALLOWED = {"--lib": False, "--all": False}
# tox: `-e <envlist>` selects which environments run. Notably absent: `-c`/`--conf` (an arbitrary alternate
# ini, whose `[testenv] commands` is arbitrary code), `-x` (open-ended override).
_TOX_ALLOWED = {"-e": True}
# nox: `-s <sessions>` selects which sessions run. Notably absent: `-f`/`--file`/`--noxfile` (an arbitrary
# alternate noxfile.py - arbitrary Python, executed).
_NOX_ALLOWED = {"-s": True}

_TEST_SHAPED_TABLE = (
    (frozenset({"pytest"}), (), False, _PYTEST_ALLOWED, _is_worktree_confined_path),
    (frozenset({"python", "python3"}), ("-m", "pytest"), False, _PYTEST_ALLOWED, _is_worktree_confined_path),
    # unittest is its OWN row (round 5, non-blocking item 3 / N3): its positional is a dotted module/test
    # name, not a path, so it gets _is_unittest_positional_ok instead of _is_worktree_confined_path - see
    # that predicate's comment above.
    (frozenset({"python", "python3"}), ("-m", "unittest"), False, _PYTEST_ALLOWED, _is_unittest_positional_ok),
    (frozenset({"npm", "pnpm", "yarn"}), ("test",), True, {}, None),
    (frozenset({"npm", "pnpm", "yarn"}), ("run", _is_npm_test_script), True, {}, None),
    (frozenset({"go"}), ("test",), False, _GO_ALLOWED, _is_go_import_path),
    (frozenset({"cargo"}), ("test",), False, _CARGO_ALLOWED, None),
    (frozenset({"make"}), (frozenset({"test", "check"}),), True, {}, None),
    (frozenset({"tox"}), (), False, _TOX_ALLOWED, _is_tox_run_positional),
    (frozenset({"nox"}), (), False, _NOX_ALLOWED, _no_bare_positionals),
    (frozenset({"sh", "bash"}), (_is_repo_test_script,), True, {}, None),
)


def _tok_matches(tok, spec, confine):
    if callable(spec):
        return spec(tok, confine)
    if isinstance(spec, frozenset):
        return tok in spec
    return tok == spec


def _tail_allowed(tail, allowed, positional_ok, confine):
    """Sequentially walk the args AFTER a row's fixed prefix.
    A positional (no leading `-`) starting with `@` (a pytest/fromfile-style argfile that reads arbitrary
    flags from a file the worker can Write) always fails the whole row, for every runner. Any OTHER
    positional passes only when `positional_ok` is None (no further restriction for this row) or returns
    True for `(tok, confine, i)` - `i` is this positional's own index into `tail` (so a predicate can require
    "first tail token only", e.g. tox's "run"/unittest's "discover" - round 5) and `confine` threads the
    realpath-confinement boundary + resolution base through (round 5 / B2, round 8 / F5) - see
    _is_worktree_confined_path / _is_tox_run_positional / _no_bare_positionals / _is_unittest_positional_ok
    above.
    A `-`-leading token must be exactly one of `allowed`'s flags (or, attached as `--flag=value`, have its
    `--flag` part on `allowed` with takes_value True) - anything else fails the whole row. A value-taking
    flag given in the separate-token form consumes the next token as its value; that value must not itself
    start with `-` (a variable-length flag like nox's `-s`/`--sessions` gets at most ONE non-dash value
    this way - argparse's nargs='*' would otherwise give it zero values and let the next token slip through
    as a different flag) and must exist at all (a flag with a missing value is not a confidently-safe
    shape) - either failure fails the whole row."""
    i, n = 0, len(tail)
    while i < n:
        tok = tail[i]
        if not tok.startswith("-"):
            if tok.startswith("@") or (positional_ok is not None and not positional_ok(tok, confine, i)):
                return False
            i += 1
            continue
        name, is_attached, _value = tok.partition("=")
        if name not in allowed or (is_attached and not allowed[name]):
            return False
        if is_attached:
            i += 1
            continue
        if allowed[name]:
            if i + 1 >= n or tail[i + 1].startswith("-"):
                return False
            i += 2
        else:
            i += 1
    return True


def _matches_table(argv, confine):
    if not argv:
        return False
    # F2 (fix round 7): the worker's shell is zsh (SHELL=/bin/zsh), where a word that BEGINS with `=` is
    # expanded to the absolute path of the named command (`pytest =pytest` runs `pytest /usr/bin/pytest` via
    # zsh's `=cmd` filename expansion - and `=~`-style forms similarly). `=` has to stay in _RAW_CHARSET_RE so
    # `--flag=value` and the `NAME=value` env prefix can be spelled at all, so it cannot be excluded there;
    # reject any token that STARTS with `=` here instead. This is checked on the WHOLE argv (program included)
    # before any row is consulted, so no row's own predicate can be the thing that first sees such a word.
    if any(tok.startswith("=") for tok in argv):
        return False
    # The program must be a BARE table name, no path separator at all: argv[0] containing "/" (a repo
    # .venv runner, a relative wrapper script, an absolute binary, ...) is never auto-allowed here - the
    # judge decides those. (Previously this basename()'d argv[0] first, which wrongly let a path THROUGH to
    # the bare-name check below.)
    prog = argv[0]
    if "/" in prog:
        return False
    rest = argv[1:]
    for progs, fixed, exact, allowed, positional_ok in _TEST_SHAPED_TABLE:
        if prog not in progs:
            continue
        if exact and len(rest) != len(fixed):
            continue
        if not exact and len(rest) < len(fixed):
            continue
        if not all(_tok_matches(tok, spec, confine) for tok, spec in zip(rest, fixed)):
            continue
        if not _tail_allowed(rest[len(fixed):], allowed, positional_ok, confine):
            continue
        return True
    return False


def is_test_shaped(cmd, worktree_root=None, cwd=None):
    """True iff `cmd` (the Bash tool's raw command string) is a single simple command whose argv (after
    stripping a plain inert env-var prefix) matches _TEST_SHAPED_TABLE. Two directories are threaded in for
    a path-like positional (see _Confine above): `worktree_root` is the REALPATH-confinement BOUNDARY - the
    broker's own worktree root, from this hook's argv (never the worker-settable payload cwd; fix round 7 /
    F5) - and `cwd` is the RESOLUTION BASE - the PreToolUse payload's `cwd`, the worker's persistent Bash
    cwd a relative token is actually resolved against (fix round 8 / F5). A missing/falsy boundary OR a
    missing/falsy resolution base fails CLOSED for a path-like positional (fix round 6 / item 3, round 8 -
    nothing to confine against, or nothing trustworthy to resolve from), while a command with no path-like
    positional is unaffected; textual confinement still applies either way. Never raises. Does NOT check the
    deny-floor - see _floor_denies/decide for that."""
    if not isinstance(cmd, str) or not _is_single_simple_command(cmd):
        return False
    # B1 (fix round 5): the strict raw-character allowlist is checked on the WHOLE raw command, BEFORE any
    # env-prefix stripping - see the _RAW_CHARSET_RE comment above for why round 4's "check it on the
    # post-strip remainder only" order left a gap. `.strip(" ")`, not a bare `.strip()`: the latter also
    # eats non-space whitespace (tab, NEL, ...) off either end, which would silently hide exactly the kind
    # of byte this charset exists to catch instead of letting the charset check see and reject it.
    stripped = cmd.strip(" ")
    if not _RAW_CHARSET_RE.match(stripped):
        return False
    rest = _strip_env_prefix(stripped)
    try:
        tokens = shlex.split(rest, posix=True)
    except ValueError:
        return False
    return _matches_table(tokens, _Confine(worktree_root, cwd))


# ---- the deny-floor check: this hook's own, not just an assumption -----------------------------------
# A Claude Code `Bash(<pattern>)` deny-floor entry matches the WHOLE raw command text, `*` (including the
# conventional trailing `:*`) standing for arbitrary text at ANY position (empirically verified; see
# adapters/cc.py's _WRITE_DENIED comments). Translated conservatively here: `:*` is normalised to a plain
# `*` first (so `Bash(git push:*)` becomes the prefix-wildcard "git push" + anything), then every remaining
# `*` is a `.*` wildcard over the full command text. Over-matching (treating MORE commands as floor-denied
# than Claude's own parser would) only costs this hook its fast path on that command - it never allows
# anything; under-matching is the only direction that would matter, so this errs wide on purpose.
def _floor_pattern_regex(inner):
    parts = inner.replace(":*", "*").split("*")
    return re.compile("^" + ".*".join(re.escape(p) for p in parts) + "$", re.DOTALL)


def _floor_denies(cmd, floor_patterns):
    """True iff `cmd`'s raw text, OR `cmd` with a leading inert env-var prefix stripped (see
    _strip_env_prefix), matches ANY `Bash(...)` entry in `floor_patterns` (non-Bash entries, e.g.
    `Edit(...)`/`WebFetch`, don't apply to a Bash command and are ignored). The stripped form is checked
    too so a floor entry written against the bare runner (e.g. `Bash(pytest:*)`) still catches `CI=1
    pytest` - a command that is test-shaped (is_test_shaped tolerates that same inert prefix) but whose
    raw text no longer starts with `pytest`."""
    if not isinstance(floor_patterns, list):
        return False
    candidates = (cmd, _strip_env_prefix(cmd.strip())) if isinstance(cmd, str) else (cmd,)
    for entry in floor_patterns:
        if not (isinstance(entry, str) and entry.startswith("Bash(") and entry.endswith(")")):
            continue
        pattern = _floor_pattern_regex(entry[len("Bash("):-1])
        if any(pattern.match(c) for c in candidates):
            return True
    return False


_ALLOW = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": (
            "code test-shaped allow rule: a single simple command matching the fixed test-runner table "
            "(pytest / python|python3 -m pytest|unittest / npm|pnpm|yarn test(:*) / go test / cargo test / "
            "make test|check / tox / nox / a repo-relative tests|scripts test script), with no open-ended "
            "runner flag and no deny-floor match, so no judge round-trip is needed."
        ),
    }
}


def decide(raw, floor_patterns=None, worktree_root=None):
    """None = no answer (fall through to the deny-floor/classifier as today); else the allow payload.
    `floor_patterns` is the code row's deny-floor (`Bash(...)` entries, from adapters/cc.py's _CODE_DENIED) -
    a command that is otherwise test-shaped but ALSO matches one of them gets no answer either: the floor
    wins, checked here rather than merely assumed (see the module docstring).

    `worktree_root` is the REALPATH-CONFINEMENT BOUNDARY: the broker's own worktree root, handed to this
    hook on its argv (see adapters/cc.py's _code_judged_hook_settings, which threads worker.worktree
    through) - NOT the PreToolUse payload's `cwd` field (fix round 7 / F5).

    The payload IS still read for its `cwd`, but only as the RESOLUTION BASE for a relative positional, and
    only once that base's own realpath has been verified inside the boundary (fix round 8 / F5). The two
    roles are separate on purpose: the runner resolves a relative token against the directory it runs in -
    the worker's persistent Bash cwd, which a `cd` moves - but the tree that cwd (and the resolved token) is
    allowed to live in is the broker-supplied root, so the confinement stays independent of anything the
    worker sets at runtime. A missing/falsy boundary, or a payload without a `cwd`, or a `cwd` whose realpath
    falls outside the boundary, fails CLOSED for any path-like positional (fix round 6 / item 3, round 8);
    a command with no path-like positional is unaffected. The payload is read ONLY for the command text, the
    cwd, and the floor patterns."""
    try:
        payload = json.loads(raw)
        cmd = (payload.get("tool_input") or {}).get("command")
        cwd = payload.get("cwd")
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(worktree_root, str) or not worktree_root:
        worktree_root = None
    if not isinstance(cwd, str) or not cwd:
        cwd = None
    if not is_test_shaped(cmd, worktree_root, cwd):
        return None
    if _floor_denies(cmd, floor_patterns or []):
        return None
    return _ALLOW


def main(argv):
    raw = sys.stdin.read()
    floor_patterns = []
    worktree_root = None
    if argv:
        try:
            floor_patterns = json.loads(argv[0])
        except (ValueError, TypeError):
            floor_patterns = []
    if len(argv) > 1:
        worktree_root = argv[1]           # fix round 7 / F5: the broker's worktree root, never the payload cwd
    try:
        out = decide(raw, floor_patterns, worktree_root)
    except Exception:
        out = None
    if out:
        print(json.dumps(out))
    # else: no stdout, exit 0 -> no answer, falls through to the deny-floor/classifier.


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        pass
