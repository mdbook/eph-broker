# Live routing overrides

To change an existing row's harness, backend, model or repo lock scope without restarting the broker, copy
`config/routing-overrides.example.json` to `config/routing-overrides.json` and edit it. This optional file
lives under `CONFIG_DIR` (the repo's `config/` directory by default); `ROUTING_OVERRIDES_FILE` selects a
different path. Unlike `routes.json`, it changes existing rows only, not triggers, prompts or tool grants.

The file accepts three optional top-level keys:

- `rows`: an object keyed by an existing route's row key (`row_id` when set, otherwise its `domain`, such
  as `code`). Each entry accepts only string fields `backend`, `adapter`, `model_pin` and `repo_lock_scope`. Adapters are
  `cc`, `hermes` or `codex`; a nonempty backend other than `claude` must resolve in the spawn registry.
  `model_pin` pins the model (for example `opus`) and skips fleet-mode model selection. Existing harness
  and capability restrictions still apply; selecting a harness here does not grant new tools.
  `repo_lock_scope` is `phases` (code/general default: serialize fetch/worktree cut, publish and removal,
  but allow independent edit/test turns up to the global cap) or `run` (whole-run single-flight).
  The wiki write route defaults to `run` to preserve its shared-branch behaviour; its publish-side
  rebase/retry remains enabled even if you select `phases`. Every scope uses the same per-repo lock,
  so cut and publish cannot overlap one another. The dashboard and `/runs/<key>` report `waiting`
  while a subject is queued on this lock or on the global turn cap, without changing coalescer state.
- `trusted_creators`: a list of ticket-creator usernames allowed to select non-Anthropic backends with
  `model:` labels. It replaces `MODEL_LABEL_TRUSTED_CREATORS`; an empty list trusts nobody. Usernames are
  matched case-insensitively.
- `row_concurrency`: an object mapping lowercase row groups to nonnegative integer caps. Entries override
  the corresponding `ROW_CONCURRENCY` values; omitted groups keep their environment defaults. A group
  covers its exact row key and all `<group>/...` rows. Zero pauses new workers in that group; pending
  work stays queued and existing workers are not stopped.

Omitted row fields keep their environment-backed defaults. An empty string explicitly clears `backend`
or `model_pin` (but is not a valid `adapter` or `repo_lock_scope`); removing the field restores its default. Removing the
overrides file restores all environment defaults.
The example pins the code row to Claude Code with `opus`, trusts nobody for non-Anthropic model labels,
and limits the code group to one worker; adapt those choices before using it.

The broker checks the file's modification time, size and inode when routing new work and reading the
live creator/concurrency settings. Write a replacement file and rename it atomically over the target to
avoid partial reads. `SIGHUP` forces a reread even when those attributes are unchanged; if the loader is
busy, the signal defers that reread to the next access rather than waiting on its lock. New workers use
the updated routing; already-running workers, including resumed turns, retain their launch route.

With [dashboard actions](configuration.md#dashboard-actions) on, **Settings > Workers** edits
`backend`, `model_pin` and `row_concurrency` in this file for you. It validates the whole result the same way,
writes a temporary file next to the target and renames it over, so the broker needs write access to the file
and its directory. A symlinked file is followed, and the temporary file goes next to the real target. Every other key in the file is kept as it is.

Reloading validates the whole file before publishing it. Malformed JSON, unknown rows or fields, invalid
adapters, unresolved backends and invalid creator/cap values reject the entire update. The broker keeps
its last good overrides (environment defaults if none loaded), logs the rejection without file contents,
and sends one operator alert per rejected file version. Successful reload logs identify changed top-level
keys and row names, not values. Keep credentials out of this file; backend validation reads registry
metadata, not credential files.
