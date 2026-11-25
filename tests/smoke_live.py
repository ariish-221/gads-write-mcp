"""Live smoke test over stdio. Changes nothing in any account.

Every plan is a validate-only request, the server runs with GADS_READ_ONLY=true, and the
status/budget plans restate the current value, so they would be no-ops even if executed.
The one executed mutate targets a deactivated account, where Google rejects everything.
The conversion checks send a made-up click id and order id, validate-only, so there is
nothing Google could import or retract.

    .venv/bin/python tests/smoke_live.py CUSTOMER_ID CAMPAIGN_ID DEACTIVATED_CUSTOMER_ID
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from gads_write_mcp import ads, conversions
from gads_write_mcp.config import Settings
from gads_write_mcp.guide import RECIPES

SERVER = str(Path(sys.executable).with_name("gads-write-mcp"))
EXPECTED_TOOLS = 14
FAKE_GCLID = "zz-smoke-test-not-a-real-click-id-0001"
FAKE_EMAIL = "zz.smoke.test@example.com"
passed = 0
recipe_results: dict[str, bool] = {}


def check(label: str, condition: bool, detail: object = "") -> None:
    global passed
    if not condition:
        raise SystemExit(f"FAIL  {label}\n      {detail}")
    passed += 1
    print(f"ok    {label}")


def text(result: types.CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def environment(state_dir: str, **extra: str) -> dict[str, str]:
    # No developer token on purpose: access is decided by the OAuth client's Cloud project.
    keep = ("PATH", "HOME", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG")
    return {**{k: os.environ[k] for k in keep if k in os.environ}, "GADS_STATE_DIR": state_dir, **extra}


def session(env: dict[str, str], answer: str | None = None):
    async def on_elicit(context, params: types.ElicitRequestParams):
        print(f"      dialog shown to the user:\n        " + params.message.replace("\n", "\n        "))
        return types.ElicitResult(action=answer, content={"approve": True} if answer == "accept" else None)

    return stdio_client(StdioServerParameters(command=SERVER, env=env)), on_elicit if answer else None


async def read_only_run(customer_id: str, campaign_id: str, state_dir: str) -> None:
    client = ads.get_client(None)
    campaign = ads.search(
        client, customer_id,
        f"SELECT campaign.status, campaign_budget.amount_micros FROM campaign WHERE campaign.id = {campaign_id}",
    )[0]
    status = campaign["campaign"]["status"]
    budget = int(campaign["campaign_budget"]["amount_micros"]) / 1_000_000
    ad_rows = ads.search(
        client, customer_id,
        "SELECT ad_group.id, ad_group_ad.ad.id, ad_group_ad.ad.final_urls FROM ad_group_ad "
        f"WHERE campaign.id = {campaign_id} AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD' LIMIT 1",
    )
    check("reads work with no developer token in the environment", status in ("ENABLED", "PAUSED"), campaign)

    transport, _ = session(environment(state_dir, GADS_READ_ONLY="true"))
    async with transport as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            tools = (await mcp.list_tools()).tools
            check(f"server lists {EXPECTED_TOOLS} tools", len(tools) == EXPECTED_TOOLS, [t.name for t in tools])

            result = await mcp.call_tool("gads_get_guardrails")
            limits = result.structured_content
            check("guardrails: fail-closed defaults", limits["allowed_customer_ids"] == [] and limits["read_only"] is True, limits)

            result = await mcp.call_tool("gads_plan_set_status", {"customer_id": customer_id, "entity_type": "campaign", "ids": [campaign_id], "status": status, "note": "smoke test, no-op"})
            check("status plan validated by Google", not result.is_error and result.structured_content["validated_by_google"], text(result))
            plan = result.structured_content
            change = plan["operations"][0]
            check("plan shows name and from -> to", change.get("label") and change["changes"]["status"] == {"from": status, "to": status}, change)

            result = await mcp.call_tool("gads_plan_update_budget", {"customer_id": customer_id, "campaign_id": campaign_id, "daily_budget": budget})
            check("budget plan validated (same amount)", not result.is_error, text(result))

            result = await mcp.call_tool("gads_plan_update_budget", {"customer_id": customer_id, "campaign_id": campaign_id, "daily_budget": 5000})
            check("budget above cap is blocked locally", result.is_error and "exceeds the cap" in text(result), text(result))

            result = await mcp.call_tool("gads_plan_mutate", {"customer_id": customer_id, "operations": [{"resource": "campaign", "action": "remove", "resource_name": f"customers/{customer_id}/campaigns/{campaign_id}"}]})
            check("campaign removal is blocked", result.is_error and "irreversible" in text(result), text(result))

            result = await mcp.call_tool("gads_plan_mutate", {"customer_id": customer_id, "operations": [{"resource": "campaign", "action": "update", "data": {"resource_name": f"customers/9999999999/campaigns/{campaign_id}", "status": status}}]})
            check("cross-account operation is blocked", result.is_error and "belongs to account" in text(result), text(result))

            result = await mcp.call_tool("gads_plan_mutate", {"customer_id": customer_id, "operations": [{"resource": "campaign", "action": "update", "data": {"resource_name": f"customers/{customer_id}/campaigns/{campaign_id}", "status": "NOT_A_STATUS"}}]})
            check("bad enum is explained", result.is_error and "Bad field" in text(result), text(result))

            result = await mcp.call_tool("gads_plan_set_status", {"customer_id": customer_id, "entity_type": "campaign", "ids": ["1"], "status": "PAUSED"})
            check("Google's rejection is passed through with a request id", result.is_error and "request id" in text(result), text(result))

            result = await mcp.call_tool("gads_plan_add_negative_keywords", {"customer_id": customer_id, "level": "campaign", "parent_id": campaign_id, "keywords": [{"text": "zz smoke test", "match_type": "EXACT"}]})
            check("negative keyword plan validated (create)", not result.is_error, text(result))

            if ad_rows:
                ad_group_id = ad_rows[0]["ad_group"]["id"]
                url = ad_rows[0]["ad_group_ad"]["ad"]["final_urls"][0]
                result = await mcp.call_tool("gads_plan_create_rsa", {
                    "customer_id": customer_id, "ad_group_id": ad_group_id, "final_url": url,
                    "headlines": ["Team Painting Workshops", "Book A Group Session", "Creative Team Building"],
                    "descriptions": ["Guided painting sessions for teams of any size.", "All supplies included. Book your date online."],
                })
                check("RSA plan validated, forced PAUSED", not result.is_error and result.structured_content["operations"][0]["create"]["status"] == "PAUSED", text(result))

                result = await mcp.call_tool("gads_plan_add_keywords", {"customer_id": customer_id, "ad_group_id": ad_group_id, "keywords": [{"text": "zz smoke test keyword", "match_type": "PHRASE", "cpc_bid": 99}]})
                check("keyword bid above cap is blocked", result.is_error and "GADS_MAX_CPC_BID" in text(result), text(result))
            else:
                print("skip  no responsive search ad in this campaign; RSA and keyword checks skipped")

            await recipe_report(mcp, customer_id, campaign_id, ad_rows)
            await conversion_checks(mcp, client, customer_id, state_dir)

            result = await mcp.call_tool("gads_apply_plan", {"plan_id": plan["plan_id"]})
            check("apply refused in read-only mode", result.is_error and "read-only" in text(result), text(result))

            result = await mcp.call_tool("gads_list_plans", {"status": "pending"})
            check("pending plans are listed", result.structured_content["count"] >= 3, result.structured_content["count"])

    transport, _ = session(environment(state_dir))
    async with transport as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            result = await mcp.call_tool("gads_apply_plan", {"plan_id": plan["plan_id"]})
            check("apply refused for an account that is not allowlisted", result.is_error and "not approved for writes" in text(result), text(result))


async def recipe_report(mcp: ClientSession, customer_id: str, campaign_id: str, ad_rows: list) -> None:
    """Have Google validate the payload guide's recipes. Reported, not asserted: a rejection
    can come from the account's own state (a name already in use, a location already targeted)."""
    stamp = f" zz-smoke-{int(time.time())}"
    ad_id = ad_rows[0]["ad_group_ad"]["ad"]["id"] if ad_rows else None
    for topic in ("search_campaign", "sitelinks_callouts_snippets", "location_language", "network_settings", "shared_negative_list", "rsa_text_update"):
        if topic == "rsa_text_update" and not ad_id:
            continue
        raw = json.dumps(RECIPES[topic]["operations"]).replace("1234567890", customer_id).replace("campaigns/555", f"campaigns/{campaign_id}").replace("ads/789", f"ads/{ad_id}")
        operations = [op for op in json.loads(raw) if op["action"] != "remove"]
        for op in operations:
            if op["action"] == "create" and "name" in op["data"]:
                op["data"]["name"] += stamp
        result = await mcp.call_tool("gads_plan_mutate", {"customer_id": customer_id, "operations": operations, "note": f"smoke test recipe {topic}"})
        verdict = "REVIEW" if result.is_error else "ok    "
        recipe_results[topic] = not result.is_error
        print(f"{verdict} recipe '{topic}' validated by Google" + (f"\n      {text(result)}" if result.is_error else ""))


