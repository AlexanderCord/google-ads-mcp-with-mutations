#!/bin/bash
# Launch the Google Ads MCP server (stdio) using the project's existing
# Google Ads credentials (startup-research-agent/agents/google_ads_test/.env).
#
# utils.py reads GADS_CLIENT_ID/SECRET/REFRESH_TOKEN + GADS_DEVELOPER_TOKEN +
# GADS_LOGIN_CUSTOMER_ID directly, so we just source the .env and run.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HERE/../startup-research-agent/agents/google_ads_test/.env"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

# Map to the names the MCP also understands (belt and suspenders).
export GOOGLE_ADS_DEVELOPER_TOKEN="${GOOGLE_ADS_DEVELOPER_TOKEN:-${GADS_DEVELOPER_TOKEN:-}}"
export GOOGLE_ADS_LOGIN_CUSTOMER_ID="${GOOGLE_ADS_LOGIN_CUSTOMER_ID:-${GADS_LOGIN_CUSTOMER_ID:-}}"

cd "$HERE"
# Run the package by module with an explicit PYTHONPATH rather than the
# installed `google-ads-mcp` console script. The editable install's .pth path
# hook does not resolve `ads_mcp` (it only ever imported because the CWD
# happened to contain the package), so the console script dies with
# ModuleNotFoundError when the client launches it. This is CWD-independent.
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
if [ -x "$HERE/.venv/bin/python" ]; then
  exec "$HERE/.venv/bin/python" -m ads_mcp.server
elif command -v uv >/dev/null 2>&1; then
  exec uv run python -m ads_mcp.server
else
  exec python -m ads_mcp.server
fi
