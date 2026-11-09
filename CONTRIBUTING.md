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

## Pull requests

- Keep each change small and focused, and add or update a test for new behavior.
- Don't add third-party dependencies. The broker stays standard library only.
- Update the docs when you change a setting, endpoint or default. [docs/settings.md](docs/settings.md) lists every environment variable.
- Keep site-specific values out of the code. They belong in config, with neutral examples in `config/site.example.env`.
- Never commit secrets, tokens or live config.
- [AGENTS.md](AGENTS.md) describes the repo layout and conventions.
