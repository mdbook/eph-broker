#!/bin/sh
# Ready-made BROKER_MCP_SH for Vikunja: set BROKER_MCP_SH to this file's absolute path.
# The name `mcp.sh` is deliberate: the general worker's deny list blocks it by name.
exec python3 "$(dirname "$0")/vikunja_bridge.py" "$@"
