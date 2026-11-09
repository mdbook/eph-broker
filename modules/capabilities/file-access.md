## File access by harness

Use the tools exposed by your actual harness, not another harness's tool names. This section describes file access, not new grants: the worker's read/write scope and secret-file denies still apply. Unsupported harness/capability combinations are refused by the broker. References to Bash below mean the selected harness's shell tool.

- **Claude Code:** read/search with `Read` / `Grep` / `Glob`. In writable roles, edit with `Edit` / `Write` (or `MultiEdit` if exposed).
- **Codex:** there are no `Read` / `Grep` / `Glob` / `Edit` / `Write` tools. Read through `exec_command` (or `Bash` if exposed), using the EXACT literal worktree prefix supplied by the harness: `cd <worktree> || exit 1; `. Replace `<worktree>` with your actual cwd, not the angle-bracket placeholder. For example, make ONE call per command:
  - `cd <worktree> || exit 1; head -n 200 path/to/file`
  - `cd <worktree> || exit 1; cat path/to/file`
  - `cd <worktree> || exit 1; rg -n PATTERN path/to/directory`
  - `cd <worktree> || exit 1; ls path/to/directory`
  These are file reads, NOT general shell execution; they remain available in read-only review roles. Do not supply `workdir` / `cwd` arguments. The required prefix is the ONLY exception to the no-chaining rule: no second `cd`, pipes, redirection, substitutions, or extra commands. Scope searches to the intended directory, not the whole repo. `git show` / `git ls-files` (including forms with paths) are NOT granted file-reading routes; do not use them as substitutes. In writable roles, use `apply_patch` for edits, not shell writes.
- **Hermes:** read with `read_file` and search/list with `search_files`. In writable roles, use `write_file` / `patch`. Read-only reviewers have no terminal or file-write grant.

Do not stop merely because Claude Code tool names are absent: use your harness's file-reading route above. If that route is denied, report the exact denial; do not bypass it.
