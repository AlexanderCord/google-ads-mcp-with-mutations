# Plan: add mutation tools to the Google Ads MCP fork

**Status:** ✅ IMPLEMENTED + VERIFIED (2026-07-21). 7 mutate tools live; all 8 no-spend verification steps pass; full NZ campaign created via the tools (PAUSED). Remaining: owner runs `claude mcp add` (below) to register in Claude Code.
**Date:** 2026-07-21

## Setup / run (venv, no uv)
```
cd mcp/google-ads-mcp
/opt/homebrew/bin/python3.11 -m venv .venv && .venv/bin/pip install -e .
```
Register in Claude Code (run in your terminal — `claude` CLI not in the agent sandbox):
```
claude mcp add google-ads -- bash /Users/ershov/Documents/TW/tw3/mcp/google-ads-mcp/run-mcp.sh
```
`run-mcp.sh` sources `../startup-research-agent/agents/google_ads_test/.env` and runs `.venv/bin/google-ads-mcp` (stdio). utils.py reads GADS_* creds directly.

## Tools shipped (ads_mcp/tools/mutate.py)
create_campaign (atomic budget+campaign+geo/lang via temp resource names, PAUSED, validate_only), create_ad_group, create_responsive_search_ad (symbol-sanitized), add_keywords, add_negative_keywords, update_campaign_status (ENABLE/PAUSE/REMOVE — REMOVE uses remove op), update_campaign_budget. Ceiling MAX_DAILY_BUDGET_USD=$50. Read tools (search/metadata/customers) stay enabled.

## Current state (read-only)
Official Google Ads MCP (FastMCP). Tools: `search` (GAQL, query-only), `get_resource_metadata`, `list_accessible_customers`. No create/modify.

## Key correction
GAQL is **query-only** — cannot create. Creating campaigns/ad groups uses the Google Ads API **Mutate services** (CampaignService.MutateCampaigns, AdGroupService, etc.), the pattern our `startup-research-agent/agents/google_ads_test/launch_sprint1_campaigns.py` already uses. This plan adds **mutation tools**, keeps GAQL for reads.

## Registration mechanism (confirmed in coordinator.py)
`initialize_and_mount_tools()` reflects over `ads_mcp/tools/*.py`, finds every `FastMCP` instance, mounts it if its category is enabled in `tools_config.yaml`. So a new `tools/mutate.py` with `mutate_mcp = FastMCP("mutate")` self-registers; we just enable `mutate` in config.

## File changes

### 1. `ads_mcp/utils.py` — credential patch (Part A: run with our creds)
`_create_credentials()`: before the `google.auth.default()` fallback, if `GADS_CLIENT_ID/SECRET/REFRESH_TOKEN` are set, build
`google.oauth2.credentials.Credentials(token=None, refresh_token=…, client_id=…, client_secret=…, token_uri="https://oauth2.googleapis.com/token", scopes=[_ADS_SCOPE])`.
Non-breaking (only activates when those vars are present).

### 2. `ads_mcp/tools/mutate.py` — new (mirror search.py)
`mutate_mcp = FastMCP("mutate")`, using existing `utils.get_googleads_service()` / `get_googleads_type()`. Tools:

| Tool | Purpose | Guardrail |
|---|---|---|
| create_campaign | budget + Search campaign, Manual CPC | status forced PAUSED; budget ≤ ceiling |
| create_ad_group | ad group under a campaign | — |
| create_responsive_search_ad | RSA (headlines/descriptions/pins) | sanitize SYMBOLS-policy chars |
| add_keywords | phrase/exact + per-kw CPC | — |
| add_negative_keywords | campaign-level negatives | — |
| update_campaign_status | PAUSE / ENABLE / REMOVE | only path to spend; explicit |
| update_campaign_budget | change daily budget | ≤ ceiling |

Port gotchas from launch_sprint1: micros = `round(usd*100)*10_000`; no campaign-level ad_rotation in v24 (set on ad group); symbol sanitize; per-name GAQL lookups (no OR/parens).

### 3. Safety layer (baked in)
- **PAUSED-by-default** on every create — no spend until explicit `update_campaign_status(ENABLED)`.
- **`validate_only` param** on each mutate tool → API request `validate_only=True` (checks, writes nothing). Default False.
- **Hard budget ceiling** constant (default **$50/day**) — reject above.
- `customer_id` required explicitly per call.
- `GoogleAdsException` → `ToolError` with request_id + messages.
- Tool annotations: `readOnlyHint=False`, `destructiveHint=True` on remove/status.

### 4. `tools_config.yaml` + `config.py`
Add `mutate: true` to namespaces; add `"mutate"` to `ALL_CATEGORIES`. Ships enabled; can disable to go read-only.

### 5. `run-mcp.sh` launcher (repo root)
Sources `../startup-research-agent/agents/google_ads_test/.env`; exports `GOOGLE_ADS_DEVELOPER_TOKEN=$GADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID=$GADS_LOGIN_CUSTOMER_ID`, passes `GADS_*` through; `uv run google-ads-mcp` (stdio).

## Adding to Claude Code
`claude mcp add google-ads -- bash /Users/ershov/Documents/TW/tw3/mcp/google-ads-mcp/run-mcp.sh` (stdio), then `/mcp` to confirm tools load.

## Verification (no spend)
1. `list_accessible_customers` → 5163667250 (creds work).
2. `create_campaign(..., validate_only=True)` → validates, no write.
3. Real create → PAUSED `TW_MCP_TEST_*`; confirm via `search`; `update_campaign_status(REMOVE)` cleanup.

## Owner decisions
- Build/test on live account 5163667250 (safe via PAUSED-default)? 
- Budget ceiling $50/day default?
- Keep read tools enabled alongside mutate (recommended yes).

## Then: real use case
Create a full new-country campaign via the MCP conversationally (campaign + ad group + RSAs + keywords), PAUSED, for the next iteration — pick a country not yet used (e.g. a new EN market for a tours or tickets test).
