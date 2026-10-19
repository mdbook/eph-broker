"""eph-gate decision logic: Claude-Code permission parity for a hermes worker, as a pure function.

`decide(tool_name, args, policy)` returns Verdict(allowed, reason, modified_args). The hermes plugin (__init__.py)
calls it from its `pre_tool_call` hook; the broker unit-tests it directly. STDLIB ONLY and Python 3.9-safe: it
runs inside hermes' venv (3.11) AND under the broker's test runner (3.9).

The POLICY is not written here. The adapter (adapters/hermes.py) serialises it per turn from the SAME source a CC
worker gets: `cc.capability_tool_grant(capability, ...)` (its --allowedTools / --disallowedTools /
--permission-mode) + `cc._SECRET_FILE_GLOBS`. A tightening of those lists in cc.py reaches hermes workers with
no edit here. What IS written here is (a) the translation of CC rule syntax onto hermes tools and (b) a small
set of HARD, fail-closed denies that hold whatever the imported lists say (git file-reading options, confinement
to the worktree, secret-file floor, `.git/` writes, `bash +n`, pytest file-writing options, shell-linted writes).

TOOL MAPPING (anything else -> DENY, incl. the tool_search bridge):
    read_file              -> Read            path confined to read_root, secret floor
    search_files content   -> Read + Grep     path confined; the tree under it is walked: any secret-floor file or
                                              any symlink leaving read_root -> deny
    search_files files     -> Read + Glob     path confined (names only, no content)
    write_file / patch     -> Edit / Write    only under acceptEdits (write/code), confined to write_root (= the
                                              worker cwd, like CC's acceptEdits), never under a `.git` component,
                                              secret floor; patch only in its plain replace mode (no V4A patches);
                                              no file type hermes would still shell-lint (SHELL_LINT_EXTS)
    terminal               -> Bash(...)       every segment of the command must match an allow rule and no deny
                                              rule; foreground only; workdir confined; argument checks below
    tool_call/tool_search/tool_describe -> DENY always (bridge off in config; the underlying name is reported)
READ-ONLY CAPABILITIES (READ_ONLY_CAPABILITIES, mr-review): write_file / patch / terminal are HARD-denied before
any rule is consulted, whatever the imported lists say (the CC mr-review grant has no acceptEdits and no Bash, so
the rule layer denies them too; this is the fail-closed belt under it). Only read_file / search_files remain.

COMMAND-PARSING CEILING (terminal). `split_command` is a small POSIX-shell lexer, not bash. It understands
words, '…' and "…" quoting, backslash escapes, line continuations, and the list/pipe operators ; && || | and
newline (it splits there and gates each segment). It REFUSES outright, wherever they appear unquoted: $ (any
expansion, $(..), $'..', ${..}), backticks, ( ) subshells/grouping, < > redirections, heredocs and process
substitution, a lone & (background), |&, glob/brace chars * ? [ ] { }, a word-start # (comment), a ~ that bash
could expand, control characters, and an unterminated quote. Inside "…" it refuses $ and backticks. So it
never has to model an expansion: any command whose meaning depends on one is denied (fail-closed).
ARGUMENT CHECKS (what is actually checked, not "every argument is resolved"): each non-message word is split
into path candidates (the word, an attached `-Xvalue`, every `=` value incl. bare `key=v` and nested `k=v=w`,
every comma-list item, the part after the last `:`), and each candidate must resolve (normpath AND realpath)
inside read_root and off the secret floor. Denied outright: a word starting with `:` (git pathspec magic such as
`:(icase)`), any candidate starting with `@` (pytest/argparse response files). git: global options before the
subcommand, GIT_DENY_LONG prefixes, `--paths*`, short F/t/O/S, `git add -f/--force`; after `--` every word is a
path. pytest: PYTEST_DENY_LONG prefixes and `-o`. bash/sh: only `-n <file>`.
NOT checked: what a program reads from a word the candidate split doesn't model (e.g. an option value in a
syntax other than `=` / `,` / `:`), and where an allowed program WRITES: it is confined to read_root (the repo),
not write_root (the cwd), apart from the pytest writers denied above. Those pytest denies are defence in depth
only: a `code` worker can write pytest.ini / tox.ini / setup.cfg / pyproject.toml with `addopts = --basetemp=..
--junitxml=/abs` or `cache_dir = /abs`, or a conftest.py, and pytest applies it from the ini, the same class as
pytest running repo code by design.
Residual it cannot see: what an ALLOWED program does with its arguments (e.g. pytest runs repo code, the
same exposure CC has), and functions/aliases the shell snapshot sources from the user's login profile
(hermes sources it like CC's own shell snapshot; the config turns off the extra ~/.bashrc auto-source).
"""
import os, re, sys

PROBE_TOOL = "__eph_gate_probe__"
# capabilities that may only READ: write_file / patch / terminal are denied outright for these (fail-closed)
READ_ONLY_CAPABILITIES = frozenset({"mr-review"})
READ_ONLY_DENIED_TOOLS = ("write_file", "patch", "terminal")
BRIDGE_TOOLS = ("tool_call", "tool_search", "tool_describe")

