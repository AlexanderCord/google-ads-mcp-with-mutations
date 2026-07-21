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
# Use the project venv (see README-venv). Falls back to `uv` if present.
if [ -x "$HERE/.venv/bin/google-ads-mcp" ]; then
  exec "$HERE/.venv/bin/google-ads-mcp"
elif command -v uv >/dev/null 2>&1; then
  exec uv run google-ads-mcp
else
  exec python -m ads_mcp.server
fi
