# AGENTS.md

A guide for coding agents working on this repo. Humans start at [README.md](README.md) and
[docs/README.md](docs/README.md).

## What this is

eph-broker is a standard-library Python service. It takes a ticket assigned to a bot, has a short-lived
coding agent make the change in a throwaway git worktree, pushes the result to a fresh branch, opens a GitLab
merge request, and has a read-only reviewer agent post a verdict on it. Workers run on a harness (an agent
CLI the broker drives): Claude Code by default, hermes-agent and codex as options.

- Python 3.9+, standard library only. There are no Python dependencies and no `pyproject.toml` or
  `requirements.txt`. Do not add one, and do not import anything outside the standard library.
- Linux, git, and tmux (tmux only for the interactive-agent row).
- License: GPL-3.0-or-later.

## Layout

| Path | What it holds |
|---|---|
| `app.py` | Entry point: the HTTP server (`127.0.0.1:8787`), webhooks, dashboard endpoints |
| `coalescer.py` | Batches a burst of events on one subject into one worker |
| `routing.py` | The routing table: which label, assignee or event source starts which worker class |
| `adapters/` | One adapter per harness (`cc.py`, `hermes.py`, `codex.py`, `stub.py`); tool grants live in `adapters/cc.py` |
| `adapters/hermes_gate/` | The `eph-gate` hermes plugin and its exec-gate providers |
| `envelope.py` | Parses the worker's final JSON result and the allowed statuses per capability |
| `prompts.py` | The sha256 hash gate checked before every worker start |
| `worktree.py` | Per-subject git worktrees; strips repo instruction files from them |
| `echo_guard.py`, `provenance.py` | Drop events the broker caused and events from bot accounts |
| `gitlab_mr.py`, `forge.py`, `cd.py` | GitLab MRs, notes, and the optional auto-merge and deploy hand-off |
| `trackers/` | Tracker adapters (Vikunja, GitLab) |
| `modules/` | Markdown prompt fragments: `role/`, `scopes/`, `capabilities/`, `constraints/`, `outputs/` |
| `modules/site/` | Site overrides that replace a generic module of the same path at render time |
| `workers/<domain>/<class>.json` | One manifest per worker class, listing modules in order |
| `prompts/<domain>/<class>.md` | Rendered prompts, committed so they can be read and diffed |
| `approved-prompts.json` | The signed sha256 of each rendered prompt |
| `config/` | JSON config and `*.example.*` templates |
| `tools/` | `check.sh` (the test gate), `render-prompts.py`, other helper scripts |
| `tests/` | Self-test suites and `tests/fixtures/` |
| `packaging/` | RPM spec and systemd units |
| `integrations/n8n-nodes-eph-broker/` | n8n nodes (JavaScript, with their own `node --test` suite) |
| `docs/` | User documentation, indexed in `docs/README.md` |

## Build and test

There is no build step. The full gate, the same one CI runs:

```sh
sh tools/check.sh
```

It runs `python3 coalescer.py --selftest`, each suite under `tests/` as a script, then
`python3 tools/render-prompts.py --check`, and the n8n node tests if `node` is installed. It needs no
network, tracker, GitLab or real agent.

Run one suite while you iterate:

```sh
python3 tests/test_routes.py
```

Each test file is a plain script with stdlib `assert`s and a `__main__` runner, not a pytest suite. Run as a
script, `tests/test_broker.py` forces its own fixture overlays (`tests/fixtures/site.test.env`,
`provenance.test.json` and the rest) so the run never reads the deployment's `config/site.env` or
`config/provenance.json`. Prefer the script form, and treat `sh tools/check.sh` as the result that counts.

Run the broker locally with nothing attached (after copying `config/provenance.example.json` to
`config/provenance.json` and `config/site.example.env` to `config/site.env`):

```sh
EVENTS_LOG=$PWD/events.jsonl python3 app.py
curl -s 127.0.0.1:8787/health
```

## Conventions