# ---- hard git denies (independent of the imported lists) --------------------------------------------------
# long options that read/write an arbitrary file, run a program, or reach another repo. git accepts any
# unambiguous PREFIX of a long option (`--fil` == `--file`, `--pathspec-fr` == `--pathspec-from-file`), so an
# argument is denied when it is a prefix of one of these.
GIT_DENY_LONG = ("file", "pathspec-from-file", "pathspec-file-nul", "no-index", "output", "output-directory",
                 "template", "gpg-sign", "ext-diff", "exec", "exec-path", "upload-pack", "receive-pack",
                 "git-dir", "work-tree", "namespace", "config-env", "contents", "orderfile", "ignore-revs-file",
                 "reference", "separate-git-dir", "include-untracked-files-from")
# short options with the same effect: commit -F <file> / -t <template> / -S (gpg), diff -O <orderfile>.
# A short cluster is scanned char by char (`-aF x` == `-a -F x`). The ONE value char that stops the scan is
# commit's `-m` (the rest of the cluster is message text); every other attached value is scanned too, so a
# value that happens to contain F/t/O/S is a false deny (fail-closed), never a miss.
GIT_DENY_SHORT = set("FtOS")

SHELLS = ("bash", "sh")

# pytest long options that write a file / dir wherever they point (--basetemp WIPES its dir), override the ini
# (-o cache_dir=...), or send data off-box (--pastebin). Prefix-matched like GIT_DENY_LONG (argparse accepts
# unambiguous abbreviations). The short -o (--override-ini) is denied in a cluster too.
PYTEST_DENY_LONG = ("junitxml", "junit-xml", "basetemp", "override-ini", "log-file", "debug", "resultlog",
                    "result-log", "report-log", "pastebin")
PYTEST_VALUE_SHORT = set("kmpcrW")        # short options whose value may be attached: the cluster scan stops there

# After an ALLOWED write_file / patch, hermes runs a SHELL linter for these extensions
# (tools/file_operations_lint.py LINTERS, v0.21.5: .js `node --check`, .ts `npx tsc`, .go `go vet`, .rs
# `rustfmt --check`), outside pre_tool_call. .py/.json/.yaml/.toml are linted IN-PROCESS (no program). No config
# key turns the shell tier off, so the plugin empties that table at register(); the gate then re-checks the
# LIVE table on every write and denies an extension it would still shell-lint. Without the hermes module
# loaded (the broker's own tests) this static list is the answer.
SHELL_LINT_EXTS = (".js", ".ts", ".go", ".rs")
LINT_MODULE = "tools.file_operations_lint"

_WS = " \t"
_SEP = ";\n"


class Deny(Exception):
    pass


class Verdict(object):
    """kind tells a replay WHY a call was refused: "floor" (an eph-gate rule/floor), "unmapped-tool"
    (the tool table has no row for it: the table may be too narrow), "judge" (the policy judge said no). sha (judge
    only) = the allow-once token name for the exact subject (see judge_subject)."""
    __slots__ = ("allowed", "reason", "modified_args", "kind", "sha", "subject")

    def __init__(self, allowed, reason, modified_args=None, kind="floor", sha=None, subject=None):
        self.allowed, self.reason, self.modified_args = allowed, reason, modified_args
        self.kind, self.sha, self.subject = kind, sha, subject

    def __repr__(self):
        return "Verdict(%r, %r, %r, %r)" % (self.allowed, self.reason, self.modified_args, self.kind)


def _deny(reason, kind="floor"):
    return Verdict(False, "eph-gate: DENIED - " + reason, kind=kind)


