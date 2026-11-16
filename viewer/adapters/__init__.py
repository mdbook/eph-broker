"""Optional adapter methods: view(entry, allowlist), steer, tail.

Presence is the fallback for every known kind. View implementations must route
outbound requests through allowlist.fetch so DNS is checked and pinned.
"""
from . import presence

KINDS = ('hermes', 'claude-code', 'codex', 'generic')
REGISTRY = {kind: presence for kind in KINDS}


def get(kind):
    return REGISTRY.get(kind, presence)