- Keep to Python 3.9 syntax. CI tests on the `python:3.9` image and the RPM targets EL9's python3.9, so no
  `match`, no `X | Y` type unions at runtime, no 3.10+ standard library calls.
- Tests are stdlib `assert` functions named `test_*` in the existing `tests/test_*.py` files. Add to the
  suite that covers the module you change. A new suite file has to be added to `tools/check.sh` or it never
  runs.
- Settings come from environment variables with defaults in code. A new variable goes in
  [docs/settings.md](docs/settings.md); neutral example values go in `config/site.example.env`.
- Docs voice: plain and direct, no em dashes. Use example hosts (`gitlab.example.com`) and documentation
  address ranges (192.0.2.0/24, 198.51.100.0/24), never a real host, address or account.
- Test data uses ticket ids in the 9000 to 9999 range.

### Prompts are signed

A worker class only starts when its rendered prompt hashes to the value in `approved-prompts.json`. So:

1. Edit a module under `modules/` (or a manifest under `workers/`), never the rendered file in `prompts/`.
2. Re-render: `python3 tools/render-prompts.py`.
3. Commit the module, manifest and rendered prompt together. `render-prompts.py --check` fails on drift.
4. Changing a prompt changes its hash, and that class stops starting until a human reads it and signs the
   new hash. Say so in your summary. Do not update `approved-prompts.json` yourself unless the task asks
   for it: signing is the operator's review step.

Details in [docs/prompts.md](docs/prompts.md).

### Gotchas

- `worktree.py` removes `CLAUDE.md`, `AGENTS.md` and `*-AGENT.md` from the root of every worker worktree
  and leaves them out of the broker's own commits, and the hermes adapter runs with `--ignore-rules`. This
  file guides agents working on eph-broker itself; the broker's workers never read it, and a broker worker
  working on this repo sees it deleted in its worktree. That deletion is expected; do not commit it.
- Tool grants live in `adapters/cc.py` and the routing rows in `routing.py`. A prompt that describes a tool
  does not grant it.
- Some code carries marked blocks for the public export: a begin marker, the live lines, an optional public
  marker followed by a commented-out public version, and an end marker. The public tree keeps the public
  version instead of the live lines. Copy the marker syntax from an existing block (`provenance.py` has
  one), keep the markers balanced, and keep every line of the public version commented out.

## Release and deploy

- The version lives in `VERSION`.
- CI (`.gitlab-ci.yml`) runs `sh tools/check.sh` on push and on merge requests, then builds the RPM from
  `packaging/eph-broker.spec` and install-smokes it on EL9. On the default branch only, it uploads the RPM
  and a sha256 checksum file to the project's generic package registry.
- Deploy is not a CI job. A person installs or upgrades the RPM, or pulls the source and restarts the
  service. The optional CD module (`cd.py`, off by default) can merge reviewed MRs and hand off a deploy
  request, see [docs/cd-deploy-paths.md](docs/cd-deploy-paths.md).
- Merging stays with a human. Do not push, merge or tag.

## What not to touch

Unless the task is specifically about them:

- Deployment config: `config/site.env`, `config/provenance.json`, `config/inbox-accounts.json`,
  `config/deploy-paths.json`, `config/pool-vikunja-ids.json` and
  `packaging/eph-broker.service.d/05-site.conf`. These hold one site's settings. Change the
  `*.example.*` templates instead.
- `approved-prompts.json` (see above) and the rendered files under `prompts/`, except as the output of
  `render-prompts.py`.
- `.gitlab-ci.yml` and `packaging/`. In `tools/check.sh`, only add a line for a new suite.
- `modules/site/`, which is site wording, not the generic prompts.
- Secrets. The broker reads its tokens from its environment (`GITLAB_MR_TOKEN`, `GITLAB_WEBHOOK_TOKEN`,
  `BROKER_HMAC_SECRET`); never write a value into the repo, a test or a commit message.
- Runtime state: `events.jsonl`, `worktrees/` and the gate denial log are gitignored and never committed.
