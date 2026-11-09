# Running workers on hermes-agent

By default every worker runs on the Claude Code harness (`cc`, `adapters/cc.py`). A built-in row (in code) or
the reviewer, through `MR_REVIEW_ADAPTER`, can switch to the
[hermes-agent](https://github.com/NousResearch/hermes-agent) harness instead by setting its adapter to `hermes`
(`adapters/hermes.py`), and a trusted `hermes` ticket label can do the same for one general or code ticket. Rows in
`config/routes.json` can't switch. Hermes is only allowed with a non-Claude backend, so you also need a
[backend registry](configuration.md#models-and-backends) entry for it to run on. Its tool calls go through the `eph-gate` plugin
(`adapters/hermes_gate/`), which applies the same allow and deny rules as the Claude Code grant.
[adapters.md](adapters.md) compares the two harnesses and covers adding another;
[security.md](security.md#with-the-hermes-harness) has what changes for security.

## Installing

The broker uses its own hermes install, never a shared one. Run these as the broker's user:

```sh
git clone https://github.com/NousResearch/hermes-agent ~/.local/share/eph-hermes/src
git -C ~/.local/share/eph-hermes/src checkout <a commit you have tested>
uv venv ~/.local/share/eph-hermes/venv
scripts/hermes-venv-reinstall.sh            # from the broker checkout, see below
```

The script installs exactly what the checkout's `uv.lock` pins. No hermes release is pinned in this repo, so
pin the checkout to a commit you've tested and upgrade it on purpose. `HERMES_BIN` (default
`~/.local/share/eph-hermes/venv/bin/hermes`) points the broker at the binary, and `HERMES_HOME_ROOT` (default
`~/.cache/eph-hermes-homes`) is where each worker gets a private config home.

## Test-runner environment assignments (hermes and Codex)

For code workers, eph-gate allows environment assignments only before the granted test runner.
The default name allow-list is `PORTCULLIS_PYTHON`, `PYTHONDONTWRITEBYTECODE`, `NO_COLOR`, `CI`, `TERM`,
`LANG` and `LC_*` locale variables. Every other name is denied, including unknown runtime configuration
knobs. Values still undergo the existing read-root and secret-file checks; the runner and its arguments
still undergo all normal checks.

Set `EPH_GATE_TEST_ENV_ALLOW=SITE_TEST_MODE,SITE_TEST_SHARD` in the broker's site settings to add reviewed,
exact, case-sensitive names (no wildcard patterns). These ride in the per-turn policy's `test_env_allow`
list, not the worker's environment. Only add benign test flags: adding a runtime/loader configuration name
would explicitly trust that variable to change how tests execute. Invalid names refuse the code turn.

## Plugging in your own exec gate (general worker only)

Claude Code's general worker has the auto permission mode judging each action. On hermes the general worker has no
allow-list, so after eph-gate's own floors (the capability grant, the secret floor, worktree-confined writes, the
general deny list) every `terminal`, `write_file` and `patch` call also needs a yes from an **exec gate**. The broker
doesn't care which one. You pick it with `EXEC_GATE` in your site config:

| `EXEC_GATE` | what judges | you set |
|---|---|---|
| unset or `deny` | nothing: every judged call is denied. The safe default | nothing |
| `none` | nothing: eph-gate's floors only. You have to ask for this explicitly | nothing |
| `exec` | your program, in any language | `EXEC_GATE_COMMAND` |
| `portcullis` | Portcullis, the reference gate, through its hook-mode wrapper | `EXEC_GATE_PORTCULLIS_WRAPPER`, `_HOOK`, `_PYTHON`, `_DIR`, optionally `_PROJECT_DIR` |

Shared knobs: `EXEC_GATE_TIMEOUT` (seconds per decision, default 5.0), `EXEC_GATE_INHERIT_ENV` (env names the gate may
inherit, default `TMPDIR,LANG,TZ`) and `EXEC_GATE_PROTECTED_TERMS` (extra literal names a general terminal command
may never mention; `portcullis` already protects its own home dir without it). `config/site.example.env` has a
commented block for all of them.

**The `exec` contract.** The broker runs `EXEC_GATE_COMMAND` once per judged call, with the call's cwd and a minimal
env (`PATH=/usr/bin:/bin`, `HOME`, plus `EXEC_GATE_INHERIT_ENV`). It writes one JSON object to stdin:

```json
{"tool": "Bash", "tool_input": {"command": "make test"}, "cwd": "/path/to/worktree",
 "session_id": "eph-<nonce>", "capability": "general", "write_root": "/path/to/worktree"}
```

`tool` is Claude Code shaped (`Bash`, `Write`, `Edit`) so a gate written for Claude Code hooks reads it as-is.
Print `{"decision": "allow", "reason": "..."}` and exit 0 to allow. Anything else is a deny: `"deny"`, a non-zero
exit, a timeout, no output, output that isn't a JSON object.

**Writing a provider module** (when `exec` isn't enough, say you want an override token like Portcullis'
allow-once). Add a stdlib-only module to `adapters/hermes_gate/providers/` and register it in that package's
`PROVIDERS`. The package docstring is the full interface: `NAME`, `LABEL`, `FLOOR_FILES`, `FLOOR_TERMS`,
`config(env)` (broker side, raise `ValueError` on a bad config) and `judge(request, cfg)` (runs inside the worker's
harness, returns `{"decision", "reason", "token"}`), plus the optional `floor_terms(cfg)` (names your gate's own
state, so the floor protects it with no extra site key) and `override_hint(cfg)`. `providers/portcullis.py` is the
worked example.

**It fails closed, every way.** An unknown `EXEC_GATE` refuses every hermes turn. A judged turn with missing or
relative paths is refused before it starts. A provider that crashes, hangs or returns garbage is a deny. A gate
going missing never turns into an open door. The code worker and the reviewer don't use the exec gate.

Claude Code workers aren't routed through this: they run under Claude Code's own permission modes plus whatever
`PreToolUse` hooks your Claude settings load, so a gate that speaks Claude Code hooks (Portcullis does) plugs in
there directly.

## The hermes venv must be a non-editable install

The broker uses a private hermes venv (`~/.local/share/eph-hermes/venv`) built from a checkout
(`~/.local/share/eph-hermes/src`).

A check in `adapters/hermes.py` (`_PATH_GUARD`) refuses every hermes turn whose interpreter
has a relative `sys.path` entry or an `__editable__` finder. An editable install (`uv sync` or `pip install -e`,
uv's default) adds exactly that kind of entry. With the worker's worktree as the current directory, it would let
a file the worker planted shadow hermes itself.

A routine `uv sync` in the checkout without `--no-editable` quietly makes the venv editable again.
From then on every hermes turn is refused. Because it fails closed, it looks like a hermes outage rather than
a security hole. Nothing keeps the venv non-editable automatically: run the reinstall again every time you re-sync the
checkout.

## Reinstalling

Run as the broker user:

```sh
scripts/hermes-venv-reinstall.sh [SRC_DIR] [VENV_DIR]
#   SRC_DIR  defaults to ~/.local/share/eph-hermes/src   (the hermes checkout; left as-is apart from build/)
#   VENV_DIR defaults to ~/.local/share/eph-hermes/venv  (the broker's private hermes venv)
```

The script runs `HERMES_NIX_BUILD=1 UV_PROJECT_ENVIRONMENT=$VENV_DIR uv sync --no-editable --frozen --inexact
--extra anthropic` in `SRC_DIR`, then removes `SRC_DIR/build/`:

| Flag | Why |
|---|---|
| `HERMES_NIX_BUILD=1` | Without it, upstream's `setup.py` refuses `bdist_wheel`, so `--no-editable` can't build. Build time only. |
| `--frozen` | Install exactly what `uv.lock` pins, no re-resolve. |
| `--inexact` | Keep packages that aren't in the lock (e.g. boto3 for the bedrock and s3 backends). A plain sync removes them. |
| `--extra anthropic` | Installs the optional `anthropic` SDK that the adapter's `anthropic_messages` transport needs. |

## Checking it worked

Both must pass:

1. `direct_url.json` reports `editable: false`. This must print `False`:

   ```sh
   python -c "import json, glob; print(json.load(open(glob.glob('$HOME/.local/share/eph-hermes/venv/lib*/python*/site-packages/hermes_agent-*.dist-info/direct_url.json')[0]))['dir_info']['editable'])"
   ```

2. There is no `__editable__*` file under the venv's `site-packages`.
