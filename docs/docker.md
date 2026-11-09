# Docker preview

The root `compose.yaml` builds the broker locally. It is a dashboard and HTTP-service preview, not a
worker-enabled image. It needs Linux Docker Engine with Compose v2; Docker Desktop is not the supported
path because the broker binds to `127.0.0.1` and this Compose file uses Linux host networking. It does not
publish a port on every interface or change the application's loopback-only security default.

## Start, check, stop

From the checkout, run `docker compose up --build -d`, then visit
`http://127.0.0.1:8787/dash.html`. No config copy, prompt approval or token is needed for this preview.
`docker compose ps` should show healthy after startup. Health checks use Python inside the container;
curl is not required. If startup fails, check `docker compose logs broker` and that port 8787 is free.

`docker compose down` stops it and keeps the named state volume. Add `-v` only to delete preview state.
To rebuild after updating the checkout, run the start command again.

## Real container smoke check

Run `sh tools/docker-smoke.sh` from the checkout with Docker Engine available. It builds the current
working tree, starts the image with Compose's read-only root filesystem, dropped capabilities,
no-new-privileges, temporary `/tmp` and a fresh named state volume, then waits up to 60 seconds for health.
It requests `/health` and `/` from inside the container and checks for HTTP 200 and the expected content.
The `/` check follows the existing redirect to `/dash.html`.

Unlike Compose, this test uses `--network none`: it has its own loopback, no egress and no published ports.
It cannot accidentally query or collide with a broker already listening on the host's `127.0.0.1:8787`.
On failure it prints container logs and exits nonzero. On exit it removes its container, volume and image.
This is a real runtime test, separate from `sh tools/check.sh`, whose quickstart checks need no Docker.
Run it from a restrictive checkout too (files mode 0600, directories 0700, as with umask 077) to check that
image readability does not depend on the builder's checkout permissions.

## What is in the image

- Python 3.12 and the broker's runtime Python files. No agent CLI, git, test toolchain or worker prompts.
- The neutral `config/demo/broker.example.json`, also used by the source preview. The real environment
  takes precedence. State is under `/var/lib/eph-broker` in the named volume.
- A non-root UID, 10001. Compose drops capabilities, disallows privilege escalation, makes the root
  filesystem read-only and provides a temporary `/tmp`. No host directory or credential is mounted.
- Copied code is made world-readable and directories searchable before switching to that UID, regardless
  of checkout modes. `HOME=/var/lib/eph-broker` keeps all `~`-based defaults, including caches and thread
  scratch directories, in the writable state volume. Other temporary files use `/tmp`.

The Docker build context is an allowlist. Live config, `.git`, local credentials, site prompt overrides
and deployment files are not sent to the builder. Do not replace it with `COPY . .`.

Host networking shares the host's network namespace. It is not network isolation, and local processes
can reach the dashboard without authentication. Do not use this as proof that workers are sandboxed.
Even in a container, workers running as the broker's user could read broker credentials in that container.
See [security.md](security.md).

## Real workers and published images

Use the [manual first-MR path](first-merge-request.md) for real work today. This preview deliberately
does not mount a host Claude installation or login and cannot create an MR. An API key in Compose is
not a substitute for the stored login that workers use.

A worker-enabled image still needs a supported agent CLI and its runtime, git, a documented tag policy,
public registry access, and a build/release workflow. No project registry tag is claimed available here.
That release should document how to log in as the container user and mount only its dedicated Claude
login state, target repo checkouts and repo-scoped credentials. It must also handle git identity,
file ownership, prompt approval and persistent worker state. Do not mount your whole home or the Docker socket.

Guided GitLab setup and a native Vikunja webhook receiver (or one-command integration check) remain
separate work. Until then, the manual path includes the account, proxy and webhook steps rather than
claiming that a dashboard start is a working coding-agent installation.