# ==========================================================================================================
# shell lexing
# ==========================================================================================================
def split_command(cmd):
    """Split a command string into segments (each a list of words, quotes removed). Raises Deny on anything the
    lexer refuses to model (see the module docstring)."""
    if not isinstance(cmd, str) or not cmd.strip():
        raise Deny("empty command")
    segs, words = [], []
    cur, in_word = [], False
    i, n = 0, len(cmd)

    def end_word():
        nonlocal cur, in_word
        if in_word:
            words.append("".join(cur))
        cur, in_word = [], False

    def end_seg():
        nonlocal words
        end_word()
        if words:
            segs.append(words)
        words = []

    while i < n:
        c = cmd[i]
        o = ord(c)
        if (o < 0x20 and c not in "\t\n") or o == 0x7f:
            raise Deny("control character in command")
        if c in _WS:
            end_word(); i += 1; continue
        if c in _SEP:
            end_seg(); i += 1; continue
        if c == "&":
            if cmd[i + 1:i + 2] == "&":
                end_seg(); i += 2; continue
            raise Deny("'&' (background / &> redirection)")
        if c == "|":
            if cmd[i + 1:i + 2] == "|":
                end_seg(); i += 2; continue
            if cmd[i + 1:i + 2] == "&":
                raise Deny("'|&'")
            end_seg(); i += 1; continue
        if c in "<>":
            raise Deny("redirection (%s)" % c)
        if c in "()":
            raise Deny("subshell / grouping (%s)" % c)
        if c == "`":
            raise Deny("backtick command substitution")
        if c == "$":
            raise Deny("'$' expansion / substitution")
        if c in "*?[]{}":
            raise Deny("unquoted glob / brace character (%s)" % c)
        if c == "#" and not in_word:
            raise Deny("comment ('#')")
        if c == "~" and not (in_word and cur and cur[-1].isalnum()):
            raise Deny("unquoted '~' (tilde expansion)")
        if c == "\\":
            if i + 1 >= n:
                raise Deny("trailing backslash")
            nxt = cmd[i + 1]
            if nxt == "\n":                               # line continuation
                i += 2; continue
            if (ord(nxt) < 0x20 and nxt != "\t") or ord(nxt) == 0x7f:
                raise Deny("control character in command")
            cur.append(nxt); in_word = True; i += 2; continue
        if c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                raise Deny("unterminated single quote")
            chunk = cmd[i + 1:j]
            if any((ord(x) < 0x20 and x not in "\t\n") or ord(x) == 0x7f for x in chunk):
                raise Deny("control character in command")
            cur.append(chunk); in_word = True; i = j + 1; continue
        if c == '"':
            j = i + 1
            buf = []
            while True:
                if j >= n:
                    raise Deny("unterminated double quote")
                d = cmd[j]
                if d == '"':
                    break
                if d in "$`":
                    raise Deny("expansion inside double quotes (%s)" % d)
                if (ord(d) < 0x20 and d not in "\t\n") or ord(d) == 0x7f:
                    raise Deny("control character in command")
                if d == "\\" and j + 1 < n and cmd[j + 1] in '$`"\\\n':
                    if cmd[j + 1] != "\n":
                        buf.append(cmd[j + 1])
                    j += 2; continue
                buf.append(d); j += 1
            cur.append("".join(buf)); in_word = True; i = j + 1; continue
        cur.append(c); in_word = True; i += 1
    end_seg()
    if not segs:
        raise Deny("empty command")
    return segs


# ==========================================================================================================
# Claude-Code rule matching
# ==========================================================================================================
def split_rules(s):
    """A CC --allowedTools / --disallowedTools value (space-joined, rules may contain spaces inside parens)
    as a list of rules. Paren-aware, like the CLI."""
    out, cur, depth = [], [], 0
    for c in s or "":
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        if c in " \t" and depth == 0:
            if cur:
                out.append("".join(cur)); cur = []
            continue
        cur.append(c)
    if cur:
        out.append("".join(cur))
    return out


def parse_rule(rule):
    """'Bash(git add:*)' -> ('Bash', 'git add:*'); 'Read' -> ('Read', None)."""
    m = re.match(r"^([A-Za-z0-9_*.-]+)(?:\((.*)\))?$", rule or "", re.S)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _wild_regex(pat):
    return re.compile("^" + ".*".join(re.escape(p) for p in pat.split("*")) + "$", re.S)


def bash_rule_matches(spec, argv, for_allow):
    """Does the Bash(<spec>) rule match one segment (argv = words, quotes removed)?
    `x:*` = word-prefix x; exact `x` = the whole argv; a mid-string `*` = wildcard over the space-joined
    command. An ALLOW wildcard never matches argv words containing whitespace (a quoted space would let the
    joined string line up with a pattern the real argv doesn't have); a DENY wildcard always tries."""
    if spec is None:
        return True                                        # bare `Bash`
    joined = " ".join(argv)
    if spec.endswith(":*"):
        prefix = spec[:-2]
        if "*" not in prefix:
            pw = prefix.split()
            return bool(pw) and argv[:len(pw)] == pw
        pat = _wild_regex(prefix)
        if for_allow and any(set(w) & set(" \t\n") for w in argv):
            return False
        return bool(pat.match(joined)) or any(pat.match(" ".join(argv[:k])) for k in range(1, len(argv)))
    if "*" in spec:
        if for_allow and any(set(w) & set(" \t\n") for w in argv):
            return False
        return bool(_wild_regex(spec).match(joined))
    return argv == spec.split()


def glob_to_regex(g):
    """CC path glob -> regex over an ABSOLUTE path. `//abs` = absolute; `**/` any dirs; `*` one component."""
    if g.startswith("//"):
        g = g[1:]
    elif g.startswith("~/"):
        g = os.path.expanduser("~") + g[1:]
    elif not g.startswith("/"):
        g = "/**/" + g                                     # cwd-relative / bare: match anywhere (stricter)
    out, i = [], 0
    while i < len(g):
        if g.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif g.startswith("**", i):
            out.append(".*"); i += 2
        elif g[i] == "*":
            out.append("[^/]*"); i += 1
        elif g[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(g[i])); i += 1
    return re.compile("^" + "".join(out) + "$", re.S)


# ==========================================================================================================
# policy
# ==========================================================================================================
# gate-hard secret floor, a belt UNDER the imported cc._SECRET_FILE_GLOBS (which is the authoritative list and
# normally a superset): even a policy that arrives without secret_globs keeps the env-file / pii-store floor.
HARD_SECRET_GLOBS = ("//**/*-env", "//**/.env", "//**/.env.*", "//**/*.env", "//**/.pii-store", "//**/.pii-store/**")