async def conversion_checks(mcp: ClientSession, client, customer_id: str, state_dir: str) -> None:
    """Offline conversions, validate-only. What Google answers depends on setup the user controls
    (Data Manager API enabled, OAuth scope), so those answers are reported; the server's own
    behaviour is asserted."""
    when = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 2 * 86400))
    row = {"gclid": FAKE_GCLID, "email": FAKE_EMAIL, "conversion_time": when, "value": 1, "currency": "CAD", "order_id": "zz-smoke-0001"}

    result = await mcp.call_tool("gads_plan_upload_click_conversions", {"customer_id": customer_id, "conversion_action_id": "1", "rows": [row]})
    check("unknown conversion action is blocked before Google sees a row", result.is_error and "No conversion action 1" in text(result), text(result))
    result = await mcp.call_tool("gads_plan_upload_click_conversions", {"customer_id": customer_id, "conversion_action_id": "1", "rows": [{**row, "value": 99_999_999}]})
    check("bad rows are blocked locally, by row", result.is_error and "row[0]" in text(result), text(result))

    actions = ads.search(client, customer_id, "SELECT conversion_action.id, conversion_action.name FROM conversion_action WHERE conversion_action.type = 'UPLOAD_CLICKS' AND conversion_action.status = 'ENABLED' LIMIT 1")
    if not actions:
        print("skip  this account has no enabled UPLOAD_CLICKS conversion action; upload and retraction validation skipped")
        return
    action_id = str(actions[0]["conversion_action"]["id"])
    action_name = f"customers/{customer_id}/conversionActions/{action_id}"

    result = await mcp.call_tool("gads_plan_upload_click_conversions", {"customer_id": customer_id, "conversion_action_id": action_id, "rows": [row], "note": "smoke test, validate-only"})
    outcome = text(result)
    clean = (not result.is_error) or "What to do:" in outcome or "- row[" in outcome or "Blocked by guardrails" in outcome
    check("upload plan: validated by Google, or refused with a reason and a next step", clean, outcome)
    if result.is_error:
        print("REVIEW upload plan was refused. Google's answer, with what to do about it:\n      " + outcome.replace("\n", "\n      "))
    else:
        print(f"ok    Data Manager API validated the upload (validate-only): {result.structured_content['validation']}")
    stored = "\n".join(path.read_text() for path in Path(state_dir).rglob("*") if path.is_file())
    check("the raw email is nowhere in the state directory or the result", FAKE_EMAIL not in stored and FAKE_EMAIL not in outcome and "zz.smoke" not in stored)

    settings = Settings.from_env()
    try:
        errors = ads.upload_adjustments(client, settings, customer_id, [conversions.retraction(row, action_name, time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))], validate_only=True)
        if errors[0]:
            print(f"ok    retraction of a made-up conversion is refused by row (adjustment service is open to this project): {errors[0][:200]}")
        else:
            print("REVIEW Google's validate-only accepted the retraction of a conversion that does not exist. Retraction plans then prove the format only, not that Google can find the conversions.")
    except ads.AdsError as exc:
        print("REVIEW retraction validation was refused outright (no undo through the API if this is the allowlist error):\n      " + str(exc).replace("\n", "\n      "))

    request = client.get_type("UploadClickConversionsRequest")
    request.customer_id, request.partial_failure, request.validate_only = customer_id, True, True
    conversion = request.conversions.add()
    conversion.gclid, conversion.conversion_action, conversion.conversion_date_time = FAKE_GCLID, action_name, conversions.ads_time(when)
    # Google answers the allowlist refusal either as an exception or, with partial_failure, as a row error.
    try:
        response = client.get_service("ConversionUploadService").upload_click_conversions(request=request)
        answer = str(ads.partial_failures(client, ads._pb(response).partial_failure_error, 1)[0])
    except Exception as exc:  # noqa: BLE001 - reported, whatever it is
        answer = str(getattr(exc, "failure", exc))
    if "CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE" in answer:
        print("note  the legacy UploadClickConversions is CLOSED to this project (CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE), as Google announced for new adopters. Uploads must use the Data Manager API.")
    else:
        print(f"note  the legacy UploadClickConversions answered without the allowlist refusal, so it may be open to this project: {answer[:200]}. The server still uses the Data Manager API, Google's stated replacement.")

