# Worker test runners and worktrees

The code worker's test runner is resolved per repo, not per route, so the one signed `code/change-proposer`
prompt serves every code repo safely. In order:

1. An exact entry in `CODE_REPO_RUNNER_SEEDS` or `CODE_REPO_RUNNERS` (`<absolute-repo-path>=<runner-key>,...`;
   the latter overrides the former) wins. A key is a menu entry (`pytest`, `npm`, `go`, `venv`, `bash-n`,
   `bash-n+unittest`, `ansible-syntax`, `repo-test`, `none`), never a raw command.
2. With repo inference on (`REPO_INFER_ENABLED=1`), a catalog repo that reaches the worker only through
   inference and has no entry from step 1 gets `none`: auto-detection runs only for a repo on the explicit
   `CODE_REPO_ALLOWLIST`.
3. For an allowlisted repo that's still unmapped, a top-level marker file picks a runner (`package.json`
   only -> `npm`; a Python marker keeps the unchanged default).
4. Anything left over falls back to the route's default runner.

So today, every catalog repo you haven't mapped with `CODE_REPO_RUNNER_SEEDS` / `CODE_REPO_RUNNERS` (and
that isn't allowlisted with an auto-detected marker) resolves to `none`. That's not a failure case: the
worker's prompt says so explicitly. With no runner granted, the worker builds the change from reading the
code, commits it, and states in its summary that the change is untested locally and the MR pipeline (your
CI) verifies it, with its reasoning. It never invents a test command and never reports a result it didn't
get. Map a repo in `CODE_REPO_RUNNER_SEEDS` once you want the worker to run that repo's own tests before
committing.

Code workers run the repo's tests on the untouched base before editing, then repeat them after the
change. New/changed tests must pass, and the failing set after must be a subset of the failing set
before. Existing failures do not stop a commit or MR. The MR summary includes both pass/fail counts
and added/resolved failing test IDs. Environment-only gaps preserve the work with an explicit account
of what could not run. Verification is blocked only when the baseline cannot execute at all and no
other permitted verification is possible; the committed branch is still preserved.

For Python repos with test dependencies, set `CODE_TEST_DEPS_CONFIG` to an operator-owned JSON map of
absolute repo paths to package requirements (see `config/code-test-deps.example.json`). A mapped repo
uses the existing `venv` runner; the broker creates `<repo>/.venv` and installs that list before starting
the worker. Include `pytest` and every required test dependency, such as `websocket-client` for a
WebSocket plugin suite. The worker cannot install packages. Setup failures are reported in its brief
as verification gaps. Unmapped repos keep their existing runner selection.

Code and issue worktrees default to short roots `~/.cache/ew/c` and `~/.cache/ew/i`. Long subject keys
use bounded hashes. Each code worker gets a private writable `TMPDIR` next to its worktree, shared
across its turns and removed when the worker exits. Keep overridden worktree roots short for socket tests.
This bounds the worker root, not every nested socket path. Pytest `tmp_path` adds username and test-name
directories that can still exceed the AF_UNIX limit; socket tests must keep their full paths short.

## Companion sources

An operator can configure per-repo [companions](configuration.md#companion-source-repositories).
After the code worktree cut and before any worker turn, the broker shallow-fetches these sources into
`<worktree>/.companions/<as>`. The reserved directory is git-ignored using `info/exclude`, so ordinary
worker commits and broker patch/WIP saves do not include it. Existing files or symlinks at that root
are a hard collision, not a directory to overwrite.

All three harnesses export `COMPANION_<UPPERCASE_AS>` to worker processes and tool shells, including
resumed turns. The brief's `companions` list supplies the literal path, repo, requested ref and full
resolved `sha`. Use that path for reads or as input to the granted test runner. For tests expecting
another environment name (such as `CORE_SRC`), configure that name in `EPH_GATE_TEST_ENV_ALLOW` and
prefix only the granted runner with a literal path assignment; this feature does not grant a new runner
or shell expansion syntax.

Companion files remain subject to the normal secret-file and symlink confinement floors. Edit/Write,
patch and mutating command operands targeting companions (also through symlink aliases) are denied.
CC gets an active deny hook even on adjudicated turns; Codex and Hermes use the shared eph-gate floor.
On companion-enabled CC turns, shell directory changes are refused; use literal paths from the brief.
Commands the shell lexer cannot safely parse are also refused, even without a literal companion path.
Source files/directories also have write permission bits removed. This is tool-call confinement, not
an immutable mount: as with existing worker tests, arbitrary same-uid repo code executed by a granted
runner can undo permissions. Tests needing generated files should write in the primary worktree or
private `TMPDIR`, not in a companion.

Before launch, the broker records `key`, `run_id`, primary `cut_sha` and the pinned companion list as
`run:<run_id>` in its durable state journal, and logs the same record (also when no state journal is
configured). It survives worktree teardown. Warm turns reuse the original snapshot, even if the remote
ref moves. Teardown restores directory write bits without following symlinks and removes the snapshots
with their worktree.

## Worker SSH configuration

Every ephemeral launch goes through `CCAdapter._one_turn`: the `cc` branch launches Claude Code;
the `codex` and `hermes` branches delegate to their harness's `one_turn`. All three call the shared
`worker_env` before starting the harness or its gate probes. This includes the `code`, `general`,
`infra` (infra-eph), and `infra/general` (infra:general) rows, plus configured rows and warm turns.

That shared launcher creates a private `ssh` executable beside the worktree and prepends its directory
after all route/backend/MCP PATH overrides. It runs `/usr/bin/ssh` with an explicit `-F` pointing at the
broker user's `$HOME/.ssh/config`, or `/dev/null` if that file is absent. OpenSSH therefore does not load
the implicit `/etc/ssh/ssh_config` or its `ssh_config.d` includes, whose root ownership can appear invalid
inside a uid-mapped sandbox. A pre-installed service PATH shim is no longer required. User host aliases,
identity settings and normal host-key checking remain intact. As with any explicit SSH config, the user
config must not itself Include the system config tree. No credential contents are copied.

The launcher directory and executable are mode 0700, reused across a worker's turns, and removed at
teardown (with a finalizer backstop). They live outside the worker's tool-write scope, like harness homes.
No extra writable sandbox root, network permission, tool grant or secret-floor exception is added.

The tool shell must preserve that PATH too. Claude Code gets `CLAUDE_BASH_NO_LOGIN=1`; Codex gets an
explicit shell-environment PATH with login shells and shell snapshots disabled; Hermes restores PATH
through its managed terminal init file after the login snapshot. These settings are rebuilt on resumed
turns as well. Hermes' venv-runner PYTHONPATH remains terminal-only.
All three also use a private shell init file (`BASH_ENV` and `ZDOTDIR/.zshenv`) to put the managed SSH
launcher first after shell startup. Non-login zsh still reads `.zshenv`, so pinning the environment PATH
alone does not prevent a user startup file from replacing it.

The regression matrix launches each of the four rows on each harness, on seed and resumed turns,
without a site shim. Offline `ssh -vvv -G` checks assert that user host settings survive, host-key checking
is unchanged, and no system SSH config is read, including after Hermes' login snapshot resets PATH.
These tests need OpenSSH but no credentials, bridge traffic or model login. Live bridge connectivity
and the real harness sandbox still need deployment validation. No service drop-in is modified.
