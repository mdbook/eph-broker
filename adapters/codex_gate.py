#!/usr/bin/env python3
"""eph-gate for the Codex CLI: the PreToolUse hook a codex worker runs every tool call through.

adapters/codex.py writes, per turn, a private CODEX_HOME with a hooks.json whose ONE PreToolUse entry (matcher "*")
runs this file as
    <broker python> -I <this file> <CODEX_HOME>/eph-gate-policy.json
with the Codex hook event (JSON) on stdin. The policy is the SAME per-turn eph-gate policy a hermes worker gets
(adapters/hermes.gate_policy, derived from cc.capability_tool_grant + cc._SECRET_FILE_GLOBS, never a copied list),
and every decision is adapters/hermes_gate/gate.py's. This file only translates Codex tool events onto it.

OUTPUT CONTRACT (Codex): a deny is exit 2 with the reason on stderr, the one blocking channel Codex honours whatever
the JSON shape. An allow is exit 0 with no output. This hook NEVER raises: any error is a deny (Codex fails OPEN on
a crashed hook). A hook that never starts, or a hooks file Codex skipped as untrusted, is outside this file: the
harness proves the hook is loaded, enabled and trusted (Codex's own hooks/list) and probes it before every turn.

TOOL MAPPING (anything else -> DENY, kind unmapped-tool):
    Bash /       the command MUST start with the exact literal `cd <abs worktree> || exit 1; ` (see WORKDIR
    exec_command BYPASS FIX below) and must contain no OTHER `cd` anywhere; a `workdir` / `cwd` argument is
                 NEVER honoured (denied outright, whatever it names - see below). Past that prefix, every segment
                 a READ command (READ_COMMANDS: cat head tail wc nl ls grep rg sed, bare names only, so a
                 worker-written ./cat never runs) -> the gate's Read/Grep/Glob rules: the capability must grant
                 the matching CC tool, every argument path stays inside read_root and off the secret floor, rg's
                 program-running options are denied and a grep/rg content search walks its trees like
                 search_files. This is how CC's Read/Grep/Glob tools map onto Codex, which reads through the
                 shell. Otherwise -> gate.decide("terminal") on the part AFTER the prefix (the CC Bash allow/deny
                 lists, the lexer ceiling, the general row's exec-gate judge, read-only capabilities hard-denied);
                 if that part does not even PARSE (H1 below), it is denied outright here instead
                 of reaching decide("terminal") at all - see WORKDIR BYPASS FIX below. `exec_command` is
                 the nested name a code_mode_only model's JS code uses (tools.exec_command(); the code-mode
                 gating fix); it is gated
                 identically to the classic `Bash` tool call, through the SAME function (_decide_exec), so a
                 code_mode_only model gets exactly the coverage a classic tool-calling one does. Either name is
                 ALSO hard-denied outright, in every capability, when the command (past the cd prefix) is a bare
                 stdin-reading interpreter (a shell or script interpreter given no script file / inline-code flag,
                 e.g. plain `bash` or `python3`): Codex never routes a later `write_stdin` to such a session
                 through PreToolUse (LOW-1 below), so the fix is to deny ever opening one, not to
                 try to gate write_stdin itself.
    apply_patch  every file the patch adds, updates, deletes or moves to -> gate.decide("write_file") (edits only
                 under acceptEdits, confined to the worker cwd, no .git, secret floor; general also asks the judge
                 with the patch text as content). The same tool name under code mode, so already covered.
    update_plan  allowed (no side effects).
    write_stdin  denied outright (see LOW-1 above: this hook may never even be asked, so the bare-interpreter
                 deny above is the real floor; denying the tool here too costs nothing and documents the intent).
    the probe    gate.PROBE_TOOL answers "eph-gate probe ok <nonce>" (as a deny), the harness's liveness proof.
A denial is appended to <policy dir>/eph-gate-denials.jsonl (hermes_gate.record_denial) and reported in the result.
NOT applied: the gate's modified_args (Codex `allow` without updatedInput is unsupported and fails open, so this
hook never answers allow; it stays silent). The checked paths are the ones Codex then uses (same-uid symlink-swap
race between check and use, as for hermes).

WORKDIR BYPASS FIX (HIGH, pre-existing on main whatever code_mode_host is): an offline real-binary probe report
    proved Codex 0.164.0 strips `workdir` (also `tty`,
    `shell`, `login`, `sandbox_permissions`) from the `tool_input` this hook sees - the event's `tool_input` is
    just `{"command": cmd}` - but still runs the command in that workdir; the event's top-level `cwd` is always
    the session cwd, never the real one. `exec_command({cmd:"cat hostname", workdir:"/etc"})` passed the gate and
    printed `/etc/hostname`: a RELATIVE path argument was checked against `policy.cwd` (the worktree) while the
    command actually ran somewhere else entirely, so the check proved nothing. Codex's own workdir/cwd fields
    cannot be trusted again (a call carrying either is denied outright, above), so confinement is enforced in the
    command TEXT itself: `_decide_exec` requires the literal prefix `cd <abs worktree> || exit 1; ` (exact
    string, built by `_cd_prefix`) before anything else, and denies any OTHER `cd` in the rest of the command.
    Whatever hidden directory Codex actually starts the shell in, an ABSOLUTE `cd` into the worktree moves it
    there before the rest of the command runs, and with no second `cd` available to leave again, every
    subsequent relative path really is relative to `policy.cwd`, exactly as `_decide_terminal` / `_read_segment`
    already assume. The worker is told the required literal prefix in its signed prompt
    (`worker_system_prompt`, below), appended by `adapters/codex.py` the same way `hermes.worker_system_prompt`
    appends `gate.PATCH_EDIT_RULE`.
    H1 (FAIL on main 0624ceaf): the prefix used to end `&& ` (a list AND-operator, not a
    statement terminator). bash parses `cd WT && A & B` as `(cd WT && A) &` followed by `B`: the backgrounded
    `(cd WT && A)` runs in a FORKED subshell, so its `cd` never touches the parent shell's directory at all, and
    `B` then runs in whatever hidden workdir Codex actually started the shell in - neither this hook nor the judge
    can see that, so the prefix proved nothing for `B`. Fix: end the prefix with `|| exit 1; ` (a statement
    terminator) instead, so the `cd` always runs - and completes - in the PARENT shell before any later `&`/`&&`/
    `;`/`|` is even parsed, whatever follows. Because `split_command` denies a bare `&` outright (Deny, "'&'
    (background...)"), a command using one never reaches the second-cd / bare-interpreter checks below as a
    parsed `segs` list either: `_decide_exec` denies outright the moment `split_command(rest)` raises, rather
    than silently treating an unparseable rest as `segs = []` and falling through to gate.decide("terminal")
    (which, for a judged capability, would ask the judge about the raw text alone) - the gate must not rely on a
    judge to catch a confinement break it cannot see, even when, as here, the fixed prefix already makes the
    break harmless; mechanical denial is the floor, the judge is not.
    N1 (general capability only - a judged capability has no mechanical floor under an
    unrecognised shape): the second-cd check used to require `cd` to be the segment's own argv[0], so `HOME=/etc
    cd`, `pushd`/`popd`, `builtin cd`, `command cd`, `eval cd`, `time cd`, `if cd /etc; then ...` (a compound-
    command keyword keeps its body in the SAME lexer segment) and `shopt -s autocd` all reached the judge instead
    of being denied mechanically (`_changes_dir`, below, strips any leading env-assignment/wrapper word first,
    stacked in any order, then checks cd/pushd/popd/autocd). A follow-up review round
    found the fix above still only covered a CLOSED list of wrapper words with no option-stripping, so `then cd`,
    `! cd`, `command -p cd`, `time -p cd` and `command` with a bare `--` before `cd` all still reached argv[1:] unrecognised - a closed
    list of prefix words will always miss one. `_changes_dir` now strips by CLASS instead of by name: the full
    bash reserved-word list (not just the ones a compound body can wear), every env-assignment word, and known
    wrapper/builtin words together with their OWN option words (and, where a wrapper takes an option argument or
    a mandatory positional before the real command, that argument too) - and treats a segment that cannot be
    reduced to a confident, unwrapped command word as a directory change too (fail-closed: unparseable denies,
    it never falls through to an allow).

CODE MODE RUNTIME (outside this file's reach): code_mode_host lets a code_mode_only model write JS that calls back
into `tools.*` instead of calling a tool directly; this hook sees and gates every one of those calls exactly like
a classic tool call (same event shape, same PreToolUse hook). What it CANNOT see or gate is the JS runtime itself -
whether the sandbox Codex runs that JS in has any ambient reach to the filesystem, network, processes or env
outside `tools.*`. That can only be checked against the real binary (tests/test_codex_canary.py); if it ever shows
such a reach, adapters/codex.py must go back to refusing code_mode_host, not trust this file to catch it.
"""
import json, os, re, shlex, signal, sys, types

