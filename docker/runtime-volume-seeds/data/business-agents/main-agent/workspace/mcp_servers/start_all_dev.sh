#!/usr/bin/env bash
set -euo pipefail
cd "${CLAUDE_WORKSPACE:-/data/business-agents/main-agent/workspace}"
python -m pip install -r mcp_servers/requirements.txt

echo "MCP servers are stdio servers and are normally started by Claude Code from .mcp.json."
echo "Use /mcp inside Claude Code to verify status."
