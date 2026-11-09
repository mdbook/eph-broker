# Contributing

Thanks for helping. The broker is standard library only, so setup is short.

## Setup

You need Linux, git and Python 3.9 or newer.

```sh
git clone https://github.com/mdbook/eph-broker
cd eph-broker
BROKER_SITE_ENV=/dev/null BROKER_CONFIG=config/demo/broker.example.json python3 app.py
```

That starts a local preview on `127.0.0.1:8787` with no secrets or agent CLI. See the [README](README.md#quick-start).

## Tests

```sh
sh tools/check.sh
```

This is the test gate and needs no real agents or network. Run it before you open a pull request.

## Refreshing screenshots

With Python Playwright and its headless Chromium already available, run:

```sh
python3 scripts/screenshots.py
```

This writes 1400x900 PNGs to `docs/images/`: `dashboard-workers.png` (Runs),
`dashboard-fleet.png` (Fleet), and `dashboard-now.png` (Now overview). Use
`--output DIRECTORY` for a different destination. Playwright is optional developer
tooling, not a broker dependency; the command reports missing tooling without
installing anything.

The script starts its own read-only broker with `config/demo/broker.example.json`
on an automatically assigned loopback port. It ignores inherited broker settings,
uses only synthetic demo workers, tickets and agents, fixes the clock and browser
settings, and removes its temporary state and stops its broker and browser on exit.
It never accepts a live broker URL or starts agent processes. Review the images
before committing them. Use the same Playwright/Chromium version for pixel-level
comparisons across refreshes.

## Pull requests

- Keep each change small and focused, and add or update a test for new behavior.
- Don't add third-party dependencies. The broker stays standard library only.
- Update the docs when you change a setting, endpoint or default. [docs/settings.md](docs/settings.md) lists every environment variable.
- Keep site-specific values out of the code. They belong in config, with neutral examples in `config/site.example.env`.
- Never commit secrets, tokens or live config.
- [AGENTS.md](AGENTS.md) describes the repo layout and conventions.