_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if _ROOT not in sys.path:                     # run as `python -I <file>`: the broker dir, never the worktree cwd
    sys.path.insert(0, _ROOT)

from adapters.hermes_gate import gate as _gate, record_denial  # noqa: E402

READ_COMMANDS = {"cat": "Read", "head": "Read", "tail": "Read", "wc": "Read", "nl": "Read", "ls": "Glob",
                 "grep": "Grep", "rg": "Grep", "sed": "Read"}
CONTENT_SEARCH = ("grep", "rg")
# rg long options that run a program or decompress through one (`--pre`, `--pre-glob`, `--search-zip`,
# `--hostname-bin`); prefix-matched both ways like gate.GIT_DENY_LONG. Short `-z` (= --search-zip) too.
RG_DENY_LONG = ("pre", "pre-glob", "search-zip", "hostname-bin")
RG_DENY_SHORT = set("z")
# Codex fails OPEN on a hook that outlives its entry's timeout (adapters/codex.HOOK_TIMEOUT_S, test-pinned equal),
# so the hook denies itself DEADLINE_S into the call, before Codex would give up on it.
HOOK_TIMEOUT_S = 30
DEADLINE_S = HOOK_TIMEOUT_S - 5
# `Bash` is the classic tool-calling shape; `exec_command` is the nested name a code_mode_only model's JS uses
# (tools.exec_command(), the code-mode gating fix). Both are gated by _decide_exec, so EXEC_KEYS covers both
# shapes' argument names (command/cmd, workdir/cwd); EXEC_KEYS was named BASH_KEYS before the code-mode gating
# fix. workdir/cwd stay recognised names (so a call carrying one gets _decide_exec's specific "never honoured"
# denial, not a generic "unexpected argument") but are never acted on since the workdir-bypass fix (see WORKDIR
# BYPASS FIX in the module docstring).
EXEC_TOOL_NAMES = frozenset({"Bash", "exec_command"})
EXEC_KEYS = frozenset({"command", "cmd", "workdir", "cwd", "timeout_ms", "timeout", "description", "justification"})
ESCALATION_KEYS = ("with_escalated_permissions", "sandbox_permissions")
ALLOWED_TOOLS = frozenset({"update_plan"})
# write_stdin feeds a running exec session; Codex never routes it through PreToolUse (LOW-1 below), so denying
# it here is belt only - the real floor is adapters/codex.codex_config's
# `unified_exec_tty = false` (the workdir-bypass fix), which makes every exec session's stdin closed; _is_interactive_bare below
# is a second, even older belt, never relied on alone.
DENIED_TOOLS = frozenset({"write_stdin"})
# Shells and script interpreters that drop into an interactive REPL reading stdin when given no script file and
# no inline-code flag. A script/file operand or inline code runs once and exits - fully gated like any other
# command - so only the truly BARE form (nothing but flags, or nothing at all) is denied here.
INTERACTIVE_PROGS = frozenset({"bash", "sh", "dash", "ksh", "mksh", "zsh", "csh", "tcsh", "fish",
                               "python", "python2", "python3", "node", "nodejs", "irb", "ruby", "perl", "php",
                               "lua"})