class Policy(object):
    def __init__(self, d):
        if not isinstance(d, dict):
            raise ValueError("policy must be a dict")
        self.nonce = str(d.get("nonce") or "")
        self.capability = str(d.get("capability") or "")
        self.read_only = self.capability in READ_ONLY_CAPABILITIES
        self.cwd = self._abs(d["cwd"])
        self.write_root = self._abs(d.get("write_root") or d["cwd"])
        self.read_root = self._abs(d.get("read_root") or d["cwd"])
        self.edits = bool(d.get("edits"))
        self.allow = [parse_rule(r) for r in d.get("allow") or []]
        self.deny = [parse_rule(r) for r in d.get("deny") or []]
        if any(t is None for t, _ in self.allow + self.deny):
            raise ValueError("unparseable rule in policy")
        globs = list(d.get("secret_globs") or []) + list(HARD_SECRET_GLOBS)
        self.secret_res = [glob_to_regex(g) for g in globs]
        self.read_deny_res = [glob_to_regex(s) for t, s in self.deny if t in ("Read", "Grep", "Glob") and s]
        self.edit_deny_res = [glob_to_regex(s) for t, s in self.deny
                              if t in ("Edit", "Write", "MultiEdit") and s]
        self.walk_cap = int(d.get("walk_cap") or 20000)
        # The policy judge (general only). Present = terminal/write_file/patch also need a judge allow.
        self.judge = d.get("judge") if isinstance(d.get("judge"), dict) else None
        if self.judge is not None and self.read_only:
            raise ValueError("a read-only capability never carries a judge")

    @staticmethod
    def _abs(p):
        p = os.path.realpath(p)
        if not os.path.isabs(p):
            raise ValueError("policy root must be absolute")
        return p

    # -- tool-name rules --
    def _bare(self, rules, name):
        return any(t == name and s is None for t, s in rules)

    def tool_allowed(self, name):
        return self._bare(self.allow, name) and not self._bare(self.deny, name)

    def tool_denied(self, name):
        return self._bare(self.deny, name)


def _inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _resolve(raw, base):
    """(normpath, realpath) of a tool/terminal path relative to `base`. `~` is expanded (then usually lands
    outside the root)."""
    p = os.path.expanduser(raw) if raw.startswith("~") else raw
    if not os.path.isabs(p):
        p = os.path.join(base, p)
    return os.path.normpath(p), os.path.realpath(p)


def _secret(policy, *paths):
    for p in paths:
        for rx in policy.secret_res:
            if rx.match(p):
                return True
    return False


def _check_read_path(policy, raw, base=None):
    """Return (resolved_path, None) or (None, reason)."""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None, "missing/invalid path"
    norm, real = _resolve(raw, base or policy.cwd)
    if not (_inside(norm, policy.read_root) and _inside(real, policy.read_root)):
        return None, "path %r is outside the worktree (%s)" % (raw, policy.read_root)
    if _secret(policy, norm, real) or any(rx.match(norm) or rx.match(real) for rx in policy.read_deny_res):
        return None, "path %r matches the secret-file read floor" % raw
    return real, None


def _check_write_path(policy, raw):
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None, "missing/invalid path"
    norm, _ = _resolve(raw, policy.cwd)
    parent_real = os.path.realpath(os.path.dirname(norm))
    real = os.path.realpath(norm)
    target = real if os.path.lexists(norm) else os.path.join(parent_real, os.path.basename(norm))
    for p in (norm, target):
        if not _inside(p, policy.write_root):
            return None, "write to %r is outside the worker cwd (%s)" % (raw, policy.write_root)
        if ".git" in p[len(policy.write_root):].split("/"):
            return None, "write under a .git directory (%r)" % raw
    if _secret(policy, norm, target) or any(rx.match(norm) or rx.match(target) for rx in policy.edit_deny_res):
        return None, "write to %r matches the secret-file floor / an Edit deny rule" % raw
    if any(POLICY_SOURCE.search(p[len(policy.write_root):]) for p in (norm, target)):
        return None, "write to %r: a gate policy source (AUTHORIZED.md / .claude settings) - hard floor" % raw
    if _shell_linted(target):
        return None, ("write to %r: hermes would run an external linter on this file type after the write "
                      "(outside the gate)" % raw)
    return target, None


# Belt: files the policy judge reads as policy. HARD floor (never an ask, so never mintable), every
# capability. Not load-bearing: an obfuscated write (python -c with chr()) never shows the name; the real fix is the
# judge's neutral CLAUDE_PROJECT_DIR (GENERAL_JUDGE["project_dir"], see judge()).
POLICY_SOURCE = re.compile(r"(?i)(^|/)(AUTHORIZED\.md|\.claude/+settings[^/]*\.json)$")
# general terminal belt: a command that NAMES a policy source or the gate dir is a hard floor deny (raw substring, so
# it holds for commands the lexer refuses; same blind spot as above for computed names).
GENERAL_TERMINAL_FLOOR = re.compile(r"(?i)authori[sz]ed\.md|\.claude/+(\./+)*settings|eph-judge")


