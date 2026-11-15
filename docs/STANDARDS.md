# Code standards

The rules this repo already follows. Each one is checkable against the code. Where the code is mixed, the
dominant pattern is the rule and the exceptions are listed under [Known drift](#known-drift). Layout,
gotchas and the "what not to touch" list live in [AGENTS.md](../AGENTS.md); this page does not repeat them.

## Language and runtime

- Python 3.9 syntax and standard library only. No `match`, no `X | Y` unions at runtime, no 3.10+ stdlib calls.
  CI tests on `python:3.9` and the RPM targets EL9's python3.9.
- No third-party imports. The one optional import is `yaml` in `site_env.py`, guarded so the broker runs
  without it (the RPM only `Recommends: python3-pyyaml`).
- No `pyproject.toml`, `requirements.txt` or build step. Do not add one.
- The dashboard is one HTML/CSS/JS string (`dash.PAGE` in `dash.py`). Plain browser JavaScript, no framework,
  no bundler, no external fonts or assets.
- The n8n nodes under `integrations/n8n-nodes-eph-broker/` are the only other language (JavaScript, tested
  with `node --test`).

## Layout

- Top-level modules, one concern each (`routing.py`, `coalescer.py`, `envelope.py`, ...). Packages only for
  pluggable families: `adapters/` (one file per harness), `trackers/` (one file per tracker).
- New harness: a class in `adapters/<name>.py` implementing `adapters/base.py`. New tracker: a module in
  `trackers/` registered through `trackers/__init__.py`.
- Prompt fragments live in `modules/`, manifests in `workers/<domain>/<class>.json`, rendered output in
  `prompts/`. Never hand-edit `prompts/`.
- Config templates are `config/*.example.*`. Live site files sit beside them and are never edited in a change.

## Naming

- `snake_case` for modules, functions and variables. `CamelCase` for classes (`CCAdapter`, `Route`).
  `UPPER_SNAKE` for module constants and environment variable names.
- A leading underscore marks module-private helpers (`_env_flag`, `_env_list`).
- Loggers are named `eph-broker.<module>`: `log = logging.getLogger("eph-broker.poster")` at module top.
- Test files are `tests/test_<area>.py`; test functions are `test_<behavior>`.
- Test ticket ids are in the 9000 to 9999 range.

## Module shape

- Every module opens with a docstring whose first line is `<file>.py: <what it does>.` followed by the
  why: invariants, defaults, what it refuses. Keep that header current when behavior changes.
- No type annotations. The code base has almost none; match it.
- Wrap at about 120 columns (the 99th percentile line is 122). Long string literals may run past it.
- Comments explain why, not what. Ticket references in comments use `(ticket N)` or `#N`.

## Configuration

- Settings come from environment variables with a neutral default in code. Most features default off and fail
  closed until set.
- A deployment's values go in `config/site.env` (or `config/broker.yaml` / `broker.json`), loaded by
  `site_env.py`. Code defaults stay neutral: no hosts, accounts or paths from one site.
- Every new variable gets a line in [settings.md](settings.md) and, if it needs a value, a neutral example in
  `config/site.example.env`.
- Secrets are read from the environment by variable name at runtime. Never write a value into the repo, a
  test, a log line or a commit message.
- JSON config under `config/` reloads on SIGHUP. A bad file keeps the last good copy and logs an error.

## Error handling and logging

- Fail closed. Unknown or invalid input means refuse and log, not guess. An invalid setting falls back to the
  safe value and logs at `error` (see the HMAC mode check in `app.py`).
- Catch broad exceptions only at I/O and process boundaries (HTTP calls, subprocesses, file reads, reload),
  log them, and keep the broker running. Do not swallow errors silently.
- HTTP handlers return `(status, dict)`. Errors are `{"ok": False, "error": "<plain sentence>"}` with a real
  status code (400 bad input, 404 unknown, 409 state conflict). Success includes `"ok": True`.
- Log through the module logger with `%s` arguments, never f-strings: `log.warning("config reload: %s", e)`.
  The root format is `%(asctime)s %(message)s` (set in `coalescer.py`).
- Log lines start with a short lowercase subject, a colon, then the fact: `config reload: broker.yaml changed`.
  Name the setting or file involved. Never log a secret value; log the variable name and `set`/`unset`.
- Operator-visible runtime state goes to `EVENTS_LOG` as JSON lines, not to the text log.

## Tests

- The gate, the same one CI runs:

  ```sh
  sh tools/check.sh
  ```

- One suite while you iterate: `python3 tests/test_routes.py`.
- Tests are plain scripts: stdlib `assert` in `test_*` functions plus a `__main__` runner. No pytest, no
  network, no real agent or tracker. Use the fixtures in `tests/fixtures/`.
- Add tests to the existing suite for the module you change. A new suite file needs a line in
  `tools/check.sh` or it never runs.
- Prompt drift is a test: `python3 tools/render-prompts.py --check` fails if `prompts/` is stale.

## Lint and format

- There is no linter or formatter in the repo or CI. Match the surrounding code by hand.
- `python3 -m py_compile` on the installed files runs in the CI package job, so the tree must compile on 3.9.

## Docs

- A change to a setting, endpoint or default updates the matching page under `docs/` in the same change.
- Plain, direct voice. No em dashes. Example hosts (`gitlab.example.com`) and documentation address ranges
  (192.0.2.0/24, 198.51.100.0/24), never a real host, address or account.

## Commits and merge requests

- Commit subject: `type(scope): summary`, lowercase, imperative, no trailing period. `fix` is by far the most
  common type; others in use are `feat`, `test`, `docs`. Scope is the module or area (`cc`, `dash`, `routing`,
  `hermes-gate`). Add the ticket as `(ticket N)` at the end when there is one.
- One topic branch per change, one MR into `main`. Keep each MR small and focused, with tests for new
  behavior.
- A human merges. MRs land as merge commits. Do not push to `main`, merge or tag yourself.
- A prompt change changes its hash, and that worker class stops starting until a person signs the new hash in
  `approved-prompts.json`. Say so in the MR description.

## Release and deploy

- The version lives in `VERSION`.
- CI (`.gitlab-ci.yml`): `test` runs `sh tools/check.sh`; `package` builds the RPM from
  `packaging/eph-broker.spec` and install-smokes it on EL9; `publish` uploads the RPM and a sha256 checksum
  file to the project's package registry, default branch only.
- Deploy is not a CI job. A person installs the RPM or pulls the source and restarts the service. The optional
  CD module (`cd.py`, off by default) is described in [cd-deploy-paths.md](cd-deploy-paths.md).
- Keep public-export marker blocks balanced (see [AGENTS.md](../AGENTS.md)).

## Known drift

1. Two test files bypass the plain-script rule: `test_dispatch_priority.py` and `test_move_to_front.py` run
   through `unittest discover`, and `test_modhost.py` is documented as a pytest suite.
2. `tests/test_modhost.py` is not listed in `tools/check.sh`, so CI never runs it. (`test_codex_canary.py`
   is opt-in by design and stays out.)
3. Commit types are mixed: `test` and `tests` both appear, and some subjects use an area as the type
   (`dash:`, `codex:`, `mr-review:`) instead of `type(scope):`.
4. Logger names mostly use the module name with underscores, but `eph-broker.write-grant` uses a hyphen.
5. Some module docstrings skip the `<file>.py:` prefix (`broker_state.py`, `coalescer.py`, `dispatch.py`,
   `gitlab_mr.py`, `modhost.py`, `repo_companions.py`, `routing_overrides.py`, `sandbox.py`, `site_env.py`,
   `test_environment.py`).
6. A handful of log messages and docstrings still use one deployment's vocabulary instead of neutral terms.
7. `Dockerfile` uses `python:3.12-slim` for the local preview while CI and the RPM target 3.9. The 3.9 rule
   still applies to code.