INLINE_CODE_FLAGS = frozenset({"-c", "-e", "-r", "--command", "--eval"})
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$")
_PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+)$")
# N1 (fail-closed rewrite): the second-cd check used to strip a CLOSED list of specific
# wrapper words (builtin/command/eval/time/if/elif/else/while/until/do) before looking for cd/pushd/popd. Any
# word outside that exact list - a reserved word the list happened to omit (`then`, `fi`, `for`, `in`, `case`,
# `esac`, `select`, `function`, `coproc`, `!`), or an option word glued to a wrapper the list DID cover
# (`command -p cd`, `command` with a bare `--` then `cd`, `time -p cd`) - walked straight past the strip loop
# and reached argv[0] of the REMAINING words unrecognised, exactly like the pre-N1 bug it was meant to close. A closed
# list of prefix words will always miss one; the fix below strips by CLASS instead: every
# bash reserved word, every env-assignment word, and every known wrapper/builtin word together with ITS OWN
# option words (and, where a wrapper takes a separate option argument or a mandatory positional before the real
# command - `timeout N cmd` - that argument too), repeating until nothing more strips. If the walk cannot reduce
# a segment down to a confident, unwrapped command word (nothing left, only flags left, or an arg-taking
# wrapper carries an option this function does not know how many words it consumes), the segment is treated as
# a directory change too: UNPARSEABLE MUST DENY, NEVER ALLOW - the gate has no way to tell such a segment apart
# from one that really does end in `cd`.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# The full bash reserved-word list (POSIX grammar keywords) plus `!` (pipeline negation): none of these is ever
# itself a command, and bash's own grammar keeps whatever follows one in the SAME logical statement - which,
# since split_command only splits on `; \n && || |` and knows nothing of shell grammar, usually means the SAME
# lexer segment too (e.g. "then cd /etc", "do cd /etc", "fi cd /etc" with no separator between the keyword and
# the next command, "! cd /etc").
_BASH_RESERVED_WORDS = frozenset({
    "if", "then", "elif", "else", "fi", "do", "done", "while", "until", "for", "in",
    "case", "esac", "select", "function", "time", "coproc", "!",
})
# Wrapper/builtin words whose OWN options are flag-only (no option ever consumes a separate argument word), so
# any number of their "-"-prefixed option words (including the bare "--" end-of-options marker) can be stripped
# blindly: `command -p cd`, `command` with a bare `--` then `cd`, `time -p cd`, `builtin cd`, `eval cd`.
_CD_WRAPPER_FLAG_ONLY = frozenset({"builtin", "command", "eval", "time"})
# Wrapper/external commands whose options MAY consume a separate argument word (env -u NAME, nice -n ADJ,
# timeout -s SIG, stdbuf -oMODE, sudo -u USER, exec -a NAME, ...). This function does not model each one's
# exact getopt shape (a wrong guess there would be a silent miss, not a safe denial) - a confident reduction
# needs the wrapper to carry NO option words at all. `timeout` additionally takes one mandatory POSITIONAL
# (the duration) before the real command even with no options, which is not an option word and must still be
# consumed - `timeout 5 cd /etc` must deny exactly like `timeout -s TERM 5 cd /etc` (the latter is denied for
# carrying an option this function does not try to parse, per the rule above).
_CD_WRAPPER_ARG_TAKING = frozenset({"exec", "nohup", "env", "nice", "timeout", "stdbuf", "sudo"})
# Process-launcher words that, like _CD_WRAPPER_ARG_TAKING, routinely exec a trailing word as the real command -
# but whose own option grammar is NOT modelled here (unlike that set, carrying no option word is not enough to
# call them confidently reduced: ionice/chrt/taskset/setsid/xargs all take positionals or option-arguments this
# function does not try to parse). Rather than guess and risk a silent miss, any of these is cheaply fail-closed
# instead: a bare cd/pushd/popd token ANYWHERE later in the same simple command denies outright, whatever else
# surrounds it.
_CD_WRAPPER_UNKNOWN_SHAPE = frozenset({"setsid", "xargs", "ionice", "chrt", "taskset"})
# Commands that change the shell's working directory outright - `cd` reached via the stripping loop below, or
# one of these two (pushd/popd are never "wrapped": they ARE the directory-changing command, argv[0] itself).
_CD_COMMANDS = frozenset({"cd", "pushd", "popd"})