def _shell_linted(path):
    """Would hermes' post-write lint pass start a PROGRAM for `path`? (see SHELL_LINT_EXTS)"""
    ext = os.path.splitext(path)[1].lower()
    mod = sys.modules.get(LINT_MODULE)
    table = getattr(mod, "LINTERS", None) if mod is not None else None
    if not isinstance(table, dict):
        return ext in SHELL_LINT_EXTS
    return ext in table and ext not in (getattr(mod, "LINTERS_INPROC", None) or {})


def _walk_clean(policy, root):
    """content search reads every file under `root`: deny if any is on the secret floor or any symlink leaves
    read_root. Bounded (walk_cap entries); over the cap is a deny, not a skip."""
    if not os.path.isdir(root):
        return None
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            seen += 1
            if seen > policy.walk_cap:
                return "search tree too large to vet (> %d entries) - narrow the path" % policy.walk_cap
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                real = os.path.realpath(p)
                if not _inside(real, policy.read_root) or _secret(policy, real):
                    return "search tree holds a symlink leaving the worktree / to a secret (%s)" % p
            if _secret(policy, p):
                return "search tree holds a secret-floor file (%s)" % p
    return None


# ==========================================================================================================
# terminal
# ==========================================================================================================
def _path_candidates(arg):
    """Substrings of one argv word that the program could treat as a path: the word itself, an attached short
    value (`-Xvalue`), every `=` value (`--opt=v`, a bare `key=v` such as pytest's `-o cache_dir=v`, nested
    `--override-ini=k=v`), every comma-list item (`--deselect=a,/b`) and the part after the last ':' (`x:path`).
    Derived to a fixpoint, so nestings (`k=a,/abs`) resolve too; each step is shorter, so it terminates."""
    seen, todo = set(), [arg]
    while todo:
        c = todo.pop()
        if not c or c in seen:
            continue
        seen.add(c)
        if c.startswith("-") and not c.startswith("--") and len(c) > 2 and "=" not in c:
            todo.append(c[2:])
        if "=" in c:
            todo.append(c.split("=", 1)[1])
        if "," in c:
            todo.extend(c.split(","))
        if ":" in c:
            todo.append(c.rsplit(":", 1)[1])
    return sorted(seen)


def _check_args_paths(policy, argv, cwd, skip):
    for idx, arg in enumerate(argv[1:], 1):
        if idx in skip:
            continue
        if arg.startswith(":"):
            return "argument %r: git pathspec magic (e.g. :(icase)) / `:path` forms are not allowed" % arg
        for c in _path_candidates(arg):
            if c.startswith("@"):
                return "argument %r: an @-prefixed argument (pytest/argparse response file) is not allowed" % arg
            norm, real = _resolve(c, cwd)
            if not (_inside(norm, policy.read_root) and _inside(real, policy.read_root)):
                return "argument %r reaches outside the worktree" % arg
            if _secret(policy, norm, real):
                return "argument %r names a secret-floor file" % arg
    return None


def _git_hard(argv):
    """Return (reason or None, set of argv indexes holding commit-message text, which is not path-checked)."""
    msg_idx = set()
    if len(argv) < 2 or argv[1].startswith("-"):
        return "git global options before the subcommand are not allowed", msg_idx
    commit, add = argv[1] == "commit", argv[1] == "add"
    skip_next = end_of_opts = False
    for idx, a in enumerate(argv[2:], 2):
        if skip_next:
            msg_idx.add(idx); skip_next = False; continue
        if end_of_opts:
            continue                     # after `--` every word is a pathspec (path-checked), never an option
        if a == "--":
            end_of_opts = True; continue
        if a.startswith("--"):
            name = a[2:].split("=", 1)[0]
            if not name or name.startswith("paths") or any(d.startswith(name) for d in GIT_DENY_LONG):
                return "git option %r (reads/writes a file, runs a program or reaches another repo)" % a, msg_idx
            if add and "force".startswith(name):
                return "git add %r (stages ignored files, e.g. secrets)" % a, msg_idx
            if commit and len(name) >= 3 and "message".startswith(name):
                if "=" in a:
                    msg_idx.add(idx)
                else:
                    skip_next = True
            continue
        if a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if ch in GIT_DENY_SHORT:
                    return "git option -%s in %r (reads a file / signs)" % (ch, a), msg_idx
                if add and ch == "f":
                    return "git add -f in %r (stages ignored files, e.g. secrets)" % a, msg_idx
                if commit and ch in "cCu":
                    break                # takes a value (attached, or the next word, which stays path-checked)
                if commit and ch == "m":
                    if k == len(a) - 1:
                        skip_next = True
                    else:
                        msg_idx.add(idx)
                    break
    return None, msg_idx


def _shell_hard(argv):
    # `bash -n <file>` (the bash-n test runner) parses only, but `+n` (or -c/-o/-O/--rcfile …) turns it into
    # execution. Only `-n` and plain file operands are allowed.
    for a in argv[1:]:
        if a.startswith("+") or (a.startswith("-") and a != "-n"):
            return "%s option %r (only `-n <file>` is allowed)" % (argv[0], a)
    return None


