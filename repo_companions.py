"""Operator-configured, source-only snapshots for code workers. No ticket-supplied repos or URLs."""
import json
import logging
import os
import re
import shutil
import subprocess
import config_paths

log = logging.getLogger("eph-broker.companions")
HERE = os.path.dirname(os.path.abspath(__file__))
DIRECTORY = ".companions"
_REPO = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)+\Z")
_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*\Z")
# Reviewed source-selection knobs, not arbitrary loader/runtime environment variables.
TEST_ENV_NAMES = frozenset({"PORTCULLIS_SRC"})


def _test_env(entry):
    bindings = entry.get("test_env", {})
    if (not isinstance(bindings, dict) or any(
            name not in TEST_ENV_NAMES or not isinstance(path, str)
            or not path or "\\" in path or "\x00" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
            for name, path in bindings.items())):
        raise ValueError("companion test_env needs reviewed names and relative source directory paths")
    return bindings


def load(path=None):
    """Read fresh at each cut. Missing default = off; explicit missing or malformed config = refuse."""
    explicit = path or os.getenv("REPO_COMPANIONS_FILE")
    try:
        with open(explicit or os.path.join(config_paths.config_dir(), "repo-companions.json"), encoding="utf-8") as f:
            mapping = json.load(f)
    except FileNotFoundError:
        if not explicit:
            return {}
        raise
    if not isinstance(mapping, dict):
        raise ValueError("repo companions must be a repo-to-list object")
    for repo, entries in mapping.items():
        if not _REPO.fullmatch(repo) or not isinstance(entries, list):
            raise ValueError("repo companions need full namespace/repo keys and lists")
        names = set()
        env_names = set()
        for entry in entries:
            if (not isinstance(entry, dict) or not {"repo", "ref", "as"} <= set(entry)
                    or set(entry) - {"repo", "ref", "as", "test_env"}
                    or not all(isinstance(entry[k], str) for k in ("repo", "ref", "as"))
                    or not _REPO.fullmatch(entry["repo"]) or not _NAME.fullmatch(entry["as"])
                    or not _REF.fullmatch(entry["ref"]) or ".." in entry["ref"]
                    or entry["ref"].endswith(("/", ".")) or entry["as"] in names):
                raise ValueError("invalid or duplicate repo companion (expected repo, ref, lowercase snake_case as)")
            names.add(entry["as"])
            bindings = _test_env(entry)
            if env_names.intersection(bindings):
                raise ValueError("duplicate companion test_env name")
            env_names.update(bindings)
    return mapping


def cut(path, base_repo):
    """Shallow-fetch each configured ref into a fresh private repo, pin it, then discard git metadata.

    Catalog resolution preserves its denied/archived-repo floor even when inference is disabled.
    The snapshots never share object stores, credentials or mutable refs with a standing checkout.
    """
    mapping = load()
    if not any(mapping.values()):
        return []
    import repo_catalog
    from worktree import _git, _wt_argv, _wt_env, _store_cred_args
    cat = repo_catalog.get_catalog()
    primary = cat.by_local_path(base_repo)
    entries = mapping.get(primary.path, []) if primary else []
    if not entries:
        return []
    sources = []
    for entry in entries:
        repo = cat.by_path.get(entry["repo"])
        if repo is None:
            raise ValueError("companion is not an admitted catalog repo: " + entry["repo"])
        sources.append((entry, repo))
    root = os.path.join(path, DIRECTORY)
    os.mkdir(root)                  # refuse tracked files/directories and symlinks, never reuse a snapshot
    exclude = _git(path, "rev-parse", "--git-path", "info/exclude")
    if not os.path.isabs(exclude):
        exclude = os.path.join(path, exclude)
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, "a", encoding="utf-8") as f:
        f.write("\n/.companions/\n")
    records = []
    for entry, repo in sources:
        dest = os.path.join(root, entry["as"])
        os.mkdir(dest)
        env = {**_wt_env(dest), "GIT_TERMINAL_PROMPT": "0"}

        def git(*args):
            return subprocess.run(_wt_argv(dest, *args), env=env, check=True, capture_output=True,
                                  text=True, timeout=300).stdout.strip()

        cred = repo_catalog.broad_cred_git_args(repo) or _store_cred_args()
        source = repo.remote or repo.http_url   # preserve an existing checkout's SSH/read-auth choice
        if not source:
            raise ValueError("companion has no catalog clone URL: " + repo.path)
        try:
            git("init", "--quiet")
            git(*cred, "-c", "fetch.recurseSubmodules=false", "fetch", "--depth=1", "--no-tags",
                "--", source, entry["ref"])
            sha = git("rev-parse", "--verify", "FETCH_HEAD^{commit}")
            git("checkout", "--quiet", "--detach", sha, "--")
        except (OSError, subprocess.SubprocessError):
            # Never send subprocess argv/stderr (credential helper or server output) to the worker.
            raise RuntimeError("companion checkout failed: " + repo.path) from None
        shutil.rmtree(os.path.join(dest, ".git"))
        for relative in _test_env(entry).values():
            source_dir = os.path.realpath(os.path.join(dest, relative))
            if not source_dir.startswith(os.path.realpath(dest) + os.sep) or not os.path.isdir(source_dir):
                raise ValueError("companion test_env must select a directory inside its snapshot")
        records.append({**entry, "sha": sha, "path": dest})
    # Permission bits are a second belt, not a same-uid sandbox. Never chmod through source symlinks.
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in files:
            file = os.path.join(directory, name)
            if not os.path.islink(file):
                os.chmod(file, 0o555 if os.stat(file).st_mode & 0o111 else 0o444)
        os.chmod(directory, 0o555)
    return records


def record_run(store, key, run_id, cut_sha, companions):
    """Pin provenance before launching the worker; the broker journal survives worktree removal."""
    if companions:
        record = {"key": key, "run_id": run_id, "cut_sha": cut_sha, "companions": companions}
        if store is not None:
            store.put("run:" + run_id, record)
        log.info("companion run record %s", json.dumps(record, sort_keys=True))


def environment(worker):
    companions = getattr(worker, "companions", [])
    env = {"COMPANION_" + c["as"].upper(): c["path"] for c in companions}
    env.update(test_environment(companions))
    return env


def test_environment(companions):
    """Bind only operator-configured knobs to this run's pinned, read-only sources."""
    return {name: os.path.join(c["path"], relative)
            for c in companions for name, relative in _test_env(c).items()}


def prepare_remove(path):
    """Restore directory owner-write bits for broker teardown, without following snapshot symlinks."""
    root = os.path.join(path, DIRECTORY)
    if os.path.islink(root):
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        os.chmod(directory, 0o700)