def _changes_dir(argv):
    """True if this ONE lexer segment changes the shell's working directory - `cd`/`pushd`/`popd` (plain,
    env-prefixed, reserved-word/wrapper-prefixed in any stacked combination, or via `shopt -s autocd`) - OR the
    segment cannot be reduced to a confident, unwrapped command word at all (fail-closed: an unrecognised or
    ambiguous prefix shape is treated as a directory change, never silently let through as one that is not)."""
    words = list(argv)
    while words:
        w = words[0]
        if _ENV_ASSIGN_RE.match(w):
            words.pop(0)
            continue
        base = os.path.basename(w)
        if base in _BASH_RESERVED_WORDS:
            words.pop(0)
            continue
        if base in _CD_WRAPPER_FLAG_ONLY:
            words.pop(0)
            while words and words[0].startswith("-"):
                words.pop(0)
            continue
        if base in _CD_WRAPPER_ARG_TAKING:
            words.pop(0)
            if words and words[0].startswith("-"):
                return True        # an option on an arg-taking wrapper: not confidently reducible - deny
            if base == "timeout":
                if not words:
                    return True    # "timeout" with nothing after it at all: unreducible - deny
                words.pop(0)       # the mandatory DURATION positional - never itself the command
            continue
        if base in _CD_WRAPPER_UNKNOWN_SHAPE:
            return any(os.path.basename(a) in _CD_COMMANDS for a in words[1:])
        break
    if not words or words[0].startswith("-"):
        return True                # stripped down to nothing, or only flags remain: unreducible - deny
    prog = os.path.basename(words[0])
    if prog in _CD_COMMANDS:
        return True
    return prog == "shopt" and "autocd" in words[1:]


