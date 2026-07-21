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


# ── helpers ──────────────────────────────────────────────────────────────────
def _cid(customer_id: str) -> str:
    return str(customer_id).replace("-", "").strip()


def _micros(usd: float) -> int:
    """USD → micros as an exact multiple of the $0.01 billable unit."""
    return int(round(float(usd) * 100)) * 10_000


def _sanitize(text: str) -> str:
    """Strip characters Google Ads' SYMBOLS policy rejects in ad text."""
    for a, b in (("★", ""), ("~", ""), ("—", "-"), ("–", "-")):
        text = text.replace(a, b)
    return text.replace("  ", " ").strip()


def _check_budget(daily_budget_usd: float) -> None:
    if daily_budget_usd > MAX_DAILY_BUDGET_USD:
        raise ToolError(
            f"Daily budget ${daily_budget_usd} exceeds the safety ceiling "
            f"${MAX_DAILY_BUDGET_USD}. Lower it or raise MAX_DAILY_BUDGET_USD."
        )


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
            c.final_url_suffix = final_url_suffix
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
        ag.cpc_bid_micros = _micros(default_cpc_usd)
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
                crit.cpc_bid_micros = _micros(kw["cpc_usd"])
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
) -> Dict[str, Any]:
    """Set a campaign's status: ENABLED, PAUSED or REMOVED.

    This is the ONLY way a campaign starts spending (status=ENABLED).
    """
    cid = _cid(customer_id)
    status_up = status.upper()
    if status_up not in ("ENABLED", "PAUSED", "REMOVED"):
        raise ToolError("status must be one of ENABLED, PAUSED, REMOVED")
    client = utils.get_googleads_client()
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
