"""Bubblewrap worker boundary. Knobs are operator-owned, never taken from a ticket.

The root is readable, not writable; secrets are overmounted and /proc belongs to a
new PID namespace. Network access is deliberately unchanged. See docs/security.md.
"""
import os
import shutil


class SandboxError(OSError):
    """Invalid sandbox configuration: refuse startup/launch, never fall back to off."""


def check():
    """Validate the real environment at startup and again at every launch."""
    mode = os.getenv("WORKER_SANDBOX", "bwrap").strip().lower()
    if mode == "off":
        return None
    if mode != "bwrap":
        raise SandboxError("WORKER_SANDBOX must be bwrap or off")
    binary = shutil.which("bwrap")
    if not binary:
        raise SandboxError("WORKER_SANDBOX=bwrap requires bubblewrap; refusing to start "
                           "(WORKER_SANDBOX=off explicitly disables isolation)")
    return os.path.abspath(binary)      # Popen changes cwd; never re-resolve a relative PATH hit in the worktree


def _path(value):
    value = os.path.expanduser(os.fspath(value))
    if not os.path.isabs(value):
        raise SandboxError("sandbox paths must be absolute")
    value = os.path.realpath(value)
    if value == "/":
        raise SandboxError("sandbox path must not be the filesystem root")
    return value


def _paths(knob):
    return [_path(p) for p in os.getenv(knob, "").split(os.pathsep) if p]


def gitdir(worktree):
    """Snapshot the common git dir BEFORE a worker runs; never execute repo config.

    Call only on the freshly broker-created worktree, not again on a resumed turn:
    a worker can rewrite its .git pointer. Non-git live working directories have no
    extra writable git mount.
    """
    if not worktree:
        return None
    dotgit = os.path.join(worktree, ".git")
    if os.path.islink(dotgit):
        raise SandboxError("refusing a symlink .git")
    if os.path.isdir(dotgit):
        return _path(dotgit)
    try:
        with open(dotgit, encoding="utf-8") as f:
            pointer = f.read().strip()
    except FileNotFoundError:
        return None
    if not pointer.startswith("gitdir: "):
        raise SandboxError("invalid worktree .git pointer")
    directory = os.path.realpath(os.path.join(worktree, pointer[len("gitdir: "):]))
    try:
        with open(os.path.join(directory, "commondir"), encoding="utf-8") as f:
            common = f.read().strip()
    except FileNotFoundError:
        return _path(directory)
    return _path(os.path.join(directory, common))


def wrap(argv, worktree, base_repo_gitdir, *, writable_paths=()):
    """Return a launch argv (off: an unchanged copy), without mutating the host.

    writable_paths are broker-created per-worker runtime state, not arbitrary
    operator/ticket grants. EXTRA_RO/HIDE are colon-separated absolute paths.
    Hides always win, including over a worktree or an EXTRA_RO entry. Files are
    masked by /dev/null (tmpfs mounts require directories); directories by tmpfs.
    """
    binary = check()
    if binary is None:
        return list(argv)
    import site_env                     # already loaded by app, after the site overlay

    out = [binary, "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
           "--cap-drop", "ALL", "--ro-bind", "/", "/", "--tmpfs", "/tmp"]
    for path in dict.fromkeys(_path(p) for p in (worktree, base_repo_gitdir, *writable_paths) if p):
        if path == "/proc" or path.startswith("/proc/") or path == "/dev" or path.startswith("/dev/"):
            raise SandboxError("runtime paths must not be under /proc or /dev")
        out += ["--bind", path, path]
    for path in _paths("WORKER_SANDBOX_EXTRA_RO"):
        out += ["--ro-bind", path, path]
    # Mount these AFTER any configured bind so EXTRA_RO cannot restore host /proc.
    out += ["--dev", "/dev", "--proc", "/proc"]
    hidden = [os.path.expanduser("~/.git-credentials"), os.path.expanduser("~/.config/eph-broker"),
              "/etc/eph-broker",          # conventional EnvironmentFile directory (docs/install.md)
              os.path.dirname(site_env.SITE_ENV), os.path.dirname(site_env.BROKER_CONFIG),
              site_env.SITE_ENV, site_env.BROKER_CONFIG,
              *_paths("WORKER_SANDBOX_HIDE")]
    # Resolve the files too: a file symlink can escape its hidden parent directory.
    # The parents mask the original locations; realpath masks targets exposed by the readable root.
    paths = sorted(set(_path(p) for p in hidden), key=lambda p: (p.count("/"), p))
    covered = []
    for path in paths:
        if any(path.startswith(parent + "/") for parent in covered):
            continue
        if not os.path.exists(path):    # absent secrets need no mount; no host placeholders are created
            continue
        if os.path.isdir(path):
            out += ["--tmpfs", path]
            covered.append(path)
        else:
            out += ["--ro-bind", "/dev/null", path]
    return [*out, "--", *argv]