def _is_interactive_bare(argv):
    """True if argv starts an INTERACTIVE_PROGS interpreter with no inline-code flag and no non-flag (script /
    file / '-') operand: it would sit reading stdin as an open-ended session rather than running once and
    exiting - exactly what a later write_stdin could then feed un-gated (LOW-1). BELT ONLY since the workdir-bypass
    fix: the real
    floor against a live write_stdin session is adapters/codex.codex_config's `unified_exec_tty = false`, which
    Codex itself enforces (every exec_command session gets closed stdin, tty:true is rejected outright) - this
    heuristic is kept as defence in depth, never relied on alone."""
    if not argv or os.path.basename(argv[0]) not in INTERACTIVE_PROGS:
        return False
    for a in argv[1:]:
        if a in INLINE_CODE_FLAGS or not a.startswith("-"):
            return False
    return True


def _cd_prefix(cwd):
    """The EXACT literal every Bash / exec_command command must start with (the workdir-bypass fix, HIGH): see
    the module docstring's WORKDIR BYPASS FIX section. `cwd` is always policy.cwd (the worktree, absolute,
    already realpath'd by Policy._abs).

    Ends with `|| exit 1; ` (a statement terminator), NOT `&& ` (H1 above): `cd WT && A & B`
    backgrounds `(cd WT && A)` into a forked subshell, so the `cd` never reaches the parent shell and `B` then
    runs in whatever hidden workdir Codex actually started the shell in. `cd WT || exit 1; ...` always runs the
    `cd` to completion in the PARENT shell first, whatever list/background operator follows it."""
    return "cd %s || exit 1; " % shlex.quote(cwd)


CD_PREFIX_RULE = (
    "\n\n## eph-gate command rule (codex harness, workdir-bypass fix)\n"
    "Codex hides `workdir` (and `tty`, `shell`, `login`, `sandbox_permissions`) from what this gate sees on every "
    "`Bash` / `exec_command` call, but still honours them when it actually runs the command - so the gate can "
    "never trust its own idea of where a command executes from a `workdir` argument alone. Every `Bash` / "
    "`exec_command` call you make MUST therefore start with exactly:\n"
    "    {prefix}\n"
    "immediately followed by the rest of the command on the same line, and must not contain any OTHER `cd` "
    "anywhere in it. A call missing this exact prefix, or containing a second `cd`, is denied; a `workdir` / "
    "`cwd` argument is denied outright, whatever it names.\n")


# probe report section 5(b): the nested `tools.*` surface codex_config() is expected to expose, given
# code_mode_host = true, unified_exec_tty = false, goals = false, sleep_tool = false and the rest of
# DISABLED_FEATURES (verified under --strict-config against the real binary, probe report "Recommended adapter
# changes" / runs/rec). tests/test_codex_probe.py asserts the REAL binary's nested `tools` object never exceeds
# this set - a Codex upgrade that adds a new nested tool must be triaged (mapped here, or added to DENIED_TOOLS /
# left to the unmapped-tool default-deny) before any codex row, not discovered live by a worker.
EXPECTED_NESTED_TOOLS = frozenset({"apply_patch", "clock__curr_time", "exec_command",
                                   "list_mcp_resource_templates", "list_mcp_resources", "read_mcp_resource",
                                   "write_stdin"})


def worker_system_prompt(system_prompt, capability, cwd):
    """The signed prompt a codex worker gets: CD_PREFIX_RULE appended, naming the EXACT prefix _decide_exec will
    require, the same pattern as hermes.worker_system_prompt appending gate.PATCH_EDIT_RULE. Called by
    adapters/codex.py before writing the private home's instructions file.

    M1: EVERY capability gets this, mr-review included. gate.READ_ONLY_CAPABILITIES
    (write_file / patch / terminal hard-denied) does NOT mean "no Bash" here: Codex reads through the shell, and
    a read command (cat/head/.../rg, see READ_COMMANDS) past the required cd prefix is gated via Read/Grep/Glob,
    never via gate.decide("terminal") - so mr-review's reads need the prefix just like any other capability's.
    Leaving the prompt unchanged for it (the old behaviour) meant its first shell read was always denied for a
    missing prefix it was never told about."""
    return system_prompt + CD_PREFIX_RULE.format(prefix=_cd_prefix(cwd))


def _allow():
    return _gate.Verdict(True, "allowed")


def _rg_hard(argv):
    for a in argv[1:]:
        if a == "--":
            break
        if a.startswith("--"):
            name = a[2:].split("=", 1)[0]
            if name and any(d.startswith(name) or name.startswith(d) for d in RG_DENY_LONG):
                return "rg option %r runs a program" % a
        elif a.startswith("-") and len(a) > 1 and set(a[1:]) & RG_DENY_SHORT:
            return "rg option -z in %r (decompresses through a program)" % a
    return None


