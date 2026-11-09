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
    search_files content   -> Read + Grep     path confined; the tree under it is walked: any secret-floor file
                                              -> deny; nested symlinks are not followed
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
path. Exact `diff --no-index <a> <b>` is an exception, with directory tree checks too. `log -L` ranges have
their file extracted before path checking (colon-bearing addresses/filenames remain denied as ambiguous).
sed: only `-n '<range>p' <files>` (numeric/$
print addresses). Test env assignments may wrap only the policy's reviewed runner, with an explicit allow-list
of benign names (plus site-reviewed additions); the unwrapped command still passes all rules and argument checks.
pytest: PYTEST_DENY_LONG prefixes and `-o`. bash/sh: only `-n <file>`.
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

ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# Unknown runtimes have unknown execution/configuration knobs: deny every name not explicitly reviewed.
# Site additions are exact, case-sensitive names, never wildcard patterns. Values remain path-checked.
TEST_ENV_ALLOW = frozenset({"PORTCULLIS_PYTHON", "PYTHONDONTWRITEBYTECODE", "NO_COLOR", "CI", "TERM", "LANG"})
TEST_ENV_LOCALE = re.compile(r"LC_[A-Za-z0-9_]+\Z")


def test_env_names(extra):
    """Validate the policy's site additions, shared with the broker-side config reader."""
    if not isinstance(extra, list) or any(not isinstance(n, str) or not ENV_NAME.fullmatch(n) for n in extra):
        raise ValueError("test_env_allow must be a list of exact environment variable names")
    return TEST_ENV_ALLOW.union(extra)

# Deliberately only print addresses, never a general sed program (no scripts, writes, or exec).
SED_PRINT = re.compile(r"(?:[1-9][0-9]*|\$)(?:,(?:[1-9][0-9]*|\$|\+[0-9]+))?p\Z")

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
    (the tool table has no row for it: the table may be too narrow), "judge" (the exec gate said no). sha
    (judge only) = the gate's override token for the exact subject (Portcullis: the allow-once token name)."""
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
        self.test_allow = [parse_rule(r) for r in d.get("test_allow") or []]
        self.test_env_allow = test_env_names(d.get("test_env_allow", []))
        if any(t is None for t, _ in self.allow + self.deny + self.test_allow):
            raise ValueError("unparseable rule in policy")
        globs = list(d.get("secret_globs") or []) + list(HARD_SECRET_GLOBS)
        self.secret_res = [glob_to_regex(g) for g in globs]
        self.read_deny_res = [glob_to_regex(s) for t, s in self.deny if t in ("Read", "Grep", "Glob") and s]
        self.edit_deny_res = [glob_to_regex(s) for t, s in self.deny
                              if t in ("Edit", "Write", "MultiEdit") and s]
        self.walk_cap = int(d.get("walk_cap") or 20000)
        # the exec-gate judge (general only). Present = terminal/write_file/patch also need a judge allow.
        # which gate answers is the judge block's "provider" (providers/), never decided here.
        self.judge = d.get("judge") if isinstance(d.get("judge"), dict) else None
        if self.judge is not None and self.read_only:
            raise ValueError("a read-only capability never carries a judge")
        # the selected gate's own policy files / names join the core floors (see POLICY_SOURCE_CORE)
        fl = d.get("floor") if d.get("floor") is not None else {}
        files, terms = (fl.get("files") or [], fl.get("terms") or []) if isinstance(fl, dict) else (None, None)
        if not (isinstance(files, list) and isinstance(terms, list)
                and all(isinstance(x, str) and x for x in files + terms)):
            raise ValueError("policy floor must be {files: [regex], terms: [regex]}")
        self.policy_source = re.compile(r"(?i)(^|/)(%s)$" % "|".join([POLICY_SOURCE_CORE] + files))
        self.terminal_floor = re.compile(r"(?i)" + "|".join([TERMINAL_FLOOR_CORE] + terms))

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
    norm, raw_real = _resolve(raw, policy.cwd)
    parent_real = os.path.realpath(os.path.dirname(norm))
    real = os.path.realpath(norm)
    target = real if os.path.lexists(norm) else os.path.join(parent_real, os.path.basename(norm))
    if _companion_path(policy, norm, raw_real, target):
        return None, "companion sources are read-only"
    for p in (norm, target):
        if not _inside(p, policy.write_root):
            return None, "write to %r is outside the worker cwd (%s)" % (raw, policy.write_root)
        if ".git" in p[len(policy.write_root):].split("/"):
            return None, "write under a .git directory (%r)" % raw
    if _secret(policy, norm, target) or any(rx.match(norm) or rx.match(target) for rx in policy.edit_deny_res):
        return None, "write to %r matches the secret-file floor / an Edit deny rule" % raw
    if any(policy.policy_source.search(p[len(policy.write_root):]) for p in (norm, target)):
        return None, ("write to %r: a gate policy source (.claude settings / the exec gate's own files), hard "
                      "floor" % raw)
    if _shell_linted(target):
        return None, ("write to %r: hermes would run an external linter on this file type after the write "
                      "(outside the gate)" % raw)
    return target, None


