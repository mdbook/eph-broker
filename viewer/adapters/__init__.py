"""Optional adapter methods: view(entry, allowlist, request), steer, tail.

Presence is the fallback for every known kind. View implementations must route
outbound requests through allowlist.open or allowlist.fetch so DNS is checked and
pinned. An adapter with view also exports READ and ACT (route names) and body_limit.
"""
from . import hermes, presence

KINDS = ('hermes', 'claude-code', 'codex', 'generic')
REGISTRY = dict({kind: presence for kind in KINDS}, hermes=hermes)


def get(kind):
    return REGISTRY.get(kind, presence)