def _pytest_hard(argv):
    """pytest options that write outside the gate's view / override the ini (see PYTEST_DENY_LONG)."""
    start = 3 if os.path.basename(argv[0]).startswith("python") else 1
    for a in argv[start:]:
        if a == "--":
            break
        if a.startswith("--"):
            name = a[2:].split("=", 1)[0]
            if name and any(d.startswith(name) for d in PYTEST_DENY_LONG):
                return "pytest option %r (writes a file/dir, overrides the ini or sends data off-box)" % a
        elif a.startswith("-") and len(a) > 1:
            for ch in a[1:]:
                if ch == "o":
                    return "pytest option -o in %r (--override-ini, e.g. cache_dir=)" % a
                if ch in PYTEST_VALUE_SHORT:
                    break
    return None


def _is_pytest(argv):
    prog = os.path.basename(argv[0])
    return prog in ("pytest", "py.test") or (prog.startswith("python") and argv[1:3] == ["-m", "pytest"])


def check_segment(policy, argv, cwd):
    """Return None if the segment is allowed, else a reason."""
    for t, s in policy.deny:
        if t == "Bash" and bash_rule_matches(s, argv, for_allow=False):
            return "matches deny rule Bash(%s)" % s if s is not None else "Bash is denied for this capability"
    if not any(t == "Bash" and bash_rule_matches(s, argv, for_allow=True) for t, s in policy.allow):
        return "%r is not on the allow-list" % " ".join(argv)[:200]
    skip = set()
    prog = os.path.basename(argv[0])
    if prog == "git":
        why, skip = _git_hard(argv)
        if why:
            return why
    elif prog in SHELLS:
        why = _shell_hard(argv)
        if why:
            return why
    elif _is_pytest(argv):
        why = _pytest_hard(argv)
        if why:
            return why
    return _check_args_paths(policy, argv, cwd, skip)


_TERMINAL_KEYS = {"command", "timeout", "workdir", "background", "pty", "notify", "heartbeat"}


def _decide_terminal(policy, args):
    if any(k not in _TERMINAL_KEYS for k in args):
        return _deny("terminal: unexpected argument(s) %s" % sorted(set(args) - _TERMINAL_KEYS))
    for k in ("background", "pty", "notify", "heartbeat"):
        if args.get(k):
            return _deny("terminal: %s is not allowed (foreground only)" % k)
    if args.get("timeout") is not None and not isinstance(args.get("timeout"), int):
        return _deny("terminal: timeout must be an integer")
    cwd = policy.cwd
    if args.get("workdir"):
        wd, why = _check_read_path(policy, args["workdir"])
        if why:
            return _deny("terminal workdir: " + why)
        cwd = wd
    try:
        segs = split_command(args.get("command"))
    except Deny as e:
        return _deny("terminal: " + str(e))
    for argv in segs:
        why = check_segment(policy, argv, cwd)
        if why:
            return _deny("terminal: segment %r: %s" % (" ".join(argv)[:200], why))
    return Verdict(True, "allowed", {"workdir": cwd})


# ==========================================================================================================
# entry point
# ==========================================================================================================
_READ_KEYS = {"path", "offset", "limit"}
_SEARCH_KEYS = {"pattern", "target", "path", "file_glob", "limit", "offset", "order", "output_mode", "context"}


def decide(tool_name, args, policy):
    """The whole decision. Never raises: any internal error is a deny."""
    try:
        return _decide(tool_name, args if isinstance(args, dict) else {}, policy)
    except Exception as e:                                  # noqa: BLE001 - fail closed
        return _deny("internal gate error (%s: %s) - fail-closed" % (type(e).__name__, e))