def _companion_path(policy, *paths):
    root = os.path.join(policy.read_root, ".companions")
    return any(_inside(p, root) for p in paths)


def _companion_command(policy, argv, cwd, skip=()):
    """Reject effectful command operands/cwds in the reserved source tree, including symlink aliases.

    The granted test runner may consume sources. Like all worker tests, its *code* is a same-uid residual,
    not something a tool-call gate can sandbox. Read commands still undergo their usual secret/option checks.
    """
    touches = _companion_path(policy, *_resolve(cwd, policy.cwd)) or any(
        _companion_path(policy, *_resolve(c, cwd))
        for i, arg in enumerate(argv[1:], 1) if i not in skip for c in _path_candidates(arg))
    if not touches:
        return None
    while argv and ENV_ASSIGN.match(argv[0]):
        argv = argv[1:]
    if not argv:
        return "companion sources are read-only"
    if argv[0] == "sed":
        return _sed_hard(argv)
    if argv[0] in ("cat", "head", "tail", "wc", "nl", "ls", "grep", "rg"):
        return None
    if argv[0] == "git" and len(argv) > 1 and argv[1] in ("diff", "status", "log"):
        return _git_hard(argv)[0]
    if any(t == "Bash" and bash_rule_matches(s, argv, for_allow=True) for t, s in policy.test_allow):
        return None
    return "companion sources are read-only"


# review item 3 (belt, (a)): files a gate reads as policy. HARD floor (never an ask, so never mintable), every
# capability. Core knows only Claude Code's settings files; the selected provider adds its own (Portcullis:
# AUTHORIZED.md) through policy["floor"], compiled in Policy. Not load-bearing: an obfuscated write (python -c with
# chr()) never shows the name; the real fix is the judge's neutral project dir (providers/portcullis.py).
POLICY_SOURCE_CORE = r"\.claude/+settings[^/]*\.json"
# general terminal belt: a command that NAMES a policy source or the gate's own dir is a hard floor deny (raw
# substring, so it holds for commands the lexer refuses; same blind spot as above for computed names). The gate's
# names come from the provider's FLOOR_TERMS + floor_terms(cfg) (its own state dir) + EXEC_GATE_PROTECTED_TERMS.
TERMINAL_FLOOR_CORE = r"\.claude/+(\./+)*settings"


def _shell_linted(path):
    """Would hermes' post-write lint pass start a PROGRAM for `path`? (see SHELL_LINT_EXTS)"""
    ext = os.path.splitext(path)[1].lower()
    mod = sys.modules.get(LINT_MODULE)
    table = getattr(mod, "LINTERS", None) if mod is not None else None
    if not isinstance(table, dict):
        return ext in SHELL_LINT_EXTS
    return ext in table and ext not in (getattr(mod, "LINTERS_INPROC", None) or {})


