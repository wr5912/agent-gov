#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
workspace="${CLAUDE_WORKSPACE:-$(cd -- "$script_dir/.." && pwd)}"
cd "$workspace"
python -m pip install -r mcp_servers/requirements.txt

echo "MCP servers are stdio servers and are normally started by Claude Code from .mcp.json."
echo "Use /mcp inside Claude Code to verify status."