def _search_paths(argv):
    """(Literal paths, -f files, follows symlinks, rg globs, glob argv indexes); None keeps the old floor.

    Only model known option arities: guessing that an unknown option is flag-only could mistake its value
    for a path and miss an implicit cwd search. Unmodelled options deliberately keep the old floor.
    Collect positionals before dropping the default pattern: -e/-f may occur AFTER a path operand.
    """
    prog = argv[0]
    value_short = "ABCefm" + ("EjMrtTg" if prog == "rg" else "")
    flag_short = "FivwxcslLqnbHhIoa" + ("NFPUSu0" if prog == "rg" else "EGPZzUrR")
    value_long = {"after-context", "before-context", "context", "max-count", "regexp", "file"}
    flag_long = {"line-number", "ignore-case", "word-regexp", "line-regexp", "fixed-strings",
                 "invert-match", "only-matching", "count", "files-with-matches", "files-without-match",
                 "quiet", "no-messages", "with-filename", "no-filename", "text"}
    if prog == "grep":
        flag_long.update(("recursive", "dereference-recursive"))
    else:
        flag_long.add("follow")
        value_long.add("glob")
    paths, pattern_files, pattern_given, end, followlinks = [], [], False, False, False
    globs, glob_indexes = [], set()
    i = 1
    while i < len(argv):
        a = argv[i]
        if not end and a == "--":
            end = True
        elif not end and a.startswith("--"):
            name, eq, value = a[2:].partition("=")
            if prog == "rg" and name == "files" and not eq:
                pattern_given = True        # file listing has no pattern operand
            elif name in value_long:
                option_index = i
                pattern_given |= name in ("regexp", "file")
                if not eq:
                    i += 1
                    if i >= len(argv):
                        return None
                    value = argv[i]
                if name == "file":
                    pattern_files.append(value)
                elif name == "glob":
                    globs.append(value)
                    glob_indexes.update((option_index, i))
            elif name not in flag_long or eq:
                return None
            elif name == ("follow" if prog == "rg" else "dereference-recursive"):
                followlinks = True
        elif not end and a.startswith("-") and a != "-":
            for k, ch in enumerate(a[1:], 1):
                if ch in value_short:
                    option_index = i
                    pattern_given |= ch in "ef"
                    value = a[k + 1:]
                    if k == len(a) - 1:
                        i += 1
                        if i >= len(argv):
                            return None
                        value = argv[i]
                    if ch == "f":
                        pattern_files.append(value)
                    elif ch == "g":
                        globs.append(value)
                        glob_indexes.update((option_index, i))
                    break                  # the rest of this cluster is the option's value
                if ch not in flag_short:
                    return None
                if ch == ("L" if prog == "rg" else "R"):
                    followlinks = True
        else:
            paths.append(a)
        i += 1
    paths = paths if pattern_given else paths[1:]
    if any(set(p) & set("*?[]{}") for p in paths):
        return None
    return paths, pattern_files, followlinks, globs, glob_indexes


def _read_segment(policy, argv, cwd):
    """None if this one segment is an allowed read, else a reason."""
    prog = argv[0]
    need = READ_COMMANDS[prog]
    if not (policy.tool_allowed("Read") and policy.tool_allowed(need)):
        return "%s: %s is not granted for this capability" % (prog, need)
    if prog == "rg":
        why = _rg_hard(argv)
        if why:
            return why
    skip = set()
    operands = _search_paths(argv) if prog in CONTENT_SEARCH else None
    if operands is not None:
        skip.update(operands[4])       # parsed rg filters name no files, including negative globs
    if prog == "sed":
        why = _gate._sed_hard(argv)
        if why:
            return why
        skip = {1, 2}
    why = _gate._check_args_paths(policy, argv, cwd, skip)
    if why:
        return why
    if prog in CONTENT_SEARCH:
        globs = None
        if operands is None:                   # unmodelled / tree-scoped options: preserve the old floor
            paths = [cwd] + [a for a in argv[1:] if not a.startswith("-")]
            followlinks = True                 # unknown options may enable symlink traversal too
        else:
            paths, pattern_files, followlinks, globs, _ = operands
            for path in pattern_files:         # -f reads a file, even attached inside a short-option cluster
                _, why = _gate._check_read_path(policy, path, cwd)
                if why:
                    return why
        for path in paths or [cwd]:            # no explicit target: rg/recursive grep may search cwd
            r, why = _gate._check_read_path(policy, path, cwd)
            if why:
                return why
            if os.path.isdir(r):
                # Vet reachable files: rg -L/--follow and grep -R /
                # --dereference-recursive can read descendants behind in-worktree directory symlinks.
                why = _gate._walk_clean(policy, r, followlinks=followlinks, globs=globs)
                if why:
                    return why
    return None