def _search_globs(globs):
    """Model rg's ordered basename globs; unsupported syntax keeps the tree-wide floor.

    Deliberately do not guess at globset's path, brace, class, comment or escape syntax. A positive glob starts a
    whitelist; the last matching include/exclude wins. These are filters, never file operands.
    """
    rules = []
    for glob in globs or ():
        exclude = glob.startswith("!")
        pattern = glob[1:] if exclude else glob
        if not re.fullmatch(r"[A-Za-z0-9_.*?-]+", pattern) or pattern in (".", ".."):
            return None
        regex = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
        rules.append((exclude, re.compile(regex + r"\Z", re.DOTALL)))
    return rules


def _glob_excludes(rules, name):
    if not rules:
        return False
    excluded = any(not exclude for exclude, _ in rules)
    for exclude, regex in rules:
        if regex.fullmatch(name):
            excluded = exclude
    return excluded


def _walk_clean(policy, root, followlinks=False, globs=None):
    """content search reads reachable files under `root`: deny if any is on the secret floor or any followed
    symlink leaves read_root. Nested symlinks are skipped unless followlinks; callers vet explicit roots.
    Bounded (walk_cap entries); over the cap is a deny, not a skip. When following directory
    symlinks, deny repeated resolved directories so cycles cannot make the walk unbounded. Only proven file
    exclusions relax the floor: directory floors, escaping links, traversal bounds and cycles stay hard."""
    if not os.path.isdir(root):
        return None
    rules = _search_globs(globs)
    seen, visited = 0, set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=followlinks):
        if followlinks:
            real = os.path.realpath(dirpath)
            if real in visited:
                # Do not silently skip aliases: a descendant may match a floor only at its alias pathname.
                return "search tree holds a directory symlink cycle / repeated target (%s)" % dirpath
            visited.add(real)
        files = set(filenames)
        for name in dirnames + filenames:
            seen += 1
            if seen > policy.walk_cap:
                return "search tree too large to vet (> %d entries) - narrow the path" % policy.walk_cap
            p = os.path.join(dirpath, name)
            excluded = name in files and _glob_excludes(rules, name)
            if os.path.islink(p):
                if not followlinks:
                    continue                  # the search tool never reads this nested link or its target
                real = os.path.realpath(p)
                if not _inside(real, policy.read_root) or (_secret(policy, real) and not excluded):
                    return "search tree holds a symlink leaving the worktree / to a secret (%s)" % p
            if not excluded and _secret(policy, p, os.path.realpath(p) if followlinks else p):
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
    log = len(argv) > 1 and os.path.basename(argv[0]) == "git" and argv[1] == "log"
    range_next = end_of_opts = False
    for idx, arg in enumerate(argv[1:], 1):
        if idx in skip:
            continue
        if log and not end_of_opts:
            if range_next or arg.startswith("-L"):
                value = arg if range_next else arg[2:]
                range_next = arg == "-L" and not range_next
                if range_next:
                    continue
                arg = _git_log_range_path(value)
                if arg is None:
                    return "git log -L: expected a range and an unambiguous file operand"
            elif arg == "--":
                end_of_opts = True
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


def _git_log_range_path(value):
    # Git owns the address grammar (numbers, /regex/, :function-regex). We only need the file.
    # With exactly one separator after an optional function marker, Git either uses THIS file or
    # rejects the range. Reject extra colons rather than guess which belong to regexes/filenames.
    if not value or value.startswith("-"):
        return None
    parts = (value[1:] if value.startswith(":") else value).split(":")
    return parts[1] if len(parts) == 2 and all(parts) else None


def _no_index_paths(argv):
    if argv[1:3] != ["diff", "--no-index"]:
        return None
    paths = argv[3:]
    if paths[:1] == ["--"]:
        paths = paths[1:]
    return paths if len(paths) == 2 and all(p and not p.startswith("-") for p in paths) else None


