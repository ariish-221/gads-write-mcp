"""Payload recipes for gads_plan_mutate, checked against the v25 protos and Google's docs (2026-09-20).

Every recipe is parsed against the installed protos in tests, so field names cannot drift
silently on a library upgrade. 1234567890 stands in for the account id.
"""

from __future__ import annotations

from typing import Any

_C = "customers/1234567890"

RECIPES: dict[str, dict[str, Any]] = {
    "search_campaign": {
        "summary": "New Search campaign with its own budget, an ad group and a keyword, in one atomic plan.",
        "rules": [
            "Temporary resource names are negative, unique in the plan, and must be created before they are referenced.",
            "campaign_budget.explicitly_shared defaults to true; send false for a campaign's own budget.",
            "contains_eu_political_advertising is required on every campaign create. It is a legal declaration: ask the user, never assume.",
            "Set exactly one bidding strategy: maximize_conversions {}, maximize_conversion_value {}, target_spend {}, manual_cpc {}, target_impression_share {...}, or bidding_strategy (portfolio).",
            "Dates are start_date_time / end_date_time ('yyyy-MM-dd HH:mm:ss'); start_date and end_date no longer exist.",
            "target_search_network requires target_google_search. Campaign-level broad match (keyword_match_type) can no longer be created.",
            "The server forces new campaigns and ads to PAUSED.",
        ],
        "operations": [
            {"resource": "campaign_budget", "action": "create", "data": {"resource_name": f"{_C}/campaignBudgets/-1", "name": "Brand Search budget", "amount_micros": 20000000, "delivery_method": "STANDARD", "explicitly_shared": False}},
            {"resource": "campaign", "action": "create", "data": {"resource_name": f"{_C}/campaigns/-2", "name": "Brand Search", "advertising_channel_type": "SEARCH", "status": "PAUSED", "campaign_budget": f"{_C}/campaignBudgets/-1", "maximize_conversions": {}, "network_settings": {"target_google_search": True, "target_search_network": False, "target_content_network": False, "target_partner_search_network": False}, "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}},
            {"resource": "ad_group", "action": "create", "data": {"resource_name": f"{_C}/adGroups/-3", "campaign": f"{_C}/campaigns/-2", "name": "Brand", "type": "SEARCH_STANDARD", "status": "ENABLED"}},
            {"resource": "ad_group_criterion", "action": "create", "data": {"ad_group": f"{_C}/adGroups/-3", "status": "ENABLED", "keyword": {"text": "brand name", "match_type": "PHRASE"}}},
        ],
    },
    "sitelinks_callouts_snippets": {
        "summary": "Create an asset, then link it to a campaign with campaign_asset (or ad_group_asset / customer_asset).",
        "rules": [
            "Sitelink: link_text 1-25 chars; description1 and description2 1-35 chars, set both or neither; final_urls sits on the asset, not inside sitelink_asset.",
            "Callout: callout_text 1-25 chars.",
            "Structured snippet: header must be one of Google's predefined headers (Brands, Amenities, Styles, Types, Destinations, Services, Courses, Neighborhoods, Shows, Insurance coverage, Degree programs, Featured hotels, Models); 3-10 values of 1-25 chars.",
            "On a link only status can be updated. To change text, create a new asset and link it; remove the old link.",
        ],
        "operations": [
            {"resource": "asset", "action": "create", "data": {"resource_name": f"{_C}/assets/-1", "final_urls": ["https://example.com/pricing"], "sitelink_asset": {"link_text": "Pricing", "description1": "Plans for every team", "description2": "No setup fees"}}},
            {"resource": "campaign_asset", "action": "create", "data": {"campaign": f"{_C}/campaigns/555", "asset": f"{_C}/assets/-1", "field_type": "SITELINK"}},
            {"resource": "asset", "action": "create", "data": {"resource_name": f"{_C}/assets/-2", "callout_asset": {"callout_text": "Free consultation"}}},
            {"resource": "campaign_asset", "action": "create", "data": {"campaign": f"{_C}/campaigns/555", "asset": f"{_C}/assets/-2", "field_type": "CALLOUT"}},
            {"resource": "asset", "action": "create", "data": {"resource_name": f"{_C}/assets/-3", "structured_snippet_asset": {"header": "Services", "values": ["Audits", "Campaign builds", "Reporting"]}}},
            {"resource": "campaign_asset", "action": "create", "data": {"campaign": f"{_C}/campaigns/555", "asset": f"{_C}/assets/-3", "field_type": "STRUCTURED_SNIPPET"}},
        ],
    },
    "location_language": {
        "summary": "Targeting is campaign_criterion rows. They are create/remove only.",
        "rules": [
            "location, language, proximity, negative and campaign are immutable: to change targeting, remove the criterion and create another. Only bid_modifier (0.1-10.0) and status can be updated.",
            "Find ids with GAQL on geo_target_constant (e.g. Canada 2124, Toronto 1002451) and language_constant (English 1000, French 1002).",
            "From late September 2026 Google rejects adding or changing language criteria on Search campaigns (OPERATION_NOT_PERMITTED_FOR_CONTEXT).",
            "A campaign without an EU political advertising declaration rejects location changes.",
        ],
        "operations": [
            {"resource": "campaign_criterion", "action": "create", "data": {"campaign": f"{_C}/campaigns/555", "location": {"geo_target_constant": "geoTargetConstants/2124"}}},
            {"resource": "campaign_criterion", "action": "create", "data": {"campaign": f"{_C}/campaigns/555", "negative": True, "location": {"geo_target_constant": "geoTargetConstants/1002451"}}},
            {"resource": "campaign_criterion", "action": "create", "data": {"campaign": f"{_C}/campaigns/555", "proximity": {"address": {"city_name": "Toronto", "postal_code": "M5V 2T6", "country_code": "CA"}, "radius": 15, "radius_units": "KILOMETERS"}}},
            {"resource": "campaign_criterion", "action": "remove", "resource_name": f"{_C}/campaignCriteria/555~2124"},
        ],
    },
    "network_settings": {
        "summary": "Turn Search partners or the Display network on or off for a campaign.",
        "rules": [
            "Send only the flags you are changing; the mask is built from the keys present.",
            "target_search_network requires target_google_search to be true.",
        ],
        "operations": [
            {"resource": "campaign", "action": "update", "data": {"resource_name": f"{_C}/campaigns/555", "network_settings": {"target_content_network": False, "target_search_network": False}}},
        ],
    },
    "shared_negative_list": {
        "summary": "A reusable negative keyword list attached to campaigns.",
        "rules": [
            "shared_criterion rows are immutable: create and remove only.",
            "shared_set names must be unique among active lists.",
            "Chaining the three creates with temporary names is not shown in Google's samples; if validation rejects it, create the list first and attach it in a second plan.",
        ],
        "operations": [
            {"resource": "shared_set", "action": "create", "data": {"resource_name": f"{_C}/sharedSets/-1", "name": "Job seekers", "type": "NEGATIVE_KEYWORDS"}},
            {"resource": "shared_criterion", "action": "create", "data": {"shared_set": f"{_C}/sharedSets/-1", "keyword": {"text": "jobs", "match_type": "BROAD"}}},
            {"resource": "campaign_shared_set", "action": "create", "data": {"campaign": f"{_C}/campaigns/555", "shared_set": f"{_C}/sharedSets/-1"}},
        ],
    },
    "rsa_text_update": {
        "summary": "Edit an existing responsive search ad in place (keeps its history).",
        "rules": [
            "Use the 'ad' resource (customers/ID/ads/AD_ID), not ad_group_ad.",
            "Lists are replaced whole: send every headline and description you want to keep. Minimum 3 headlines and 2 descriptions; limits 15 x 30 chars and 4 x 90 chars.",
            "pinned_field is optional: HEADLINE_1..3, DESCRIPTION_1..2.",
        ],
        "operations": [
            {"resource": "ad", "action": "update", "data": {"resource_name": f"{_C}/ads/789", "responsive_search_ad": {"headlines": [{"text": "Team Painting Workshops", "pinned_field": "HEADLINE_1"}, {"text": "Book A Group Session"}, {"text": "Creative Team Building"}], "descriptions": [{"text": "Guided painting sessions for teams of any size."}, {"text": "All supplies included. Book your date online."}]}}},
        ],
    },
    "image_asset": {
        "summary": "Upload an image as an asset.",
        "rules": [
            "image_asset.data is base64 of the file bytes. JPEG, PNG or GIF; Performance Max caps images at 5120 KB.",
            "Large images make large tool calls. Prefer uploading in the Google Ads UI and linking the existing asset by resource name.",
            "Uploading bytes that already exist returns the existing asset and drops the new name.",
        ],
        "operations": [
            {"resource": "asset", "action": "create", "data": {"resource_name": f"{_C}/assets/-1", "name": "Hero 1200x628", "type": "IMAGE", "image_asset": {"data": "iVBORw0KGgo="}}},
        ],
    },
    "performance_max": {
        "summary": "Performance Max campaign with one asset group. Never validated live by this server: expect iteration.",
        "rules": [
            "Everything goes in ONE plan, in this order: budget, campaign, assets, asset_group, asset_group_asset links, brand campaign_asset links. An asset group cannot be created without all its required assets.",
            "Budget must not be shared. Bidding is maximize_conversions or maximize_conversion_value only. Do not set advertising_channel_sub_type. The account needs at least one conversion action.",
            "Required per asset group: HEADLINE 3-15 (30 chars), LONG_HEADLINE 1-5 (90), DESCRIPTION 2-5 (90), MARKETING_IMAGE 1-20 (1.91:1, min 600x314), SQUARE_MARKETING_IMAGE 1-20 (1:1, min 300x300).",
            "With brand_guidelines_enabled true (the default for new campaigns, cannot be turned off later) BUSINESS_NAME (exactly 1, 25 chars) and LOGO (1-5, 1:1, min 128x128) are campaign_asset links, not asset_group_asset links.",
            "Listing group filters are for Merchant Center (retail) campaigns only.",
            "Text assets are plain assets: {'text_asset': {'text': '...'}}.",
        ],
        "operations": [
            {"resource": "campaign_budget", "action": "create", "data": {"resource_name": f"{_C}/campaignBudgets/-1", "name": "PMax budget", "amount_micros": 30000000, "explicitly_shared": False}},
            {"resource": "campaign", "action": "create", "data": {"resource_name": f"{_C}/campaigns/-2", "name": "PMax", "status": "PAUSED", "advertising_channel_type": "PERFORMANCE_MAX", "campaign_budget": f"{_C}/campaignBudgets/-1", "maximize_conversions": {}, "brand_guidelines_enabled": True, "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}},
            {"resource": "asset", "action": "create", "data": {"resource_name": f"{_C}/assets/-10", "text_asset": {"text": "Team Painting Workshops"}}},
            {"resource": "asset_group", "action": "create", "data": {"resource_name": f"{_C}/assetGroups/-3", "campaign": f"{_C}/campaigns/-2", "name": "Main", "final_urls": ["https://example.com"], "status": "PAUSED"}},
            {"resource": "asset_group_asset", "action": "create", "data": {"asset_group": f"{_C}/assetGroups/-3", "asset": f"{_C}/assets/-10", "field_type": "HEADLINE"}},
            {"resource": "campaign_asset", "action": "create", "data": {"campaign": f"{_C}/campaigns/-2", "asset": f"{_C}/assets/-11", "field_type": "BUSINESS_NAME"}},
        ],
    },
}

NOT_POSSIBLE = {
    "conversion value restatements, call conversions, store sales": "not built. Click conversion uploads (gads_plan_upload_click_conversions) and their retraction (gads_plan_rollback) are",
    "custom audiences": "CustomAudienceService; not part of GoogleAdsService.Mutate",
    "keyword ideas and forecasts": "planning services are blocked at Explorer access; needs Basic",
    "invoices, billing setup, account budgets": "billing services are blocked at Explorer access",
    "user access and invitations": "user management is blocked at Explorer access",
    "auction insights": "allowlist-only metrics; not available to ordinary projects at any access level",
}