def _decide_exec(tool, policy, ti):
    """-> (hermes tool name, args, Verdict). Shared by Codex's classic `Bash` tool call and the code_mode_only
    `exec_command` call (tools.exec_command() from the sandboxed JS, the code-mode gating fix): same rules either
    way.

    The workdir-bypass fix (HIGH): a `workdir` / `cwd` argument is never honoured (Codex strips it from what this
    hook sees but still honours it when it actually runs the command - see the module docstring's WORKDIR BYPASS
    FIX section), so it is denied outright here instead. Confinement is enforced in the command TEXT: the command
    must start with the exact literal `_cd_prefix(policy.cwd)` and contain no other `cd`, which forces the real
    execution into the worktree whatever hidden workdir Codex actually started the shell in, before anything
    past that prefix is evaluated against policy.cwd exactly as _read_segment / gate.decide("terminal") assume.

    H1: when the part AFTER the prefix does not even parse, this denies outright rather
    than treating it as an empty segment list - an unparseable rest (e.g. a bare `&`) must never silently skip
    the second-cd / bare-interpreter checks below, nor fall through to gate.decide("terminal") where a judged
    capability would have only the raw text, with no mechanical floor under it, to go on."""
    extra = set(ti) - EXEC_KEYS
    if any(ti.get(k) for k in ESCALATION_KEYS):
        return "terminal", ti, _gate._deny("%s: sandbox escalation is not allowed" % tool)
    if extra:
        return "terminal", ti, _gate._deny("%s: unexpected argument(s) %s" % (tool, sorted(extra)))
    if ti.get("workdir") or ti.get("cwd"):
        return "terminal", ti, _gate._deny(
            "%s: a workdir/cwd argument is never honoured (workdir-bypass fix: Codex hides the real "
            "workdir from this hook but still honours it) - the command itself must carry the required cd "
            "prefix instead" % tool)
    cmd = ti.get("command", ti.get("cmd"))
    if isinstance(cmd, list) and cmd and all(isinstance(w, str) for w in cmd):
        cmd = " ".join(shlex.quote(w) for w in cmd)          # an argv list: the lexer splits it back to the same words
    if not isinstance(cmd, str) or not cmd.strip():
        return "terminal", {"command": cmd}, _gate._deny("%s: empty or malformed command" % tool)
    args = {"command": cmd}
    prefix = _cd_prefix(policy.cwd)
    if not cmd.startswith(prefix):
        return "terminal", args, _gate._deny(
            "%s: command must start with exactly %r (workdir-bypass fix: Codex hides the real workdir "
            "from this hook but still honours it, so the command itself must force its own cwd into the "
            "worktree) - this one starts %r" % (tool, prefix, cmd[:len(prefix) + 40]))
    rest = cmd[len(prefix):]
    if not rest.strip():
        return "terminal", args, _gate._deny("%s: nothing after the required cd prefix" % tool)
    try:
        segs = _gate.split_command(rest)
    except _gate.Deny as e:
        # H1: deny outright - do NOT treat an unparseable rest as an empty segment list and fall through. That
        # would skip the second-cd and bare-interpreter checks below vacuously, and (for a judged capability)
        # hand the judge the raw text alone with no mechanical floor under it - exactly the bare-`&` shape that
        # escapes the cd prefix.
        return "terminal", args, _gate._deny(
            "%s: the command after the required cd prefix does not parse (%s) - denied outright (H1: an "
            "unparseable rest must not skip the second-cd / bare-interpreter checks)" % (tool, e))
    if any(argv and _changes_dir(argv) for argv in segs):
        return "terminal", args, _gate._deny(
            "%s: a second directory change (cd/pushd/popd - plain, env-prefixed, builtin/command/eval/time-"
            "wrapped, a compound-command body, or shopt -s autocd) is not allowed - the gate cannot see where "
            "it would then leave the command (workdir-bypass fix, N1)" % tool)
    bare = next((argv for argv in segs if _is_interactive_bare(argv)), None)
    if bare:                                  # LOW-1 belt: the real floor is unified_exec_tty=false (codex_config)
        return "terminal", args, _gate._deny(
            "%s: segment %r starts an interactive stdin-reading interpreter with no script / inline code - a "
            "later write_stdin would never reach PreToolUse (fail-closed)" % (tool, " ".join(bare)[:200]))
    if segs and all(argv and argv[0] in READ_COMMANDS for argv in segs):
        cwd = policy.cwd
        for argv in segs:
            why = _read_segment(policy, argv, cwd)
            if why:
                return "terminal", args, _gate._deny("%s: segment %r: %s" % (tool, " ".join(argv)[:200], why))
        return "terminal", args, _allow()
    return "terminal", args, _gate.decide("terminal", {"command": rest}, policy)