def _decide(tool_name, args, policy):
    if policy is None:
        return _deny("no policy loaded - fail-closed")
    if tool_name == PROBE_TOOL:
        return Verdict(False, "eph-gate probe ok %s" % policy.nonce)
    if tool_name in BRIDGE_TOOLS:
        under = ""
        calls = args.get("calls") if isinstance(args, dict) else None
        if isinstance(calls, list):
            under = ",".join(str((c or {}).get("name")) for c in calls if isinstance(c, dict))
        return _deny("the tool_search bridge (%s) is disabled%s" % (tool_name,
                     " (underlying: %s)" % under if under else ""))
    if policy.read_only and tool_name in READ_ONLY_DENIED_TOOLS:
        return _deny("%s: capability %s is READ-ONLY (hard deny)" % (tool_name, policy.capability))
    if tool_name == "read_file":
        if set(args) - _READ_KEYS:
            return _deny("read_file: unexpected argument(s)")
        if not policy.tool_allowed("Read"):
            return _deny("read_file: Read is not granted")
        real, why = _check_read_path(policy, args.get("path"))
        return _deny("read_file: " + why) if why else Verdict(True, "allowed", {"path": real})
    if tool_name == "search_files":
        if set(args) - _SEARCH_KEYS:
            return _deny("search_files: unexpected argument(s)")
        target = args.get("target", "content")
        need = "Grep" if target == "content" else "Glob" if target == "files" else None
        if need is None or not (policy.tool_allowed("Read") and policy.tool_allowed(need)):
            return _deny("search_files: %s is not granted" % (need or "target %r" % target))
        real, why = _check_read_path(policy, args.get("path", "."))
        if why:
            return _deny("search_files: " + why)
        for k in (("file_glob",) if target == "content" else ("pattern", "file_glob")):   # content pattern = regex
            v = args.get(k)
            if v is not None and (not isinstance(v, str) or v.startswith(("/", "~")) or ".." in v.split("/")):
                return _deny("search_files: %s %r may leave the search root" % (k, v))
        if target == "content":
            why = _walk_clean(policy, real)
            if why:
                return _deny("search_files: " + why)
        return Verdict(True, "allowed", {"path": real})
    if tool_name in ("write_file", "patch"):
        if not policy.edits or policy.tool_denied("Edit") or policy.tool_denied("Write"):
            return _deny("%s: file edits are not granted for this capability" % tool_name)
        if tool_name == "write_file":
            if set(args) - {"path", "content"}:
                return _deny("write_file: unexpected argument(s)")
        else:
            if set(args) - {"path", "old_string", "new_string", "replace_all", "mode"} or \
                    args.get("mode", "replace") != "replace":
                return _deny("patch: only the plain replace mode is allowed (no V4A multi-file patches)")
        target, why = _check_write_path(policy, args.get("path"))
        if why:
            return _deny("%s: %s" % (tool_name, why))
        if policy.judge is not None:
            ti = {"file_path": target, "content": args.get("content", "")} if tool_name == "write_file" else \
                {"file_path": target, "old_string": args.get("old_string", ""), "new_string": args.get("new_string", ""),
                 "replace_all": bool(args.get("replace_all"))}
            v = judge(policy, "Write" if tool_name == "write_file" else "Edit", ti, policy.cwd)
            if not v.allowed:
                return v
        return Verdict(True, "allowed", {"path": target})
    if tool_name == "terminal":
        if policy.judge is not None:
            return _decide_terminal_general(policy, args)
        if not any(t == "Bash" for t, _ in policy.allow):
            return _deny("terminal: Bash is not granted for this capability")
        return _decide_terminal(policy, args)
    # A table miss is its own kind, so a replay can tell "the table is too narrow" from a judged deny
    return _deny("unmapped-tool: %s (not mapped to a Claude Code rule; default deny)" % tool_name, kind="unmapped-tool")


# ==========================================================================================================
# general: eph-gate floors FIRST, then the policy judge
# ==========================================================================================================
# The general row has no allow-list (CC runs it under the auto classifier / skip-permissions + the _GENERAL_DENIED
# floor), so eph-gate's allow-list path can't decide it. policy.judge (set by adapters/hermes.gate_policy for
# general ONLY) routes the effectful tools to the same judge entry every standing agent uses
# (gate-wrapper.sh calling gate-hook.py; armed = enforce + full, so any no-decision is a BLOCK).
#   read_file / search_files  eph-gate only: secret floor + the imported Read deny rules + (content search) the
#                             tree walk. The judge's own Read/Grep rule is "allow unless a secret path", which the
#                             floor + walk already cover more strictly, so no judge round-trip (no 4B load).
#   write_file / patch        eph-gate floors (write_root = the worktree, no .git, secret floor, no shell-linted
#                             type), THEN the judge as Write / Edit.
#   terminal                  foreground only + workdir floor; if the lexer can split the command, every segment
#                             is checked against the imported Bash deny rules (_GENERAL_DENIED: git push/reset/...,
#                             rm, sudo, mcp.sh ...); THEN the judge as Bash with the EXACT command string.
#   anything else             unmapped-tool deny (process_manage and web_* included: web is not shipped).
# CEILING: the lexer refuses ~2/3 of real general commands (redirections, $(..), globs); for those the deny rules
# can't be applied here and the policy judge (tier0 + rules + 4B) is the only judge. That is the same surface a CC
# general worker on skip-permissions has (its --disallowedTools match is CC's, the policy judge covers the rest).
_JUDGE_ENV_PASS = ("ZBUS_ENV", "TMPDIR", "LANG", "TZ")


def _decide_terminal_general(policy, args):
    if any(k not in _TERMINAL_KEYS for k in args):
        return _deny("terminal: unexpected argument(s) %s" % sorted(set(args) - _TERMINAL_KEYS))
    for k in ("background", "pty", "notify", "heartbeat"):
        if args.get(k):
            return _deny("terminal: %s is not allowed (foreground only)" % k)
    if args.get("timeout") is not None and not isinstance(args.get("timeout"), int):
        return _deny("terminal: timeout must be an integer")
    cmd = args.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return _deny("terminal: empty command")
    if GENERAL_TERMINAL_FLOOR.search(cmd):
        return _deny("terminal: names a gate policy source or the gate dir (AUTHORIZED.md / .claude settings / "
                     "the judge dir) - hard floor")
    cwd = policy.cwd
    if args.get("workdir"):
        wd, why = _check_read_path(policy, args["workdir"])
        if why:
            return _deny("terminal workdir: " + why)
        cwd = wd
    try:
        segs = split_command(cmd)
    except Deny:
        segs = []                                           # unparseable: the policy judge alone judges (CEILING above)
    for argv in segs:
        for t, s in policy.deny:
            if t == "Bash" and bash_rule_matches(s, argv, for_allow=False):
                return _deny("terminal: segment %r matches deny rule Bash(%s)" % (" ".join(argv)[:200], s))
    v = judge(policy, "Bash", {"command": cmd}, cwd)
    if v.allowed:
        v.modified_args = {"workdir": cwd}
    return v