def _git_hard(argv):
    """Return (reason or None, set of argv indexes holding commit-message text, which is not path-checked)."""
    msg_idx = set()
    if len(argv) < 2 or argv[1].startswith("-"):
        return "git global options before the subcommand are not allowed", msg_idx
    commit, add, log = argv[1] == "commit", argv[1] == "add", argv[1] == "log"
    skip_next = end_of_opts = range_next = False
    for idx, a in enumerate(argv[2:], 2):
        if skip_next:
            msg_idx.add(idx); skip_next = False; continue
        if end_of_opts:
            continue                     # after `--` every word is a pathspec (path-checked), never an option
        if range_next:
            range_next = False
            if _git_log_range_path(a) is None:
                return "git log -L: unsupported range or missing file", msg_idx
            continue
        if a == "--":
            end_of_opts = True; continue
        if log and a.startswith("-L"):
            if a == "-L":
                range_next = True
            elif _git_log_range_path(a[2:]) is None:
                return "git log -L: unsupported range or missing file", msg_idx
            continue                     # do not scan the range's filename as a short-option cluster
        if a == "--no-index" and _no_index_paths(argv) is not None:
            continue                     # exact two-operand form only; paths still checked below
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
    return ("git log -L: missing range/file" if range_next else None), msg_idx


def _sed_hard(argv):
    if len(argv) < 4 or argv[1] != "-n" or not SED_PRINT.fullmatch(argv[2]):
        return "sed: only `-n '<range>p' <file>` print reads are allowed"
    files = argv[3:]
    if files[:1] == ["--"]:
        files = files[1:]
    if not files or any(not f or f.startswith("-") for f in files):
        return "sed: expected plain file operands (no options or stdin)"
    return None


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
    original = argv
    assignments = []
    while argv and ENV_ASSIGN.match(argv[0]):
        assignments.append(argv[0])
        argv = argv[1:]
    if assignments:
        if policy.capability != "code" or not argv or not any(
                t == "Bash" and bash_rule_matches(s, argv, for_allow=True) for t, s in policy.test_allow):
            return "environment assignments may only prefix the granted test runner"
        for a in assignments:
            name = a.split("=", 1)[0]
            if name not in policy.test_env_allow and not TEST_ENV_LOCALE.fullmatch(name):
                return "test environment variable %r is not on the allow-list" % name
        why = _check_args_paths(policy, ["env"] + assignments, cwd, set())
        if why:
            return why
    for t, s in policy.deny:
        if t == "Bash" and (bash_rule_matches(s, argv, for_allow=False) or
                            bash_rule_matches(s, original, for_allow=False)):
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
    elif prog == "sed":
        why = _sed_hard(argv)
        if why:
            return why
        skip = {1, 2}                    # validated flags/program, not paths
    why = _check_args_paths(policy, argv, cwd, skip)
    if why:
        return why
    why = _companion_command(policy, argv, cwd, skip)
    if why:
        return why
    if prog == "git" and _no_index_paths(argv) is not None:
        # no-index accepts directories too: vet every descendant just like a content search.
        for p in _no_index_paths(argv):
            real = _resolve(p, cwd)[1]
            if os.path.isdir(real):
                why = _walk_clean(policy, real)
                if why:
                    return why
    return None


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
# patch: the plain single-file replace mode ONLY (mode absent or "replace"); V4A (`mode: "patch"`) is denied.
PATCH_KEYS = frozenset({"path", "old_string", "new_string", "replace_all", "mode"})
PATCH_MODE = "replace"
# hermes' OWN built-in tool guidance tells the model to use V4A patches for existing-file edits, which the
# check above denies; the hermes adapter appends this note to an editing worker's system prompt so the two agree
# (tests/test_hermes_gate.py checks the note against decide()). Adapter text, NOT a signed prompt.
PATCH_EDIT_RULE = (
    "\n\n## eph-gate edit rule (hermes harness)\n"
    "The `patch` tool is allowed ONLY in its plain replace mode: one file per call, with `path`, `old_string`, "
    "`new_string` (optionally `replace_all`), and `mode` omitted or set to \"replace\". V4A patches "
    "(`mode: \"patch\"`) are DENIED by the gate, whatever the tool's own guidance says; do not use them. To "
    "create a new file, or rewrite one wholesale, use `write_file`.\n")


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
            why = _walk_clean(policy, real, globs=[args["file_glob"]] if args.get("file_glob") else None)
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
            if set(args) - PATCH_KEYS or args.get("mode", PATCH_MODE) != PATCH_MODE:
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
# general: eph-gate floors FIRST, then the exec-gate judge
# ==========================================================================================================
# The general row has no allow-list (CC runs it under the auto classifier / skip-permissions + the _GENERAL_DENIED
# floor), so eph-gate's allow-list path can't decide it. policy.judge (set by adapters/hermes.gate_policy for
# general ONLY) routes the effectful tools to the configured exec gate (providers/; with EXEC_GATE unset that is
# `deny`, so every judged call is refused).
#   read_file / search_files  eph-gate only: secret floor + the imported Read deny rules + (content search) the
#                             tree walk. No judge round-trip: the floor + walk are the read policy.
#   write_file / patch        eph-gate floors (write_root = the worktree, no .git, secret floor, no shell-linted
#                             type), THEN the judge as Write / Edit.
#   terminal                  foreground only + workdir floor; if the lexer can split the command, every segment
#                             is checked against the imported Bash deny rules (_GENERAL_DENIED: git push/reset/...,
#                             rm, sudo, mcp.sh ...); THEN the judge as Bash with the EXACT command string.
#   anything else             unmapped-tool deny (process_manage and web_* included: web is not shipped).
# CEILING: the lexer refuses ~2/3 of real general commands (redirections, $(..), globs); for those the deny rules
# can't be applied here and the exec gate is the only judge. That is the same surface a CC general worker on
# skip-permissions has (its --disallowedTools match is CC's, its PreToolUse gate judges the rest).


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
    if policy.terminal_floor.search(cmd):
        return _deny("terminal: names a gate policy source or the gate dir (.claude settings / the exec gate's "
                     "own files), hard floor")
    cwd = policy.cwd
    if args.get("workdir"):
        wd, why = _check_read_path(policy, args["workdir"])
        if why:
            return _deny("terminal workdir: " + why)
        cwd = wd
    try:
        segs = split_command(cmd)
    except Deny:
        segs = []                                           # unparseable: the exec gate alone judges (CEILING above)
    for argv in segs:
        for t, s in policy.deny:
            if t == "Bash" and bash_rule_matches(s, argv, for_allow=False):
                return _deny("terminal: segment %r matches deny rule Bash(%s)" % (" ".join(argv)[:200], s))
    v = judge(policy, "Bash", {"command": cmd}, cwd)
    if v.allowed:
        v.modified_args = {"workdir": cwd}
    return v