def patch_paths(text):
    """Every path a Codex apply_patch text adds, updates, deletes or moves to, in order (deduplicated)."""
    out = []
    for line in (text or "").splitlines():
        line = line.rstrip("\r")
        m = _PATCH_FILE.match(line) or _PATCH_MOVE.match(line)
        if m and m.group(1).strip() and m.group(1).strip() not in out:
            out.append(m.group(1).strip())
    return out


def _patch_text(ti):
    for k in ("input", "patch", "command"):
        v = ti.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):
            hits = [w for w in v if isinstance(w, str) and "*** Begin Patch" in w]
            if len(hits) == 1:
                return hits[0]
    return None


def _decide_patch(policy, ti):
    text = _patch_text(ti)
    paths = patch_paths(text)
    if not paths:
        return "patch", ti, _gate._deny("apply_patch: no file headers found in the patch (fail-closed)")
    for p in paths:
        v = _gate.decide("write_file", {"path": p, "content": text}, policy)
        if not v.allowed:
            return "patch", {"path": p}, v
    return "patch", {"path": paths[0]}, _allow()


def evaluate(event, policy):
    """One Codex PreToolUse event -> (hermes tool name, args, Verdict). Never raises."""
    try:
        if policy is None:
            return "?", {}, _gate._deny("no policy loaded - fail-closed")
        if not isinstance(event, dict):
            return "?", {}, _gate._deny("malformed hook event - fail-closed")
        tool = event.get("tool_name")
        ti = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
        if not isinstance(tool, str) or not tool:
            return "?", ti, _gate._deny("hook event has no tool_name - fail-closed")
        if tool == _gate.PROBE_TOOL:
            return tool, ti, _gate.decide(tool, ti, policy)
        if tool in EXEC_TOOL_NAMES:
            return _decide_exec(tool, policy, ti)
        if tool == "apply_patch":
            return _decide_patch(policy, ti)
        if tool in ALLOWED_TOOLS:
            return tool, ti, _allow()
        if tool in DENIED_TOOLS:
            return tool, ti, _gate._deny(
                "%s: denied outright (LOW-1: a running exec session's write_stdin never reaches PreToolUse; "
                "Bash/exec_command refuse to ever start a bare interactive session, so there is nothing live "
                "to feed)" % tool)
        return tool, ti, _gate._deny("unmapped-tool: %s (no eph-gate mapping for this Codex tool; default deny)"
                                     % tool, kind="unmapped-tool")
    except Exception as e:                                    # noqa: BLE001 - fail closed
        return "?", {}, _gate._deny("internal codex gate error (%s) - fail-closed" % type(e).__name__)


def load_policy(path):
    try:
        with open(path, encoding="utf-8") as f:
            return _gate.Policy(json.load(f))
    except Exception:                                         # noqa: BLE001 - no/garbled policy: deny everything
        return None


def _deadline(_signum, _frame):
    """SIGALRM: the call took too long to decide -> deny and exit NOW (os._exit: no handler, cleanup or a blocked
    call in the main flow can turn it into an allow)."""
    try:
        sys.stderr.write("eph-gate: DENIED - codex hook deadline (%ss) exceeded (fail-closed)\n" % DEADLINE_S)
        sys.stderr.flush()
    finally:
        os._exit(2)


def main(argv, stdin, deadline_s=DEADLINE_S):
    """The hook entry point -> exit code (0 allow, 2 deny). Deny reasons go to stderr."""
    if deadline_s and hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _deadline)
        signal.alarm(deadline_s)
    try:
        # Codex runs no post-write linter (that table is hermes-only): an empty table keeps gate._shell_linted from
        # denying .js/.ts/.go/.rs edits here. Only in this hook process, never in the broker's own.
        stub = types.ModuleType(_gate.LINT_MODULE)
        stub.LINTERS = {}
        sys.modules[_gate.LINT_MODULE] = stub
        policy_path = argv[1] if len(argv) > 1 else ""
        policy = load_policy(policy_path) if policy_path else None
        try:
            event = json.loads(stdin.read() or "null")
        except ValueError:
            event = None
        tool, args, v = evaluate(event, policy)
        if v.allowed:
            return 0
        if tool != _gate.PROBE_TOOL and policy is not None:
            record_denial(tool, args, v, home=os.path.dirname(policy_path))
        sys.stderr.write((v.reason or "eph-gate: DENIED") + "\n")
        return 2
    except BaseException:                                     # noqa: BLE001 - never fail open
        try:
            sys.stderr.write("eph-gate: DENIED - codex hook error (fail-closed)\n")
        except Exception:                                     # noqa: BLE001
            pass
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin))
