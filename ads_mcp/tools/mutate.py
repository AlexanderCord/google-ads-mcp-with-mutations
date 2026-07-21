# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Mutation tools: create/update campaigns, ad groups, ads and keywords.

Safety model:
  * Campaigns are created PAUSED. Nothing can spend until an explicit
    `update_campaign_status(..., status="ENABLED")` call.
  * Every mutating tool accepts `validate_only` (default False). When True the
    Google Ads API validates the request and writes nothing.
  * Daily budgets are capped by MAX_DAILY_BUDGET_USD.
"""

import os
from typing import Any, Dict, List
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2

import ads_mcp.utils as utils

mutate_mcp = FastMCP("mutate")

# Hard ceiling — any create/update above this is rejected before hitting the API.
MAX_DAILY_BUDGET_USD = 50.0
# Per-click bid ceiling. A budget cap alone doesn't stop a runaway CPC from
# burning the day's budget in a handful of terrible-quality clicks.
MAX_CPC_BID_USD = 20.0

# Asset field types remove_account_asset_links may unlink. This is an allowlist,
# not a formatting nicety: the value is interpolated into a GAQL WHERE clause,
# so an unconstrained string could broaden the query and unlink assets of every
# type. Extend deliberately.
REMOVABLE_FIELD_TYPES = {"SITELINK", "CALLOUT", "STRUCTURED_SNIPPET", "PROMOTION"}

# Account allowlist. The OAuth credentials can reach every account the user can
# (often including unrelated businesses), and this server is driven by an LLM
# that reads untrusted text (web pages, API responses, research files). Without
# a scope check, a prompt injection could enable or remove campaigns in an
# account that has nothing to do with this project. Set
# GADS_MCP_ALLOWED_CUSTOMER_IDS (comma-separated) to override.
_DEFAULT_ALLOWED = "5163667250"
ALLOWED_CUSTOMER_IDS = {
    c.replace("-", "").strip()
    for c in os.environ.get("GADS_MCP_ALLOWED_CUSTOMER_IDS", _DEFAULT_ALLOWED).split(",")
    if c.strip()
}


# ── helpers ──────────────────────────────────────────────────────────────────
def _cid(customer_id: str) -> str:
    """Normalize and authorize the target account for a mutating call."""
    cid = str(customer_id).replace("-", "").strip()
    if not cid.isdigit():
        raise ToolError(f"customer_id must be 10 digits, got {customer_id!r}")
    if cid not in ALLOWED_CUSTOMER_IDS:
        raise ToolError(
            f"Account {cid} is not in this server's allowlist "
            f"({sorted(ALLOWED_CUSTOMER_IDS)}). Mutations are refused for accounts "
            "outside it. Set GADS_MCP_ALLOWED_CUSTOMER_IDS to change this."
        )
    return cid


def _check_cpc(usd: Any, label: str = "cpc") -> float:
    """Validate a bid: finite, positive, and under the per-click ceiling."""
    try:
        val = float(usd)
    except (TypeError, ValueError):
        raise ToolError(f"{label} must be a number, got {usd!r}")
    if val != val or val in (float("inf"), float("-inf")):
        raise ToolError(f"{label} must be a finite number, got {usd!r}")
    if val <= 0:
        raise ToolError(f"{label} must be greater than 0, got {val}")
    if val > MAX_CPC_BID_USD:
        raise ToolError(
            f"{label} ${val} exceeds the per-click ceiling ${MAX_CPC_BID_USD}."
        )
    return val


def _micros(usd: float) -> int:
    """USD → micros as an exact multiple of the $0.01 billable unit."""
    return int(round(float(usd) * 100)) * 10_000


def _sanitize(text: str) -> str:
    """Strip characters Google Ads' SYMBOLS policy rejects in ad text."""
    for a, b in (("★", ""), ("~", ""), ("—", "-"), ("–", "-")):
        text = text.replace(a, b)
    return text.replace("  ", " ").strip()


def _check_budget(daily_budget_usd: Any) -> float:
    try:
        val = float(daily_budget_usd)
    except (TypeError, ValueError):
        raise ToolError(f"daily_budget_usd must be a number, got {daily_budget_usd!r}")
    if val != val or val in (float("inf"), float("-inf")) or val <= 0:
        raise ToolError(f"daily_budget_usd must be finite and > 0, got {daily_budget_usd!r}")
    if val > MAX_DAILY_BUDGET_USD:
        raise ToolError(
            f"Daily budget ${val} exceeds the safety ceiling "
            f"${MAX_DAILY_BUDGET_USD}. Lower it or raise MAX_DAILY_BUDGET_USD."
        )
    return val