def judge(policy, cc_tool, tool_input, cwd):
    """One policy-judge decision for a CC-shaped call, via the judge wrapper. Returns a Verdict (kind "judge" on a deny).
    Fail-closed: no config, a spawn error, a timeout, rc 2 (the armed wrapper's BLOCK), anything but a well-formed
    allow means deny. On a deny, `sha` is the hook's OWN subject_sha from its decisions.jsonl final row
    (_allow_once_sha) = sha256 of the exact subject bytes, heredoc body + newlines included = the allow-once token
    filename. Passed through byte-exact, never recomputed here.
    CEILING: one shared gate dir (eph-general) = ONE concurrent general worker. Two workers would
    share ask-dm-sent / clearance-pending / m4b-pin state and interleave decisions.jsonl. Per-worker isolation =
    a gate dir per worker (agent name + dir from config, see the ceiling note in adapters/hermes.py); not built."""
    import json, subprocess
    j = policy.judge
    if not isinstance(j, dict) or not all(j.get(k) for k in ("wrapper", "hook", "python", "gate_dir", "project_dir")):
        return _deny("%s: no policy judge configured - fail-closed" % cc_tool, kind="judge")
    pdir = os.path.realpath(j["project_dir"])
    if _inside(pdir, policy.write_root) or _inside(policy.write_root, pdir):
        return _deny("%s: judge project_dir overlaps the worker's write root - fail-closed" % cc_tool, kind="judge")
    # The key fix: CLAUDE_PROJECT_DIR is a NEUTRAL dir outside the worktree, never policy.cwd.
    # gate-hook.agent_policy() injects <CLAUDE_PROJECT_DIR>/AUTHORIZED.md into the 4B prompt as the agent's own
    # authorizations; pointing it at the worktree let the worker write part of its own judge's input.
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "JUDGE_HOOK": j["hook"], "JUDGE_PY": j["python"],
           "JUDGE_DIR": j["gate_dir"], "JUDGE_HOOK_TIMEOUT": str(j.get("timeout") or "5.0"),
           "CLAUDE_PROJECT_DIR": pdir}
    env.update({k: os.environ[k] for k in _JUDGE_ENV_PASS if os.environ.get(k)})
    ev = {"hook_event_name": "PreToolUse", "tool_name": cc_tool, "tool_input": tool_input, "cwd": cwd,
          "session_id": "eph-" + policy.nonce}
    log = os.path.join(j["gate_dir"], "decisions.jsonl")
    try:
        log_off = os.path.getsize(log)
    except OSError:
        log_off = 0
    try:
        r = subprocess.run([j["wrapper"]], input=json.dumps(ev), env=env, cwd=cwd, capture_output=True, text=True,
                           timeout=float(j.get("timeout") or 5.0) + 5)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return _deny("%s: policy judge did not run (%s) - fail-closed" % (cc_tool, type(e).__name__), kind="judge")
    decision, reason = None, ""
    if r.returncode == 0:
        try:
            hso = json.loads(r.stdout)["hookSpecificOutput"]
            decision, reason = hso.get("permissionDecision"), str(hso.get("permissionDecisionReason") or "")
        except (ValueError, KeyError, TypeError):
            decision = None
    else:
        reason = (r.stderr or "").strip()[:400] or "wrapper rc=%s" % r.returncode
    if decision == "allow":
        return Verdict(True, "allowed by the policy judge: " + reason[:200], kind="judge")
    sha = _allow_once_sha(log, log_off) if r.returncode == 0 and decision == "deny" else None
    return Verdict(False, "eph-gate: DENIED by the policy judge - %s%s" % (
        reason or "no decision (fail-closed)",
        " | reported to ops in your result; do not rephrase, split or route around it - carry on with other work "
        "or finish blocked" if sha else ""), kind="judge", sha=sha)


def _allow_once_sha(log, off):
    """The allow-once token name for the call just judged, read from the hook's OWN `kind:"final"` row
    (gate-hook.py emit_outcome: subject_sha = sha256(subject) = the exact name approve_once_consume looks for; the
    hook's stdout reason never carries it, only its operator DM). Byte-exact, never recomputed. Only an allow-once-capable
    final yields one: outcome "ask" (ask/escalate path) or a "4b:" deny (a 4B false-deny is overridable); a tier0 /
    gate_rules hard deny, an operator decline or an expired clearance -> None. Reads only the bytes appended since `off`
    (the one-canary ceiling above: a concurrent worker could interleave; then the LAST final wins; a wrong sha only
    makes the mint a no-op, never an over-grant, because the token must match the retried bytes)."""
    import json
    try:
        with open(log, "rb") as f:
            f.seek(off)
            tail = f.read(1 << 20)
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("kind") != "final":
            continue
        reason, sha = str(row.get("reason") or ""), row.get("subject_sha")
        ok = row.get("outcome") == "ask" or (row.get("outcome") == "deny" and reason.startswith("4b:"))
        return sha if ok and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) else None
    return None
