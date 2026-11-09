"""Offline SSH configuration checks shared by the three fake-harness suites."""
import os
import subprocess


def ssh_home(root):
    home = os.path.join(root, "ssh home")
    directory = os.path.join(home, ".ssh")
    os.makedirs(directory, mode=0o700)
    with open(os.path.join(directory, "config"), "w") as f:
        f.write("Host eph-ssh-regression\n  HostName ssh.example.test\n  User regression\n")
    return home


def assert_ssh_config(argv=None, env=None):
    # -G evaluates config and exits: no connection, credential read or bridge traffic.
    r = subprocess.run((argv or ["ssh"]) + ["-vvv", "-G", "eph-ssh-regression"],
                       env=env, capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    assert "hostname ssh.example.test\n" in r.stdout, r.stdout
    assert "user regression\n" in r.stdout, r.stdout
    assert "stricthostkeychecking ask\n" in r.stdout, r.stdout
    assert "Reading configuration data /etc/ssh/" not in r.stderr, r.stderr
    assert "/etc/ssh/ssh_config.d" not in r.stderr, r.stderr