def _check_owned(resource_name: str, cid: str, kind: str) -> str:
    """Resource must live under the authorized account, with a numeric id.

    Stops a caller from passing customer_id=<allowed> while pointing the
    operation at `customers/<other>/...`, and pins the id segment to digits so
    the value is safe to use in a GAQL literal (no injection surface) and can
    never be a malformed/crafted resource name.

    Returns the validated resource name.
    """
    prefix = f"customers/{cid}/{kind}/"
    if not isinstance(resource_name, str) or not resource_name.startswith(prefix):
        raise ToolError(
            f"resource_name {resource_name!r} does not belong to account {cid} "
            f"(expected it to start with {prefix!r})."
        )
    entity_id = resource_name[len(prefix):]
    if not entity_id.isdigit():
        raise ToolError(
            f"resource_name {resource_name!r} has a non-numeric id segment "
            f"({entity_id!r}); expected {prefix}<digits>."
        )
    return resource_name


def _entity_id(resource_name: str) -> str:
    """Digit-only id from a resource name already validated by _check_owned."""
    return resource_name.rsplit("/", 1)[-1]


# Ads may only point at domains we control. An LLM-driven tool that can set an
# ad's destination is an injection target: pointing ads at an arbitrary domain
# spends the account's money and risks suspension.
ALLOWED_AD_DOMAINS = {
    d.strip().lower()
    for d in os.environ.get("GADS_MCP_ALLOWED_AD_DOMAINS", "ticketworld.travel").split(",")
    if d.strip()
}
_MAX_URL_LEN = 2048
_MAX_SUFFIX_LEN = 512


def _check_final_url(url: str) -> str:
    from urllib.parse import urlparse

    if not isinstance(url, str) or len(url) > _MAX_URL_LEN:
        raise ToolError(f"final_url must be a string under {_MAX_URL_LEN} chars.")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ToolError(f"final_url must use https, got {parsed.scheme!r}.")
    host = (parsed.hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in ALLOWED_AD_DOMAINS):
        raise ToolError(
            f"final_url host {host!r} is not in the allowed ad-destination domains "
            f"({sorted(ALLOWED_AD_DOMAINS)}). Set GADS_MCP_ALLOWED_AD_DOMAINS to change."
        )
    return url


def _check_url_suffix(suffix: str) -> str:
    if not isinstance(suffix, str) or len(suffix) > _MAX_SUFFIX_LEN:
        raise ToolError(f"final_url_suffix must be a string under {_MAX_SUFFIX_LEN} chars.")
    if any(ch in suffix for ch in ("\n", "\r", " ", "#")):
        raise ToolError("final_url_suffix must be url-encoded query params (no spaces/newlines/#).")
    return suffix


def _raise(ex: GoogleAdsException) -> None:
    msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
    raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(msgs))


_MUT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)