async def confirmation_run(dead_customer_id: str, state_dir: str) -> None:
    """Full apply path against a deactivated account, where nothing can change."""
    plans = Path(state_dir) / "plans"
    plans.mkdir(parents=True, exist_ok=True)

    def stage(plan_id: str) -> None:
        (plans / f"{plan_id}.json").write_text(json.dumps({
            "plan_id": plan_id, "status": "pending", "customer_id": dead_customer_id, "note": "smoke test",
            "created_at": "2026-01-01T00:00:00+00:00", "expires_at": "2099-01-01T00:00:00+00:00", "warnings": [],
            "operations": [{"resource": "campaign", "action": "update", "data": {"resource_name": f"customers/{dead_customer_id}/campaigns/1", "status": "PAUSED"}}],
            "before": [{"name": "placeholder", "status": "PAUSED"}],
        }))

    env = environment(state_dir, GADS_ALLOWED_CUSTOMER_IDS=dead_customer_id)
    for answer, plan_id, expected in (("decline", "plan_000000d1", "declined"), ("accept", "plan_000000a1", "Google Ads rejected")):
        stage(plan_id)
        transport, on_elicit = session(env, answer)
        async with transport as (read, write):
            async with ClientSession(read, write, elicitation_callback=on_elicit) as mcp:
                await mcp.initialize()
                result = await mcp.call_tool("gads_apply_plan", {"plan_id": plan_id})
                check(f"confirmation dialog {answer} -> {expected!r}", result.is_error and expected in text(result), text(result))

    stage("plan_000000c1")
    transport, _ = session(env)
    async with transport as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            result = await mcp.call_tool("gads_apply_plan", {"plan_id": "plan_000000c1"})
            check("client without a confirmation dialog is refused", result.is_error and "confirmation dialog" in text(result), text(result))

    events = [json.loads(line)["event"] for line in (Path(state_dir) / "audit.jsonl").read_text().splitlines()]
    check("audit log recorded every step", {"plan_validated", "plan_blocked", "plan_rejected", "apply_declined", "apply_failed"} <= set(events), sorted(set(events)))
    check("audit log never recorded a successful apply", "applied" not in events, events)


async def main() -> None:
    customer_id, campaign_id, dead_customer_id = sys.argv[1:4]
    with tempfile.TemporaryDirectory() as state_dir:
        await read_only_run(customer_id, campaign_id, state_dir)
        await confirmation_run(dead_customer_id, state_dir)
    review = [topic for topic, ok in recipe_results.items() if not ok]
    print(f"\n{passed} checks passed. No account was changed.")
    print(f"Recipes validated by Google: {len(recipe_results) - len(review)}/{len(recipe_results)}" + (f". Needs review: {', '.join(review)}" if review else ""))


if __name__ == "__main__":
    asyncio.run(main())
