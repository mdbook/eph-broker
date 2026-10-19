# Running workers on hermes-agent

By default every worker runs on the Claude Code harness (`cc`, `adapters/cc.py`). A built-in row (in code) or
the reviewer, through `MR_REVIEW_ADAPTER`, can switch to the
[hermes-agent](https://github.com/NousResearch/hermes-agent) harness instead by setting its adapter to `hermes`
(`adapters/hermes.py`), and a trusted `hermes` ticket label can do the same for one general ticket. Rows in
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

## The policy judge (general worker only)

Claude Code's general worker has the auto permission mode judging each action. On hermes, the `eph-gate`
plugin asks an external policy judge instead, for every shell command and file write the general worker makes.
No judge ships with the broker. The paths are fixed in `adapters/hermes.py` (`GENERAL_JUDGE`):
`~/.eph-judge/bin/gate-wrapper.sh`, run with the judge's own Python and directory under `~/.eph-judge/`. The
wrapper gets a Claude Code `PreToolUse` hook event on stdin and must answer like a Claude Code hook
(`hookSpecificOutput.permissionDecision` of `allow`) within 5 seconds. Anything else is a deny.

Without a judge, every general worker action on hermes is denied, so treat the general worker on hermes as
unsupported outside the original setup. The code worker and the reviewer don't use the judge.

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