# ── create_campaign ──────────────────────────────────────────────────────────
@mutate_mcp.tool(annotations=_MUT)
def create_campaign(
    customer_id: str,
    name: str,
    daily_budget_usd: float,
    geo_target_constants: List[int] = [],
    language_constant: int = 1000,
    final_url_suffix: str = "",
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Create a Search campaign (Manual CPC), ALWAYS created PAUSED.

    Also creates its (non-shared) daily budget. Presence-only geo targeting.
    Enable it later with `update_campaign_status`.

    Args:
        customer_id: 10-digit account id (no dashes).
        name: Campaign name.
        daily_budget_usd: Daily budget in USD (<= safety ceiling).
        geo_target_constants: geoTargetConstant ids (e.g. 2036 AU, 2826 UK, 2840 US).
        language_constant: languageConstant id (1000 = English).
        final_url_suffix: Optional tracking suffix (utm params etc.).
        validate_only: If True, validate without creating.

    Returns:
        {campaign_resource_name, budget_resource_name, status:"PAUSED"}.
    """
    _check_budget(daily_budget_usd)
    cid = _cid(customer_id)
    client = utils.get_googleads_client()
    enums = client.enums
    # Temporary resource names let budget + campaign + criteria be created in a
    # single atomic mutate (and validated together under validate_only).
    temp_budget = f"customers/{cid}/campaignBudgets/-1"
    temp_campaign = f"customers/{cid}/campaigns/-2"
    try:
        ops = []

        # Budget
        mo = client.get_type("MutateOperation")
        b = mo.campaign_budget_operation.create
        b.resource_name = temp_budget
        b.name = f"{name} budget"
        b.amount_micros = _micros(daily_budget_usd)
        b.delivery_method = enums.BudgetDeliveryMethodEnum.STANDARD
        b.explicitly_shared = False
        ops.append(mo)

        # Campaign (PAUSED)
        mo = client.get_type("MutateOperation")
        c = mo.campaign_operation.create
        c.resource_name = temp_campaign
        c.name = name
        c.advertising_channel_type = enums.AdvertisingChannelTypeEnum.SEARCH
        c.status = enums.CampaignStatusEnum.PAUSED
        c.campaign_budget = temp_budget
        c.manual_cpc.enhanced_cpc_enabled = False
        c.network_settings.target_google_search = True
        c.network_settings.target_search_network = False
        c.network_settings.target_content_network = False
        c.network_settings.target_partner_search_network = False
        c.geo_target_type_setting.positive_geo_target_type = enums.PositiveGeoTargetTypeEnum.PRESENCE
        c.geo_target_type_setting.negative_geo_target_type = enums.NegativeGeoTargetTypeEnum.PRESENCE
        if final_url_suffix:
            c.final_url_suffix = _check_url_suffix(final_url_suffix)
        c.contains_eu_political_advertising = (
            enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )
        ops.append(mo)

        # geo + language criteria (reference the temp campaign)
        for g in geo_target_constants:
            mo = client.get_type("MutateOperation")
            cr = mo.campaign_criterion_operation.create
            cr.campaign = temp_campaign
            cr.location.geo_target_constant = f"geoTargetConstants/{g}"
            ops.append(mo)
        mo = client.get_type("MutateOperation")
        cr = mo.campaign_criterion_operation.create
        cr.campaign = temp_campaign
        cr.language.language_constant = f"languageConstants/{language_constant}"
        ops.append(mo)

        ga = client.get_service("GoogleAdsService")
        req = client.get_type("MutateGoogleAdsRequest")
        req.customer_id = cid
        req.mutate_operations.extend(ops)
        req.validate_only = validate_only
        resp = ga.mutate(request=req)

        camp_rn, budget_rn = "", ""
        if not validate_only:
            for r in resp.mutate_operation_responses:
                if r._pb.HasField("campaign_result"):
                    camp_rn = r.campaign_result.resource_name
                elif r._pb.HasField("campaign_budget_result"):
                    budget_rn = r.campaign_budget_result.resource_name

        return {
            "campaign_resource_name": camp_rn,
            "budget_resource_name": budget_rn,
            "status": "PAUSED",
            "validated_only": validate_only,
        }
    except GoogleAdsException as ex:
        _raise(ex)


# ── create_ad_group ──────────────────────────────────────────────────────────
@mutate_mcp.tool(annotations=_MUT)
def create_ad_group(
    customer_id: str,
    campaign_resource_name: str,
    name: str,
    default_cpc_usd: float = 1.0,
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Create an ad group under a campaign (rotate ads forever).

    Args:
        customer_id: 10-digit account id.
        campaign_resource_name: e.g. customers/123/campaigns/456.
        name: Ad group name.
        default_cpc_usd: Default max CPC bid.
        validate_only: If True, validate without creating.
    """
    cid = _cid(customer_id)
    _check_owned(campaign_resource_name, cid, "campaigns")
    client = utils.get_googleads_client()
    enums = client.enums
    try:
        op = client.get_type("AdGroupOperation")
        ag = op.create
        ag.name = name
        ag.campaign = campaign_resource_name
        ag.status = enums.AdGroupStatusEnum.ENABLED
        ag.type_ = enums.AdGroupTypeEnum.SEARCH_STANDARD
        ag.ad_rotation_mode = enums.AdGroupAdRotationModeEnum.ROTATE_FOREVER
        ag.cpc_bid_micros = _micros(_check_cpc(default_cpc_usd, "default_cpc_usd"))
        req = client.get_type("MutateAdGroupsRequest")
        req.customer_id = cid
        req.operations.append(op)
        req.validate_only = validate_only
        res = client.get_service("AdGroupService").mutate_ad_groups(request=req)
        return {
            "ad_group_resource_name": "" if validate_only else res.results[0].resource_name,
            "validated_only": validate_only,
        }
    except GoogleAdsException as ex:
        _raise(ex)


# ── create_responsive_search_ad ──────────────────────────────────────────────
@mutate_mcp.tool(annotations=_MUT)
def create_responsive_search_ad(
    customer_id: str,
    ad_group_resource_name: str,
    final_url: str,
    headlines: List[str],
    descriptions: List[str],
    pinned_headline1: str = "",
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Create a Responsive Search Ad (3-15 headlines, 2-4 descriptions).

    Text is sanitized for the SYMBOLS policy (no star/tilde/em-dash).

    Args:
        pinned_headline1: If given, that headline is pinned to position 1.
    """
    cid = _cid(customer_id)
    _check_owned(ad_group_resource_name, cid, "adGroups")
    _check_final_url(final_url)
    client = utils.get_googleads_client()
    enums = client.enums
    try:
        op = client.get_type("AdGroupAdOperation")
        ad = op.create
        ad.ad_group = ad_group_resource_name
        ad.status = enums.AdGroupAdStatusEnum.ENABLED
        rsa = ad.ad.responsive_search_ad
        for h in headlines[:15]:
            asset = client.get_type("AdTextAsset")
            asset.text = _sanitize(h)
            if pinned_headline1 and h == pinned_headline1:
                asset.pinned_field = enums.ServedAssetFieldTypeEnum.HEADLINE_1
            rsa.headlines.append(asset)
        for d in descriptions[:4]:
            asset = client.get_type("AdTextAsset")
            asset.text = _sanitize(d)
            rsa.descriptions.append(asset)
        ad.ad.final_urls.append(final_url)
        req = client.get_type("MutateAdGroupAdsRequest")
        req.customer_id = cid
        req.operations.append(op)
        req.validate_only = validate_only
        res = client.get_service("AdGroupAdService").mutate_ad_group_ads(request=req)
        return {
            "ad_resource_name": "" if validate_only else res.results[0].resource_name,
            "validated_only": validate_only,
        }
    except GoogleAdsException as ex:
        _raise(ex)


# ── add_keywords ─────────────────────────────────────────────────────────────
@mutate_mcp.tool(annotations=_MUT)
def add_keywords(
    customer_id: str,
    ad_group_resource_name: str,
    keywords: List[Dict[str, Any]],
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Add keywords to an ad group.

    Args:
        keywords: list of {text, match_type: EXACT|PHRASE|BROAD, cpc_usd?}.
    """
    cid = _cid(customer_id)
    _check_owned(ad_group_resource_name, cid, "adGroups")
    client = utils.get_googleads_client()
    enums = client.enums
    try:
        ops = []
        for kw in keywords:
            op = client.get_type("AdGroupCriterionOperation")
            crit = op.create
            crit.ad_group = ad_group_resource_name
            crit.status = enums.AdGroupCriterionStatusEnum.ENABLED
            crit.keyword.text = kw["text"]
            crit.keyword.match_type = getattr(
                enums.KeywordMatchTypeEnum, str(kw.get("match_type", "PHRASE")).upper()
            )
            if kw.get("cpc_usd"):
                crit.cpc_bid_micros = _micros(_check_cpc(kw["cpc_usd"], "keyword cpc_usd"))
            ops.append(op)
        req = client.get_type("MutateAdGroupCriteriaRequest")
        req.customer_id = cid
        req.operations.extend(ops)
        req.validate_only = validate_only
        res = client.get_service("AdGroupCriterionService").mutate_ad_group_criteria(request=req)
        return {"added": 0 if validate_only else len(res.results), "validated_only": validate_only}
    except GoogleAdsException as ex:
        _raise(ex)


# ── add_negative_keywords ────────────────────────────────────────────────────
@mutate_mcp.tool(annotations=_MUT)
def add_negative_keywords(
    customer_id: str,
    campaign_resource_name: str,
    keywords: List[Dict[str, Any]],
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Add campaign-level negative keywords.

    Args:
        keywords: list of {text, match_type: EXACT|PHRASE|BROAD}.
    """
    cid = _cid(customer_id)
    _check_owned(campaign_resource_name, cid, "campaigns")
    client = utils.get_googleads_client()
    enums = client.enums
    try:
        ops = []
        for kw in keywords:
            op = client.get_type("CampaignCriterionOperation")
            crit = op.create
            crit.campaign = campaign_resource_name
            crit.negative = True
            crit.keyword.text = kw["text"]
            crit.keyword.match_type = getattr(
                enums.KeywordMatchTypeEnum, str(kw.get("match_type", "PHRASE")).upper()
            )
            ops.append(op)
        req = client.get_type("MutateCampaignCriteriaRequest")
        req.customer_id = cid
        req.operations.extend(ops)
        req.validate_only = validate_only
        res = client.get_service("CampaignCriterionService").mutate_campaign_criteria(request=req)
        return {"added": 0 if validate_only else len(res.results), "validated_only": validate_only}
    except GoogleAdsException as ex:
        _raise(ex)


# ── update_campaign_status ───────────────────────────────────────────────────
@mutate_mcp.tool(annotations=_DESTRUCTIVE)
def update_campaign_status(
    customer_id: str,
    campaign_resource_name: str,
    status: str,
    validate_only: bool = False,
    confirm_destructive: bool = False,
) -> Dict[str, Any]:
    """Set a campaign's status: ENABLED, PAUSED or REMOVED.

    This is the ONLY way a campaign starts spending (status=ENABLED).

    Guards:
      * The campaign must belong to the authorized account.
      * ENABLED is refused if the campaign's existing daily budget exceeds
        MAX_DAILY_BUDGET_USD — otherwise enabling a pre-existing big-budget
        campaign would bypass the create-time ceiling.
      * REMOVED requires confirm_destructive=True (removal is not undoable).
    """
    cid = _cid(customer_id)
    _check_owned(campaign_resource_name, cid, "campaigns")
    status_up = status.upper()
    if status_up not in ("ENABLED", "PAUSED", "REMOVED"):
        raise ToolError("status must be one of ENABLED, PAUSED, REMOVED")
    if status_up == "REMOVED" and not confirm_destructive:
        raise ToolError(
            "Removing a campaign is not undoable. Re-call with "
            "confirm_destructive=True if that is really intended."
        )
    client = utils.get_googleads_client()

    # Enabling spends money — verify the existing budget is within the ceiling.
    if status_up == "ENABLED" and not validate_only:
        try:
            ga = client.get_service("GoogleAdsService")
            # campaign_id is digit-only (enforced by _check_owned above), so it
            # is safe as a GAQL literal — no user text reaches the query.
            campaign_id = _entity_id(campaign_resource_name)
            rows = list(
                ga.search(
                    customer_id=cid,
                    query=(
                        "SELECT campaign.name, campaign_budget.amount_micros "
                        f"FROM campaign WHERE campaign.id = {campaign_id}"
                    ),
                )
            )
            if not rows:
                raise ToolError(f"Campaign {campaign_resource_name} not found in account {cid}.")
            budget_usd = rows[0].campaign_budget.amount_micros / 1_000_000
            if budget_usd > MAX_DAILY_BUDGET_USD:
                raise ToolError(
                    f"Refusing to ENABLE '{rows[0].campaign.name}': its daily budget "
                    f"${budget_usd:.2f} exceeds the safety ceiling ${MAX_DAILY_BUDGET_USD}. "
                    "Lower the budget first (update_campaign_budget)."
                )
        except GoogleAdsException as ex:
            _raise(ex)

    try:
        op = client.get_type("CampaignOperation")
        if status_up == "REMOVED":
            # Removal is a `remove` operation, not a status update.
            op.remove = campaign_resource_name
        else:
            op.update.resource_name = campaign_resource_name
            op.update.status = getattr(client.enums.CampaignStatusEnum, status_up)
            client.copy_from(op.update_mask, field_mask_pb2.FieldMask(paths=["status"]))
        req = client.get_type("MutateCampaignsRequest")
        req.customer_id = cid
        req.operations.append(op)
        req.validate_only = validate_only
        client.get_service("CampaignService").mutate_campaigns(request=req)
        return {"campaign_resource_name": campaign_resource_name, "status": status_up,
                "validated_only": validate_only}
    except GoogleAdsException as ex:
        _raise(ex)


# ── add_sitelinks ────────────────────────────────────────────────────────────
@mutate_mcp.tool(annotations=_MUT)
def add_sitelinks(
    customer_id: str,
    campaign_resource_name: str,
    sitelinks: List[Dict[str, str]],
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Create sitelink assets and attach them to a campaign.

    Campaign-level sitelinks OVERRIDE account-level ones. Without them a
    campaign inherits whatever sits at account level, which is a common way to
    end up advertising an unrelated destination on a paid click.

    Args:
        sitelinks: 2-8 items of {text (<=25 chars), url, description1?,
            description2? (<=35 chars each)}. Urls are validated against the
            allowed ad-destination domains.
    """
    cid = _cid(customer_id)
    _check_owned(campaign_resource_name, cid, "campaigns")
    if not 1 <= len(sitelinks) <= 8:
        raise ToolError("provide between 1 and 8 sitelinks")
    client = utils.get_googleads_client()
    try:
        asset_ops = []
        for s in sitelinks:
            text = _sanitize(str(s.get("text", "")))
            if not 1 <= len(text) <= 25:
                raise ToolError(f"sitelink text must be 1-25 chars, got {text!r}")
            url = _check_final_url(s.get("url", ""))
            op = client.get_type("AssetOperation")
            a = op.create
            a.sitelink_asset.link_text = text
            for i, key in enumerate(("description1", "description2"), start=1):
                d = _sanitize(str(s.get(key, "")))
                if d:
                    if len(d) > 35:
                        raise ToolError(f"{key} must be <=35 chars, got {d!r}")
                    setattr(a.sitelink_asset, key, d)
            a.final_urls.append(url)
            asset_ops.append(op)
        areq = client.get_type("MutateAssetsRequest")
        areq.customer_id = cid
        areq.operations.extend(asset_ops)
        areq.validate_only = validate_only
        ares = client.get_service("AssetService").mutate_assets(request=areq)
        if validate_only:
            return {"sitelinks_validated": len(asset_ops), "validated_only": True}

        link_ops = []
        for r in ares.results:
            op = client.get_type("CampaignAssetOperation")
            x = op.create
            x.campaign = campaign_resource_name
            x.asset = r.resource_name
            x.field_type = client.enums.AssetFieldTypeEnum.SITELINK
            link_ops.append(op)
        lreq = client.get_type("MutateCampaignAssetsRequest")
        lreq.customer_id = cid
        lreq.operations.extend(link_ops)
        lres = client.get_service("CampaignAssetService").mutate_campaign_assets(request=lreq)
        return {"sitelinks_attached": len(lres.results), "campaign": campaign_resource_name}
    except GoogleAdsException as ex:
        _raise(ex)


# ── add_callouts ─────────────────────────────────────────────────────────────
@mutate_mcp.tool(annotations=_MUT)
def add_callouts(
    customer_id: str,
    campaign_resource_name: str,
    callouts: List[str],
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Create callout assets (short benefit phrases) and attach to a campaign.

    Args:
        callouts: 2-10 strings, each <=25 chars.
    """
    cid = _cid(customer_id)
    _check_owned(campaign_resource_name, cid, "campaigns")
    if not 1 <= len(callouts) <= 10:
        raise ToolError("provide between 1 and 10 callouts")
    client = utils.get_googleads_client()
    try:
        asset_ops = []
        for t in callouts:
            text = _sanitize(str(t))
            if not 1 <= len(text) <= 25:
                raise ToolError(f"callout must be 1-25 chars, got {text!r}")
            op = client.get_type("AssetOperation")
            op.create.callout_asset.callout_text = text
            asset_ops.append(op)
        areq = client.get_type("MutateAssetsRequest")
        areq.customer_id = cid
        areq.operations.extend(asset_ops)
        areq.validate_only = validate_only
        ares = client.get_service("AssetService").mutate_assets(request=areq)
        if validate_only:
            return {"callouts_validated": len(asset_ops), "validated_only": True}

        link_ops = []
        for r in ares.results:
            op = client.get_type("CampaignAssetOperation")
            x = op.create
            x.campaign = campaign_resource_name
            x.asset = r.resource_name
            x.field_type = client.enums.AssetFieldTypeEnum.CALLOUT
            link_ops.append(op)
        lreq = client.get_type("MutateCampaignAssetsRequest")
        lreq.customer_id = cid
        lreq.operations.extend(link_ops)
        lres = client.get_service("CampaignAssetService").mutate_campaign_assets(request=lreq)
        return {"callouts_attached": len(lres.results), "campaign": campaign_resource_name}
    except GoogleAdsException as ex:
        _raise(ex)


# ── remove_account_asset_links ───────────────────────────────────────────────
@mutate_mcp.tool(annotations=_DESTRUCTIVE)
def remove_account_asset_links(
    customer_id: str,
    field_type: str = "SITELINK",
    confirm_destructive: bool = False,
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Unlink account-level assets of a given type (SITELINK, CALLOUT, ...).

    Account-level assets are inherited by every campaign that has none of its
    own, so stale ones end up on unrelated ads. This removes the account-level
    LINK only; the underlying assets remain and stay attached wherever they are
    linked at campaign level.

    Requires confirm_destructive=True.
    """
    cid = _cid(customer_id)
    ft = field_type.upper()
    if ft not in REMOVABLE_FIELD_TYPES:
        raise ToolError(
            f"field_type must be one of {sorted(REMOVABLE_FIELD_TYPES)}, got {field_type!r}."
        )
    client = utils.get_googleads_client()
    try:
        ga = client.get_service("GoogleAdsService")
        rows = list(ga.search(customer_id=cid, query=(
            "SELECT customer_asset.resource_name, customer_asset.field_type, "
            "asset.sitelink_asset.link_text, "
            "asset.callout_asset.callout_text FROM customer_asset "
            f"WHERE customer_asset.field_type = '{ft}' AND customer_asset.status = 'ENABLED'"
        )))
        # Defence in depth: never unlink a type we did not ask for, even if the
        # query were somehow broadened.
        rows = [r for r in rows if r.customer_asset.field_type.name == ft]
        if not rows:
            return {"removed": 0, "detail": f"no enabled account-level {ft} assets"}
        removed = [
            (r.asset.sitelink_asset.link_text or r.asset.callout_asset.callout_text)
            for r in rows
        ]
        if not confirm_destructive:
            raise ToolError(
                f"This unlinks {len(rows)} account-level {ft} asset(s): "
                f"{', '.join(x or '(unnamed)' for x in removed)}. "
                "Re-call with confirm_destructive=True if that is intended."
            )
        ops = []
        for r in rows:
            op = client.get_type("CustomerAssetOperation")
            op.remove = r.customer_asset.resource_name
            ops.append(op)
        req = client.get_type("MutateCustomerAssetsRequest")
        req.customer_id = cid
        req.operations.extend(ops)
        req.validate_only = validate_only
        res = client.get_service("CustomerAssetService").mutate_customer_assets(request=req)
        return {
            "field_type": ft,
            "removed": 0 if validate_only else len(res.results),
            "unlinked": removed,
            "validated_only": validate_only,
        }
    except GoogleAdsException as ex:
        _raise(ex)


# ── add_ad_schedule ──────────────────────────────────────────────────────────
@mutate_mcp.tool(annotations=_MUT)
def add_ad_schedule(
    customer_id: str,
    campaign_resource_name: str,
    market_timezone: str,
    day_start_hour: int = 6,
    day_end_hour: int = 22,
    night_bid_modifier: float = 0.55,
    day_bid_modifier: float = 1.0,
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Add a day-parting schedule with bid modifiers, in the MARKET's local time.

    Google runs ad schedules on the ACCOUNT's timezone, which is usually not the
    timezone of the market you're targeting. You give local hours here (e.g.
    06:00-22:00 Sydney) and this converts them to account time for you.

    Covers all 24h so nothing is switched off: the daytime window gets
    `day_bid_modifier` and the remaining night hours get `night_bid_modifier`
    (0.55 = -45%). Set night_bid_modifier low to spend less at night while still
    collecting data — preferable to a hard cut-off before you have hour-of-day
    numbers. The same pattern is applied to all 7 days.

    Args:
        market_timezone: IANA tz of the target market, e.g. "Australia/Sydney",
            "Europe/London".
        day_start_hour / day_end_hour: local daytime window (0-23).
        night_bid_modifier / day_bid_modifier: 0.1-10.0 (1.0 = no change).

    Note: fails if the campaign already has ad-schedule criteria (they may not
    overlap) — remove the existing ones first.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    cid = _cid(customer_id)
    _check_owned(campaign_resource_name, cid, "campaigns")
    for label, mod in (("day_bid_modifier", day_bid_modifier), ("night_bid_modifier", night_bid_modifier)):
        try:
            mod = float(mod)
        except (TypeError, ValueError):
            raise ToolError(f"{label} must be a number, got {mod!r}")
        if not (0.1 <= mod <= 10.0):
            raise ToolError(f"{label} must be between 0.1 and 10.0, got {mod}")
    if not (0 <= int(day_start_hour) <= 23 and 0 <= int(day_end_hour) <= 23):
        raise ToolError("day_start_hour/day_end_hour must be 0-23 (local market time)")

    client = utils.get_googleads_client()
    enums = client.enums

    # Account timezone drives ad schedules — read it rather than assume.
    try:
        ga = client.get_service("GoogleAdsService")
        rows = list(ga.search(customer_id=cid, query="SELECT customer.time_zone FROM customer"))
        account_tz = rows[0].customer.time_zone
    except GoogleAdsException as ex:
        _raise(ex)

    try:
        now = datetime.now(ZoneInfo(account_tz))
        acct_off = now.utcoffset().total_seconds() / 3600
        mkt_off = now.astimezone(ZoneInfo(market_timezone)).utcoffset().total_seconds() / 3600
    except Exception as e:
        raise ToolError(f"Bad timezone ({market_timezone!r} / {account_tz!r}): {e}")
    shift = int(acct_off - mkt_off)  # local hour + shift = account hour

    start = (int(day_start_hour) + shift) % 24
    end = (int(day_end_hour) + shift) % 24

    # Build 24h coverage in account time: daytime blocks + night blocks.
    if start < end:
        day_blocks, night_blocks = [(start, end)], [(0, start), (end, 24)]
    else:  # daytime window wraps past account midnight
        day_blocks, night_blocks = [(start, 24), (0, end)], [(end, start)]
    day_blocks = [(a, b) for a, b in day_blocks if a < b]
    night_blocks = [(a, b) for a, b in night_blocks if a < b]

    days = [enums.DayOfWeekEnum.MONDAY, enums.DayOfWeekEnum.TUESDAY,
            enums.DayOfWeekEnum.WEDNESDAY, enums.DayOfWeekEnum.THURSDAY,
            enums.DayOfWeekEnum.FRIDAY, enums.DayOfWeekEnum.SATURDAY,
            enums.DayOfWeekEnum.SUNDAY]
    try:
        ops = []
        for day in days:
            for blocks, modifier in ((day_blocks, float(day_bid_modifier)),
                                     (night_blocks, float(night_bid_modifier))):
                for start_h, end_h in blocks:
                    op = client.get_type("CampaignCriterionOperation")
                    crit = op.create
                    crit.campaign = campaign_resource_name
                    crit.bid_modifier = modifier
                    crit.ad_schedule.day_of_week = day
                    crit.ad_schedule.start_hour = start_h
                    crit.ad_schedule.start_minute = enums.MinuteOfHourEnum.ZERO
                    crit.ad_schedule.end_hour = end_h
                    crit.ad_schedule.end_minute = enums.MinuteOfHourEnum.ZERO
                    ops.append(op)
        req = client.get_type("MutateCampaignCriteriaRequest")
        req.customer_id = cid
        req.operations.extend(ops)
        req.validate_only = validate_only
        res = client.get_service("CampaignCriterionService").mutate_campaign_criteria(request=req)
        return {
            "account_timezone": account_tz,
            "market_timezone": market_timezone,
            "shift_hours": shift,
            "local_day_window": f"{int(day_start_hour):02d}:00-{int(day_end_hour):02d}:00",
            "account_day_blocks": [f"{a:02d}:00-{b:02d}:00" for a, b in day_blocks],
            "account_night_blocks": [f"{a:02d}:00-{b:02d}:00" for a, b in night_blocks],
            "day_bid_modifier": float(day_bid_modifier),
            "night_bid_modifier": float(night_bid_modifier),
            "criteria_added": 0 if validate_only else len(res.results),
            "validated_only": validate_only,
        }
    except GoogleAdsException as ex:
        _raise(ex)


# ── update_campaign_budget ───────────────────────────────────────────────────
@mutate_mcp.tool(annotations=_MUT)
def update_campaign_budget(
    customer_id: str,
    budget_resource_name: str,
    daily_budget_usd: float,
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Change a campaign budget's daily amount (<= safety ceiling)."""
    _check_budget(daily_budget_usd)
    cid = _cid(customer_id)
    _check_owned(budget_resource_name, cid, "campaignBudgets")
    client = utils.get_googleads_client()
    try:
        op = client.get_type("CampaignBudgetOperation")
        op.update.resource_name = budget_resource_name
        op.update.amount_micros = _micros(daily_budget_usd)
        client.copy_from(op.update_mask, field_mask_pb2.FieldMask(paths=["amount_micros"]))
        req = client.get_type("MutateCampaignBudgetsRequest")
        req.customer_id = cid
        req.operations.append(op)
        req.validate_only = validate_only
        client.get_service("CampaignBudgetService").mutate_campaign_budgets(request=req)
        return {"budget_resource_name": budget_resource_name, "daily_budget_usd": daily_budget_usd,
                "validated_only": validate_only}
    except GoogleAdsException as ex:
        _raise(ex)