def judge(policy, cc_tool, tool_input, cwd):
    """One exec-gate decision for a CC-shaped call, through the provider the policy names.
    -> Verdict (kind "judge" on a deny). Fail-closed: no judge block, an unknown provider, a provider that raises
    or returns anything but {"decision": "allow"} -> deny. sha = the provider's override token (Portcullis: the
    allow-once sha, byte-exact from the hook), only on a deny and only if it is a plain token."""
    j = policy.judge if isinstance(policy.judge, dict) else {}
    try:
        from . import providers
        prov = providers.get(j.get("provider"))
    except (ImportError, ValueError) as e:
        return _deny("%s: no usable exec gate (%s), fail-closed" % (cc_tool, str(e)[:200]), kind="judge")
    req = {"tool": cc_tool, "tool_input": tool_input, "cwd": cwd, "session_id": "eph-" + policy.nonce,
           "capability": policy.capability, "write_root": policy.write_root}
    try:
        out = prov.judge(req, j)
    except BaseException as e:          # noqa: BLE001 - a crashing gate is a deny, never a pass
        return _deny("%s: exec gate %s raised %s, fail-closed" % (cc_tool, prov.NAME, type(e).__name__),
                     kind="judge")
    if not isinstance(out, dict):
        return _deny("%s: exec gate %s returned no decision, fail-closed" % (cc_tool, prov.NAME), kind="judge")
    reason = str(out.get("reason") or "")
    if out.get("decision") == "allow":
        return Verdict(True, reason or "allowed by " + prov.LABEL, kind="judge")
    return Verdict(False, reason or "eph-gate: DENIED by %s (no reason given)" % prov.LABEL, kind="judge",
                   sha=providers.valid_token(out.get("token")))
