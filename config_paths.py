"""config_paths.py: locate operator config independently of the installed code.

BROKER_CONFIG_DIR is a bootstrap setting, supplied by the service environment, not by a
file inside the directory it selects. Source checkouts retain their in-tree defaults.
An external directory is authoritative: never fall back to packaged prompts or signatures.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SEED_MARKER = ".eph-config-initialized"


def config_dir():
    return os.getenv("CONFIG_DIR") or os.getenv("BROKER_CONFIG_DIR") or os.path.join(HERE, "config")


def prompts_dir():
    return os.getenv("PROMPTS_DIR") or os.path.join(os.getenv("BROKER_CONFIG_DIR") or HERE, "prompts")


def approved_path():
    return os.getenv("APPROVED_PROMPTS") or os.path.join(
        os.getenv("BROKER_CONFIG_DIR") or HERE, "approved-prompts.json")


def _copy_missing(source, target):
    if not os.path.lexists(target):
        # Preserve ownership as well as modes, including symlinks. Never copy onto an
        # existing directory: cp -a would also change that directory's metadata.
        subprocess.run(["cp", "-a", "--", source, target], check=True)
    elif (os.path.isdir(source) and not os.path.islink(source)
          and os.path.isdir(target) and not os.path.islink(target)):
        for name in os.listdir(source):
            _copy_missing(os.path.join(source, name), os.path.join(target, name))


def seed_site_config(source, target):
    """Initialize once, merging only missing paths and preserving operator files and metadata.

    The RPM migrates an old installation before replacing its packaged files. This also lets
    source installs explicitly copy defaults once without making runtime readers write config.
    The parent may already hold an EnvironmentFile, so only the completion marker gates seeding.
    """
    marker = os.path.join(target, SEED_MARKER)
    if os.path.lexists(marker):
        return
    if os.path.islink(target):
        raise ValueError("config target must not be a symlink")
    _copy_missing(os.path.join(source, "config"), target)
    _copy_missing(os.path.join(source, "prompts"), os.path.join(target, "prompts"))
    _copy_missing(os.path.join(source, "approved-prompts.json"), os.path.join(target, "approved-prompts.json"))
    with open(marker, "x"):
        pass


if __name__ == "__main__":
    seed_site_config(HERE, sys.argv[1])
