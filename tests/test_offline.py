"""Offline tests: no network, no credentials. Run: .venv/bin/python -m unittest discover tests -v"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials
from pydantic import ValidationError

from gads_write_mcp import ads, conversions, datamanager, guardrails
from gads_write_mcp.config import Settings, clean_customer_id
from gads_write_mcp.ops import Operation, OperationError, build, leaf_paths, normalize
from gads_write_mcp.plans import PlanError, PlanStore

CID = "1234567890"
CLIENT = GoogleAdsClient(credentials=AnonymousCredentials(), use_proto_plus=False)


def settings(**overrides) -> Settings:
    os.environ.pop("GADS_ALLOWED_CUSTOMER_IDS", None)
    return replace(Settings.from_env(), **overrides)


def update(resource: str, name: str, **fields) -> Operation:
    return Operation(resource=resource, action="update", data={"resource_name": name, **fields})


class OpsTest(unittest.TestCase):
    def test_camel_case_keys_are_normalized(self):
        self.assertEqual(normalize({"cpcBidMicros": 1, "keyword": {"matchType": "EXACT"}}), {"cpc_bid_micros": 1, "keyword": {"match_type": "EXACT"}})

    def test_mask_keeps_falsy_values(self):
        self.assertEqual(leaf_paths({"negative": False, "amount_micros": 0, "keyword": {"text": "x"}}), ["negative", "amount_micros", "keyword.text"])

    def test_shape_is_validated(self):
        with self.assertRaises(ValidationError):
            Operation(resource="campaign", action="update", data={"status": "PAUSED"})
        with self.assertRaises(ValidationError):
            Operation(resource="campaign", action="remove", data={"x": 1}, resource_name=f"customers/{CID}/campaigns/1")
        with self.assertRaises(ValidationError):
            Operation(resource="campaign", action="create")

    def test_update_builds_proto_and_mask(self):
        message = build(CLIENT, update("campaign", f"customers/{CID}/campaigns/5", status="PAUSED"))
        operation = message.campaign_operation
        self.assertEqual(message.WhichOneof("operation"), "campaign_operation")
        self.assertEqual(operation.update.resource_name, f"customers/{CID}/campaigns/5")
        self.assertEqual(list(operation.update_mask.paths), ["status"])
        self.assertEqual(operation.update.status, CLIENT.enums.CampaignStatusEnum.PAUSED)

    def test_string_micros_and_nested_create(self):
        op = Operation(
            resource="adGroupCriterion",
            action="create",
            data={"adGroup": f"customers/{CID}/adGroups/7", "keyword": {"text": "paint night", "matchType": "PHRASE"}, "cpcBidMicros": "1500000"},
        )
        created = build(CLIENT, op).ad_group_criterion_operation.create
        self.assertEqual(created.keyword.text, "paint night")
        self.assertEqual(created.cpc_bid_micros, 1_500_000)

    def test_reserved_word_fields_use_the_api_name_in_masks_and_diffs(self):
        from gads_write_mcp.ops import dig

        for key in ("type", "type_"):
            data = {"resource_name": f"customers/{CID}/sharedSets/1", key: "NEGATIVE_KEYWORDS", "name": "n"}
            op = Operation(resource="shared_set", action="update", data=data)
            self.assertEqual(op.mask, ["type", "name"])
            self.assertEqual(list(build(CLIENT, op).shared_set_operation.update_mask.paths), ["type", "name"])
            self.assertEqual(dig(op.data, "type"), "NEGATIVE_KEYWORDS")
        self.assertEqual(dig({"type_": "SEARCH_STANDARD"}, "type"), "SEARCH_STANDARD")

    def test_remove(self):
        name = f"customers/{CID}/adGroupCriteria/7~9"
        message = build(CLIENT, Operation(resource="ad_group_criterion", action="remove", resource_name=name))
        self.assertEqual(message.ad_group_criterion_operation.remove, name)

    def test_unknown_resource_suggests_a_fix(self):
        with self.assertRaisesRegex(OperationError, "campaign_budget"):
            build(CLIENT, Operation(resource="campaign_budgets", action="create", data={"name": "x"}))

    def test_bad_field_is_reported(self):
        with self.assertRaisesRegex(OperationError, "Bad field"):
            build(CLIENT, update("campaign", f"customers/{CID}/campaigns/5", not_a_field=1))

    def test_unsupported_action_is_reported(self):
        with self.assertRaisesRegex(OperationError, "does not support update"):
            build(CLIENT, update("campaign_label", f"customers/{CID}/campaignLabels/1~2", status="x"))


class GuideTest(unittest.TestCase):
    """The recipes handed to the model must keep parsing against whatever protos are installed."""

    def test_every_recipe_parses_and_passes_the_guardrails(self):
        from gads_write_mcp.guide import RECIPES

        for topic, recipe in RECIPES.items():
            with self.subTest(topic=topic):
                operations = [Operation(**raw) for raw in recipe["operations"]]
                review = guardrails.review(CID, operations, settings())
                self.assertTrue(review.ok, review.violations)
                for op in operations:
                    build(CLIENT, op)

    def test_empty_message_selects_the_bidding_strategy(self):
        from gads_write_mcp.guide import RECIPES

        campaign = Operation(**RECIPES["search_campaign"]["operations"][1])
        created = build(CLIENT, campaign).campaign_operation.create
        self.assertEqual(created.WhichOneof("campaign_bidding_strategy"), "maximize_conversions")
        self.assertFalse(created.network_settings.target_content_network)

    def test_image_bytes_are_decoded_from_base64(self):
        from gads_write_mcp.guide import RECIPES

        created = build(CLIENT, Operation(**RECIPES["image_asset"]["operations"][0])).asset_operation.create
        self.assertEqual(created.image_asset.data, b"\x89PNG\r\n\x1a\n")

    def test_mask_may_be_a_comma_separated_string(self):
        op = Operation(
            resource="campaign", action="update", update_mask="network_settings.target_content_network, name",
            data={"resource_name": f"customers/{CID}/campaigns/5", "network_settings": {"target_content_network": False}},
        )
        self.assertEqual(op.mask, ["network_settings.target_content_network", "name"])


class AdsFormattingTest(unittest.TestCase):
    def test_eu_declaration_block_explains_what_to_do(self):
        from google.ads.googleads.errors import GoogleAdsException

        failure = CLIENT.get_type("GoogleAdsFailure")
        error = failure.errors.add()
        reasons = error.error_code.DESCRIPTOR.fields_by_name["mutate_error"].enum_type
        error.error_code.mutate_error = reasons.values_by_name["EU_POLITICAL_ADVERTISING_DECLARATION_REQUIRED"].number
        message = ads.format_failure(GoogleAdsException(None, None, failure, "req-2"))
        self.assertIn("What to do:", message)
        self.assertIn("missing_eu_political_advertising_declaration = TRUE", message)

    def test_failures_are_readable_in_both_proto_flavours(self):
        from google.ads.googleads.errors import GoogleAdsException

        plus_client = GoogleAdsClient(credentials=AnonymousCredentials(), use_proto_plus=True)
        for client in (CLIENT, plus_client):
            failure = client.get_type("GoogleAdsFailure")
            pb = ads._pb(failure)
            error = pb.errors.add()
            reasons = error.error_code.DESCRIPTOR.fields_by_name["mutate_error"].enum_type
            error.error_code.mutate_error = reasons.values_by_name["RESOURCE_NOT_FOUND"].number
            error.message = "Resource was not found."
            element = error.location.field_path_elements.add()
            element.field_name, element.index = "mutate_operations", 0
            message = ads.format_failure(GoogleAdsException(None, None, failure, "req-1"))
            self.assertIn("request id req-1", message)
            self.assertIn("mutate_error.RESOURCE_NOT_FOUND at mutate_operations[0]: Resource was not found.", message)

    def test_result_names(self):
        response = CLIENT.get_type("MutateGoogleAdsResponse")
        response.mutate_operation_responses.add().campaign_result.resource_name = f"customers/{CID}/campaigns/5"
        response.mutate_operation_responses.add().ad_group_criterion_result.resource_name = f"customers/{CID}/adGroupCriteria/7~9"
        self.assertEqual(ads.result_names(response), [f"customers/{CID}/campaigns/5", f"customers/{CID}/adGroupCriteria/7~9"])


class GuardrailsTest(unittest.TestCase):
    def check(self, *operations: Operation, **overrides):
        return guardrails.review(CID, list(operations), settings(**overrides))

    def test_budget_cap(self):
        budget = f"customers/{CID}/campaignBudgets/1"
        self.assertTrue(self.check(update("campaign_budget", budget, amount_micros=100_000_000)).ok)
        blocked = self.check(update("campaign_budget", budget, amount_micros=100_010_000))
        self.assertIn("exceeds the cap of 100.00", blocked.violations[0])

    def test_budget_in_units_instead_of_micros_warns(self):
        review = self.check(update("campaign_budget", f"customers/{CID}/campaignBudgets/1", amount_micros=75))
        self.assertTrue(review.ok)
        self.assertIn("Check the units", review.warnings[0])

    def test_bid_cap_applies_to_nested_bids(self):
        review = self.check(update("campaign", f"customers/{CID}/campaigns/1", target_spend={"cpc_bid_ceiling_micros": "25000000"}))
        self.assertIn("GADS_MAX_CPC_BID", review.violations[0])

    def test_irreversible_removes_are_blocked(self):
        blocked = self.check(Operation(resource="campaign", action="remove", resource_name=f"customers/{CID}/campaigns/1"))
        self.assertIn("irreversible", blocked.violations[0])
        allowed = self.check(Operation(resource="ad_group_criterion", action="remove", resource_name=f"customers/{CID}/adGroupCriteria/1~2"))
        self.assertTrue(allowed.ok)

    def test_other_accounts_are_rejected(self):
        review = self.check(update("campaign", "customers/9999999999/campaigns/1", status="PAUSED"))
        self.assertIn("belongs to account 9999999999", review.violations[0])
        nested = self.check(Operation(resource="ad_group", action="create", data={"campaign": "customers/9999999999/campaigns/1", "name": "x"}))
        self.assertFalse(nested.ok)

    def test_temporary_names_are_accepted(self):
        review = self.check(
            Operation(resource="campaign_budget", action="create", data={"resource_name": f"customers/{CID}/campaignBudgets/-1", "amount_micros": 20_000_000}),
            Operation(resource="campaign", action="create", data={"name": "c", "campaign_budget": f"customers/{CID}/campaignBudgets/-1"}),
        )
        self.assertTrue(review.ok, review.violations)

    def test_new_campaigns_and_ads_are_forced_paused(self):
        campaign = Operation(resource="campaign", action="create", data={"name": "c", "status": "ENABLED"})
        ad = Operation(resource="ad_group_ad", action="create", data={"ad_group": f"customers/{CID}/adGroups/1"})
        review = self.check(campaign, ad)
        self.assertEqual((campaign.data["status"], ad.data["status"]), ("PAUSED", "PAUSED"))
        self.assertEqual(len(review.warnings), 2)

    def test_operation_count_limit(self):
        operations = [update("campaign", f"customers/{CID}/campaigns/{i}", status="PAUSED") for i in range(1, 5)]
        self.assertIn("exceeds the limit of 3", self.check(*operations, max_ops_per_plan=3).violations[0])

    def test_blocked_resources(self):
        review = self.check(update("customer", f"customers/{CID}", auto_tagging_enabled=False), blocked_resources=frozenset({"customer"}))
        self.assertIn("is blocked", review.violations[0])

    def test_bid_increase_limit(self):
        name = f"customers/{CID}/adGroupCriteria/1~2"
        raise_bid = update("ad_group_criterion", name, cpc_bid_micros=2_000_000)
        blocked = guardrails.review_changes([raise_bid], [{"cpc_bid_micros": "1000000"}], settings())
        self.assertIn("+100%", blocked.violations[0])
        within = guardrails.review_changes([raise_bid], [{"cpc_bid_micros": "1500000"}], settings())
        lowered = guardrails.review_changes([update("ad_group_criterion", name, cpc_bid_micros=100_000)], [{"cpc_bid_micros": "1000000"}], settings())
        self.assertTrue(within.ok and lowered.ok)


class PlanStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = PlanStore(Path(self.dir.name), ttl_minutes=30)

    def tearDown(self):
        self.dir.cleanup()

    def test_lifecycle(self):
        plan = self.store.create(customer_id=CID, operations=[], before=[], warnings=[], note="")
        self.assertEqual(self.store.load_pending(plan["plan_id"])["status"], "pending")
        plan["status"] = "applied"
        self.store.save(plan)
        with self.assertRaisesRegex(PlanError, "already applied"):
            self.store.load_pending(plan["plan_id"])

    def test_expiry(self):
        self.store.ttl = timedelta(minutes=-1)
        plan = self.store.create(customer_id=CID, operations=[], before=[], warnings=[], note="")
        with self.assertRaisesRegex(PlanError, "expired"):
            self.store.load_pending(plan["plan_id"])
        self.assertEqual(self.store.load(plan["plan_id"])["status"], "expired")

    def test_ids_cannot_escape_the_plans_directory(self):
        with self.assertRaisesRegex(PlanError, "not a plan id"):
            self.store.load("../../etc/passwd")

    def test_audit_appends(self):
        self.store.audit("plan_validated", plan_id="plan_00000000")
        self.store.audit("applied", plan_id="plan_00000000")
        self.assertEqual(len(self.store.audit_path.read_text().splitlines()), 2)


class ApplyGateTest(unittest.TestCase):
    """The checks between a stored plan and the live account."""

    def setUp(self):
        from gads_write_mcp import server

        self.server = server
        self.dir = tempfile.TemporaryDirectory()
        self.env = {"GADS_STATE_DIR": self.dir.name}
        self.plan = {
            "plan_id": "plan_00000000",
            "customer_id": CID,
            "operations": [{"resource": "campaign", "action": "update", "data": {"resource_name": f"customers/{CID}/campaigns/1", "status": "PAUSED"}}],
        }

    def tearDown(self):
        self.dir.cleanup()

    def configure(self, **env: str):
        for key in ("GADS_ALLOWED_CUSTOMER_IDS", "GADS_READ_ONLY", "GADS_MAX_DAILY_BUDGET"):
            os.environ.pop(key, None)
        os.environ.update({**self.env, **env})
        self.server._settings.cache_clear()
        self.server._store.cache_clear()

    def test_unlisted_account_is_refused(self):
        self.configure()
        with self.assertRaisesRegex(PlanError, "not approved for writes"):
            self.server._check_can_apply(self.plan)

    def test_read_only_wins_over_allowlist(self):
        self.configure(GADS_ALLOWED_CUSTOMER_IDS=CID, GADS_READ_ONLY="true")
        with self.assertRaisesRegex(PlanError, "read-only"):
            self.server._check_can_apply(self.plan)

    def test_allowlisted_account_passes(self):
        self.configure(GADS_ALLOWED_CUSTOMER_IDS="123-456-7890")
        self.assertEqual(len(self.server._check_can_apply(self.plan)), 1)

    def test_guardrails_run_again_at_apply_time(self):
        self.configure(GADS_ALLOWED_CUSTOMER_IDS=CID, GADS_MAX_DAILY_BUDGET="10")
        self.plan["operations"] = [{"resource": "campaign_budget", "action": "update", "data": {"resource_name": f"customers/{CID}/campaignBudgets/1", "amount_micros": 50_000_000}}]
        with self.assertRaisesRegex(OperationError, "exceeds the cap"):
            self.server._check_can_apply(self.plan)

    def test_rsa_copy_rules(self):
        from mcp.server.mcpserver.exceptions import ToolError

        self.configure()
        with self.assertRaisesRegex(ToolError, "exclamation point") as caught:
            asyncio.run(
                self.server.gads_plan_create_rsa(
                    customer_id=CID,
                    ad_group_id="1",
                    headlines=["Book Now!", "A headline that is far longer than thirty chars", "Third"],
                    descriptions=["One.", "Two."],
                    final_url="https://example.com",
                )
            )
        self.assertIn("max 30", str(caught.exception))


class ConfirmationTest(unittest.IsolatedAsyncioTestCase):
    """gads_apply_plan through a real in-memory MCP session. The executor is stubbed: no network."""

    def setUp(self):
        from gads_write_mcp import server

        self.server = server
        self.dir = tempfile.TemporaryDirectory()
        for key in ("GADS_READ_ONLY", "GADS_CONFIRM"):
            os.environ.pop(key, None)
        os.environ.update(GADS_STATE_DIR=self.dir.name, GADS_ALLOWED_CUSTOMER_IDS=CID)
        server._settings.cache_clear()
        server._store.cache_clear()
        self.plan = self.new_plan()

    def new_plan(self):
        return self.server._store().create(
            customer_id=CID, note="", warnings=[], before=[{"name": "Brand", "status": "ENABLED"}],
            operations=[{"resource": "campaign", "action": "update", "data": {"resource_name": f"customers/{CID}/campaigns/1", "status": "PAUSED"}}],
        )

    def tearDown(self):
        self.dir.cleanup()

    # 'legacy' asks mid-call over a back-channel; 'auto' negotiates the newer
    # input-required round trip. Claude clients may speak either.
    MODES = ("legacy", "auto")

    async def apply(self, mode: str, action: str | None, approve: bool = True, client_info=None):
        from unittest import mock

        from mcp import types
        from mcp.client import Client

        shown: list[str] = []

        async def on_elicit(context, params):
            shown.append(params.message)
            return types.ElicitResult(action=action, content={"approve": approve} if action == "accept" else None)

        executed = mock.Mock(return_value={"status": "applied"})
        with mock.patch.object(self.server, "_execute", executed):
            async with Client(self.server.mcp, mode=mode, client_info=client_info, elicitation_callback=on_elicit if action else None) as client:
                result = await client.call_tool("gads_apply_plan", {"plan_id": self.plan["plan_id"]})
        message = "\n".join(block.text for block in result.content if hasattr(block, "text"))
        return result, message, executed, shown

    async def test_the_model_cannot_supply_the_approval(self):
        tools = {tool.name: tool for tool in await self.server.mcp.list_tools()}
        self.assertEqual(list(tools["gads_apply_plan"].input_schema["properties"]), ["plan_id"])

    async def test_apply_is_flagged_as_requiring_user_interaction(self):
        from mcp.client import Client

        async with Client(self.server.mcp) as client:
            tools = {tool.name: tool.meta for tool in (await client.list_tools()).tools}
        self.assertEqual(tools["gads_apply_plan"], {"anthropic/requiresUserInteraction": True})
        self.assertEqual([name for name, meta in tools.items() if meta], ["gads_apply_plan"])

    async def test_auto_mode_picks_the_gate_from_the_client(self):
        from mcp.types import Implementation

        cases = (
            ("claude-code", "2.1.275", "client_permission_prompt"),
            ("Claude Code", "2.1.199", "client_permission_prompt"),
            ("claude-code", "2.1.198", "elicitation"),
            ("claude-code", "", "elicitation"),
            ("some-other-client", "9.9.9", "elicitation"),
        )
        for name, version, expected in cases:
            with self.subTest(client=name, version=version):
                info = Implementation(name=name, version=version)
                result, message, executed, shown = await self.apply("legacy", "accept", client_info=info)
                self.assertFalse(result.is_error, message)
                self.assertEqual(executed.call_args.args[2], expected)
                self.assertEqual(len(shown), 0 if expected == "client_permission_prompt" else 1)
                self.plan = self.new_plan()

    async def test_accept_executes_and_the_dialog_shows_the_diff(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                result, message, executed, shown = await self.apply(mode, "accept")
                self.assertFalse(result.is_error, message)
                self.assertEqual(executed.call_args.args[2], "elicitation")
                self.assertIn("Brand status: ENABLED -> PAUSED", shown[0])

    async def test_decline_cancel_and_unticked_box_execute_nothing(self):
        for mode in self.MODES:
            for action, approve in (("decline", True), ("cancel", True), ("accept", False)):
                with self.subTest(mode=mode, action=action, approve=approve):
                    result, message, executed, _ = await self.apply(mode, action, approve)
                    self.assertTrue(result.is_error)
                    self.assertIn("declined", message)
                    executed.assert_not_called()

    async def test_client_without_a_dialog_is_refused(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                result, message, executed, _ = await self.apply(mode, None)
                self.assertTrue(result.is_error)
                self.assertIn("confirmation dialog", message)
                executed.assert_not_called()

    async def test_client_mode_skips_the_dialog(self):
        os.environ["GADS_CONFIRM"] = "client"
        self.server._settings.cache_clear()
        try:
            result, message, executed, shown = await self.apply("legacy", None)
        finally:
            os.environ.pop("GADS_CONFIRM")
            self.server._settings.cache_clear()
        self.assertFalse(result.is_error, message)
        self.assertEqual((executed.call_args.args[2], shown), ("client_permission_prompt", []))


# --- offline conversions ------------------------------------------------------

GCLID = "Cj0KCQjw-fake-gclid-for-offline-tests-0001"
RAW_EMAIL, RAW_PHONE = "Jane.Doe+crm@Example.com", "+1 (647) 555-0142"
# Every spelling of the personal data that must never be written or returned: as given, and normalized.
RAW_VALUES = ("jane.doe", "example.com", "555-0142", "5550142", "6475550142")
ACTION = {"id": "777", "resource_name": f"customers/{CID}/conversionActions/777", "name": "Booked job", "status": "ENABLED", "type": "UPLOAD_CLICKS", "click_through_lookback_window_days": "90", "default_value": 1.0, "default_currency": "CAD", "always_use_default_value": False}


def ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")


def parse(*rows, **options):
    return conversions.parse_rows(list(rows), timezone=options.pop("timezone", None), currency=options.pop("currency", None), **options)


class ConversionHashingTest(unittest.TestCase):
    """Vectors from the tests of Google's own formatter (googleads/data-manager-python, util/format)."""

    def test_google_email_vectors(self):
        vectors = (
            ("QuinnY@example.com", "quinny@example.com"),
            ("QuinnY@EXAMPLE.com", "quinny@example.com"),
            ("Jefferson.Loves.hiking@gmail.com", "jeffersonloveshiking@gmail.com"),
            ("Jefferson.LOVES.Hiking@googlemail.com", "jeffersonloveshiking@googlemail.com"),
            ("Cloudy.SanFrancisco+shopping@googlemail.com", "cloudysanfrancisco@googlemail.com"),
            ("Cloudy.SanFrancisco+shopping@gmail.com", "cloudysanfrancisco@gmail.com"),
            ("user.name+NYC@Example.com", "user.name+nyc@example.com"),
            ("  alexz@example.com \n", "alexz@example.com"),
        )
        for raw, expected in vectors:
            self.assertEqual(conversions.normalize_email(raw), expected, raw)
        for invalid in ("", "  ", "no-at-sign.example.com", "two@@example.com", "a@b@example.com", "@example.com", "user@", "user@nodot", "+tag.only@gmail.com"):
            self.assertIsNone(conversions.normalize_email(invalid), invalid)

    def test_google_phone_vectors(self):
        vectors = (
            ("1 800 555 0100", "+18005550100"),
            ("18005550100", "+18005550100"),
            ("+1 800-555-0100", "+18005550100"),
            ("441134960987", "+441134960987"),
            ("+441134960987", "+441134960987"),
            ("+44-113-496-0987", "+441134960987"),
            ("+45 12 34 56 78", "+4512345678"),
        )
        for raw, expected in vectors:
            self.assertEqual(conversions.normalize_phone(raw), expected, raw)

    def test_phones_without_a_visible_country_code_are_refused(self):
        for invalid in ("", "  ", " +A BCD EFG ", "416 555 0199", "(416) 555-0199", "0151 1234 5678", "+0 151 1234 5678", "+1234567890123456"):
            self.assertIsNone(conversions.normalize_phone(invalid), invalid)

    def test_google_hash_vectors(self):
        rows, problems = parse({"email": "AlexZ@Example.com", "phone": "1 800 555 0100", "conversion_time": ago(days=1)})
        self.assertEqual(problems, [])
        self.assertEqual(rows[0]["hashed_email"], "509e933019bb285a134a9334b8bb679dff79d0ce023d529af4bd744d47b4fd8a")
        self.assertEqual(rows[0]["hashed_phone"], "fb4f73a6ec5fdb7077d564cdd22c3554b43ce49168550c3b12c547b78c517b30")


class ConversionRowsTest(unittest.TestCase):
    def test_rows_hold_hashes_and_never_the_raw_value(self):
        rows, problems = parse({"gclid": GCLID, "email": RAW_EMAIL, "phone": RAW_PHONE, "conversion_time": ago(days=1), "value": "250.00", "currency": "cad", "order_id": "A-1"})
        self.assertEqual(problems, [])
        row = rows[0]
        self.assertEqual(row["hashed_email"], hashlib.sha256(b"jane.doe+crm@example.com").hexdigest())
        self.assertEqual(row["hashed_phone"], hashlib.sha256(b"+16475550142").hexdigest())
        self.assertEqual((row["value"], row["currency"], row["order_id"]), (250.0, "CAD", "A-1"))
        self.assertFalse({"email", "phone"} & set(row))
        self.assertFalse(any(raw in json.dumps(rows).lower() for raw in RAW_VALUES))

    def test_hashed_input_is_kept_as_is(self):
        digest = hashlib.sha256(b"lead@example.com").hexdigest()
        for key in ("email", "hashed_email"):
            rows, problems = parse({key: digest.upper(), "conversion_time": ago(days=1)})
            self.assertEqual((rows[0]["hashed_email"], problems), (digest, []))

    def test_problems_never_quote_personal_data(self):
        time = ago(days=1)
        bad_rows = (
            {"contact": RAW_EMAIL, "conversion_time": time},           # unknown key
            {RAW_EMAIL: "x", "gclid": GCLID, "conversion_time": time},  # a value used as a key
            {"email": "jane.doe-at-example.com", "conversion_time": time},
            {"phone": "555-0142", "conversion_time": time},            # no country code
            {"email": 6475550142, "conversion_time": time},
            {"gclid": GCLID, "order_id": RAW_EMAIL, "conversion_time": time},   # shifted columns
            {"gclid": GCLID, "value": RAW_EMAIL, "conversion_time": time},
            {"gclid": "6475550142", "conversion_time": time},          # a phone where the click id goes
            {"gclid": GCLID, "order_id": "+16475550142", "conversion_time": time},
            RAW_EMAIL,
        )
        rows, problems = parse(*bad_rows)
        self.assertEqual(rows, [])
        self.assertEqual(len({p.split(":")[0] for p in problems}), len(bad_rows))
        text = "\n".join(problems).lower()
        for raw in RAW_VALUES:
            self.assertNotIn(raw, text)

    def test_one_click_id_and_at_least_one_identifier(self):
        _, problems = parse({"gclid": GCLID, "wbraid": GCLID, "conversion_time": ago(days=1)}, {"conversion_time": ago(days=1)})
        self.assertIn("row[0]: has gclid and wbraid", problems[0])
        self.assertIn("row[1]: needs a click id", problems[1])

    def test_time_needs_an_offset_or_a_timezone(self):
        naive = {"gclid": GCLID, "conversion_time": "2026-09-18 14:30:00"}
        self.assertIn("has no UTC offset", parse(naive)[1][0])
        self.assertEqual(parse(naive, timezone="America/Toronto")[0][0]["conversion_time"], "2026-09-18T14:30:00-04:00")
        self.assertEqual(parse({**naive, "conversion_time": "2026-01-18T14:30:00Z"})[0][0]["conversion_time"], "2026-01-18T14:30:00+00:00")
        self.assertIn("not readable", parse({**naive, "conversion_time": "9/18/2026 2:30 PM"})[1][0])
        with self.assertRaisesRegex(OperationError, "IANA"):
            parse(naive, timezone="Toronto time")

    def test_values_are_plain_numbers(self):
        for bad in ("$250", "1,250.00", "1 250,00", "-5", "nan", True):
            self.assertIn("plain non-negative number", parse({"gclid": GCLID, "conversion_time": ago(days=1), "value": bad})[1][0], bad)
        self.assertEqual(parse({"gclid": GCLID, "conversion_time": ago(days=1), "value": 0})[0][0]["value"], 0.0)

    def test_ads_api_time_format(self):
        self.assertEqual(conversions.ads_time("2026-09-18T14:30:00-04:00"), "2026-09-18 14:30:00-04:00")
        self.assertEqual(conversions.ads_time("2026-09-18T14:30:00+00:00"), "2026-09-18 14:30:00+00:00")


class ConversionCsvTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text: str, encoding: str = "utf-8") -> str:
        path = Path(self.dir.name) / "export.csv"
        path.write_bytes(text.encode(encoding))
        return str(path)

    def test_google_template_headers_and_ignored_columns(self):
        path = self.write(f"Google Click ID,Conversion Name,Conversion Time,Conversion Value,Conversion Currency,Email,Deal Owner\n{GCLID},Booked job,2026-09-18 14:30:00-04:00,250.00,CAD,{RAW_EMAIL},Sam\n\n")
        raw, lines, notes = conversions.read_csv(path)
        rows, problems = conversions.parse_rows(raw, timezone=None, currency=None, lines=lines)
        self.assertEqual(problems, [])
        self.assertEqual((rows[0]["gclid"], rows[0]["value"], rows[0]["line"], rows[0]["conversion_name"]), (GCLID, 250.0, 2, "Booked job"))
        self.assertIn("'Google Click ID' -> gclid", notes[0])
        self.assertIn("'Deal Owner'", notes[1])
        self.assertNotIn("Sam", json.dumps(rows))

    def test_excel_semicolons_bom_and_french_headers(self):
        path = self.write(f"\ufeffgclid;Courriel;T\u00e9l\u00e9phone;conversion_time\n{GCLID};{RAW_EMAIL};{RAW_PHONE};2026-09-18T14:30:00-04:00\n")
        raw, lines, _ = conversions.read_csv(path)
        rows, problems = conversions.parse_rows(raw, timezone=None, currency=None, lines=lines)
        self.assertEqual((problems, set(rows[0])), ([], {"gclid", "hashed_email", "hashed_phone", "conversion_time", "line"}))

    def test_problems_name_the_csv_line(self):
        path = self.write(f"gclid,conversion_time\n{GCLID},2026-09-18T14:30:00-04:00\n{GCLID},yesterday\n")
        raw, lines, _ = conversions.read_csv(path)
        self.assertIn("row[1] (CSV line 3): conversion_time 'yesterday' is not readable", conversions.parse_rows(raw, timezone=None, currency=None, lines=lines)[1][0])

    def test_a_file_without_headers_does_not_leak_its_first_row(self):
        path = self.write(f"{RAW_EMAIL},{RAW_PHONE},2026-09-18T14:30:00-04:00\nother@example.com,+14165550100,2026-09-18T15:30:00-04:00\n")
        with self.assertRaisesRegex(OperationError, "needs a header row") as caught:
            conversions.read_csv(path)
        for raw in RAW_VALUES:
            self.assertNotIn(raw, str(caught.exception).lower())

    def test_unreadable_files_are_refused(self):
        with self.assertRaisesRegex(OperationError, "No CSV file"):
            conversions.read_csv(str(Path(self.dir.name) / "missing.csv"))
        with self.assertRaisesRegex(OperationError, "not UTF-8"):
            conversions.read_csv(self.write("gclid,conversion_time\n\u00e9,x\n", encoding="utf-16"))


class ConversionGuardrailsTest(unittest.TestCase):
    def row(self, **fields):
        return {"gclid": GCLID, "conversion_time": ago(days=2), "order_id": "A-1", **fields}

    def check(self, *rows, action=ACTION, **overrides):
        return guardrails.review_conversions(list(rows), settings(**overrides), datetime.now(timezone.utc), action)

    def test_conversion_action_must_exist_be_enabled_and_take_imports(self):
        self.assertIn("No conversion action 777", guardrails.review_conversion_action(None, "777", CID).violations[0])
        self.assertTrue(guardrails.review_conversion_action(ACTION, "777", CID).ok)
        paused = guardrails.review_conversion_action({**ACTION, "status": "REMOVED"}, "777", CID)
        self.assertIn("is REMOVED, not ENABLED", paused.violations[0])
        website = guardrails.review_conversion_action({**ACTION, "type": "WEBPAGE"}, "777", CID)
        self.assertIn("has type WEBPAGE", website.violations[0])

    def test_account_conditions(self):
        own = {"google_ads_conversion_customer": f"customers/{CID}", "accepted_customer_data_terms": True, "enhanced_conversions_for_leads_enabled": True}
        self.assertEqual(guardrails.review_tracking(own, CID, True).__dict__, {"violations": [], "warnings": []})
        managed = guardrails.review_tracking({**own, "google_ads_conversion_customer": "customers/9999999999"}, CID, False)
        self.assertIn("tracked by customers/9999999999", managed.violations[0])
        no_terms = guardrails.review_tracking({**own, "accepted_customer_data_terms": False}, CID, True)
        self.assertIn("customer data terms", no_terms.violations[0])
        self.assertTrue(guardrails.review_tracking({**own, "accepted_customer_data_terms": False}, CID, False).ok)
        self.assertIn("Enhanced conversions for leads", guardrails.review_tracking({**own, "enhanced_conversions_for_leads_enabled": False}, CID, True).warnings[0])

    def test_row_cap(self):
        rows = [self.row(gclid=f"{GCLID}{i}", order_id=f"A-{i}") for i in range(4)]
        self.assertIn("4 conversions exceeds the limit of 3", self.check(*rows, max_conversions_per_plan=3).violations[0])
        self.assertIn("no conversions", self.check().violations[0])

    def test_future_and_expired_times(self):
        future = self.check(self.row(conversion_time=ago(minutes=-5)))
        self.assertIn("row[0]: conversion_time", future.violations[0])
        self.assertIn("in the future", future.violations[0])
        self.assertIn("more than 90 days ago", self.check(self.row(conversion_time=ago(days=91))).violations[0])
        self.assertIn("more than 30 days ago", self.check(self.row(conversion_time=ago(days=31)), action={**ACTION, "click_through_lookback_window_days": "30"}).violations[0])
        self.assertTrue(self.check(self.row(conversion_time=ago(days=89))).ok)

    def test_value_cap_catches_unit_mistakes(self):
        self.assertTrue(self.check(self.row(value=10_000.0)).ok)
        blocked = self.check(self.row(value=25_000.0), self.row(gclid=GCLID + "b", order_id="A-2", value=250_000_000.0))
        self.assertEqual(len(blocked.violations), 2)
        self.assertIn("row[1]: value 250,000,000.00 exceeds the cap of 10,000.00", blocked.violations[1])
        self.assertTrue(self.check(self.row(value=25_000.0), max_conversion_value=50_000.0).ok)

    def test_duplicates_are_blocked_by_row(self):
        same_instant = datetime.now(timezone.utc) - timedelta(days=2)
        toronto = same_instant.astimezone(timezone(timedelta(hours=-4))).isoformat(timespec="seconds")
        repeated = self.check(self.row(conversion_time=same_instant.isoformat(timespec="seconds")), self.row(conversion_time=toronto, order_id="A-2"))
        self.assertIn("row[1]: repeats row[0]", repeated.violations[0])
        orders = self.check(self.row(), self.row(gclid=GCLID + "b"))
        self.assertIn("order_id 'A-1' is also on row[0]", orders.violations[0])
        later = self.check(self.row(), self.row(conversion_time=ago(days=1), order_id="A-2"))
        self.assertTrue(later.ok, later.violations)

    def test_rows_for_another_conversion_action_are_blocked(self):
        review = self.check(self.row(conversion_name="booked JOB"), self.row(gclid=GCLID + "b", order_id="A-2", conversion_name="Newsletter signup"))
        self.assertEqual(len(review.violations), 1)
        self.assertIn("row[1]: is for conversion 'Newsletter signup'", review.violations[0])

    def test_warnings(self):
        lead_only = {"hashed_email": "a" * 64, "conversion_time": ago(hours=1), "date_only": True, "line": 7}
        review = self.check(self.row(), lead_only)
        self.assertTrue(review.ok, review.violations)
        text = " ".join(review.warnings)
        for expected in ("1 of 2 rows lack an order_id", "1 of 2 rows could never be retracted", "date without a time: 1", "less than 6 hours ago: 1 (row[1] (CSV line 7))"):
            self.assertIn(expected, text)


class ConversionRequestsTest(unittest.TestCase):
    def plan(self, **fields):
        rows = [
            {"gclid": GCLID, "hashed_email": "a" * 64, "hashed_phone": "b" * 64, "conversion_time": "2026-09-18T14:30:00-04:00", "value": 250.0, "currency": "CAD", "order_id": "A-1"},
            {"wbraid": GCLID, "conversion_time": "2026-09-18T15:30:00-04:00", "line": 3, "date_only": True},
        ]
        return {"customer_id": CID, "conversion_action": ACTION, "rows": rows, "consent": {"ad_user_data": None, "ad_personalization": None}, **fields}

    def test_ingest_payload(self):
        payload = conversions.ingest_payload(self.plan(), None)
        self.assertEqual(payload["destinations"], [{"operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": CID}, "productDestinationId": "777"}])
        self.assertEqual(payload["encoding"], "HEX")
        self.assertNotIn("consent", payload)
        first, second = payload["events"]
        self.assertEqual(first, {
            "eventTimestamp": "2026-09-18T14:30:00-04:00", "eventSource": "WEB", "adIdentifiers": {"gclid": GCLID}, "transactionId": "A-1", "conversionValue": 250.0, "currency": "CAD",
            "userData": {"userIdentifiers": [{"emailAddress": "a" * 64}, {"phoneNumber": "b" * 64}]},
        })
        self.assertEqual(second, {"eventTimestamp": "2026-09-18T15:30:00-04:00", "eventSource": "WEB", "adIdentifiers": {"wbraid": GCLID}})
        self.assertEqual(conversions.ingest_payload(self.plan(event_source="PHONE"), None)["events"][0]["eventSource"], "PHONE")

    def test_manager_login_consent_and_encoding(self):
        plan = self.plan(consent={"ad_user_data": "GRANTED", "ad_personalization": "DENIED"})
        plan["rows"] = plan["rows"][1:]
        payload = conversions.ingest_payload(plan, "9999999999")
        self.assertEqual(payload["destinations"][0]["loginAccount"], {"accountType": "GOOGLE_ADS", "accountId": "9999999999"})
        self.assertEqual(payload["consent"], {"adUserData": "CONSENT_GRANTED", "adPersonalization": "CONSENT_DENIED"})
        self.assertNotIn("encoding", payload)

    def test_read_only_forces_validate_only(self):
        session = mock.Mock()
        session.request.return_value = mock.Mock(ok=True, content=b"{}", json=lambda: {"requestId": "r"})
        with mock.patch.object(datamanager, "_session", return_value=session):
            datamanager.ingest(settings(read_only=True), {"events": []}, validate_only=False)
            datamanager.ingest(settings(), {"events": []}, validate_only=False)
        sent = [call.kwargs["json"]["validateOnly"] for call in session.request.call_args_list]
        self.assertEqual(sent, [True, False])
        self.assertEqual(session.request.call_args.args, ("POST", "https://datamanager.googleapis.com/v1/events:ingest"))

    def test_data_manager_errors_name_rows_and_say_what_to_do(self):
        bad_request = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Request contains an invalid argument.", "details": [
            {"@type": "type.googleapis.com/google.rpc.BadRequest", "fieldViolations": [
                {"field": "events.events[3].event_timestamp", "description": "Timestamp is in the future.", "reason": "EVENT_TIME_INVALID"},
                {"field": "destinations[0].product_destination_id", "description": "Not found."}]}]}}
        message = datamanager.format_failure(400, bad_request)
        self.assertIn("- row[3] event_timestamp: Timestamp is in the future. (EVENT_TIME_INVALID)", message)
        self.assertIn("- destinations[0].product_destination_id: Not found.", message)
        disabled = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "API has not been used.", "details": [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED", "metadata": {"consumer": "projects/123456789012", "activationUrl": "https://console.example/enable"}}]}}
        message = datamanager.format_failure(403, disabled)
        self.assertIn("SERVICE_DISABLED (projects/123456789012, https://console.example/enable)", message)
        self.assertIn("gcloud services enable datamanager.googleapis.com", message)
        self.assertEqual(message.count("What to do:"), 1)
        scope = {"error": {"code": 403, "status": "PERMISSION_DENIED", "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}
        self.assertIn("auth/datamanager", datamanager.format_failure(403, scope))
        self.assertIn("HTTP 502", datamanager.format_failure(502, {}))

    def test_missing_scope_at_token_refresh_is_explained(self):
        from google.auth.exceptions import RefreshError

        session = mock.Mock()
        session.request.side_effect = RefreshError("invalid_scope: Bad Request")
        with mock.patch.object(datamanager, "_session", return_value=session):
            with self.assertRaisesRegex(ads.AdsError, "gcloud auth application-default login.*auth/datamanager"):
                datamanager.request_status("req-1")

    def test_retractions_build_conversion_adjustment_protos(self):
        rows = self.plan()["rows"]
        by_order = conversions.retraction(rows[0], ACTION["resource_name"], "2026-09-20T09:00:00+00:00")
        by_click = conversions.retraction({k: v for k, v in rows[0].items() if k != "order_id"}, ACTION["resource_name"], "2026-09-20T09:00:00+00:00")
        self.assertIsNone(conversions.retraction(rows[1], ACTION["resource_name"], "2026-09-20T09:00:00+00:00"))
        self.assertIsNone(conversions.retraction({**rows[1], "order_id": "A-2"}, ACTION["resource_name"], "2026-09-20T09:00:00+00:00"), "wbraid conversions cannot be adjusted")
        self.assertIsNone(conversions.retraction({"gbraid": GCLID, "conversion_time": rows[1]["conversion_time"]}, ACTION["resource_name"], "2026-09-20T09:00:00+00:00"))
        self.assertNotIn("gclid_date_time_pair", by_order)

        from google.protobuf import json_format

        request = CLIENT.get_type("UploadConversionAdjustmentsRequest")
        for data in (by_order, by_click):
            json_format.ParseDict(data, request.conversion_adjustments.add())
        first, second = request.conversion_adjustments
        self.assertEqual(first.adjustment_type, CLIENT.enums.ConversionAdjustmentTypeEnum.RETRACTION)
        self.assertEqual((first.order_id, first.conversion_action, first.adjustment_date_time), ("A-1", ACTION["resource_name"], "2026-09-20 09:00:00+00:00"))
        self.assertEqual((second.gclid_date_time_pair.gclid, second.gclid_date_time_pair.conversion_date_time), (GCLID, "2026-09-18 14:30:00-04:00"))
        self.assertFalse(first.HasField("restatement_value"))

    def test_partial_failures_are_spread_over_their_rows(self):
        failure = CLIENT.get_type("GoogleAdsFailure")
        reasons = failure.errors.add().error_code.DESCRIPTOR.fields_by_name["conversion_adjustment_upload_error"].enum_type
        del failure.errors[:]
        for index, reason in ((2, "CONVERSION_NOT_FOUND"), (0, "CONVERSION_ALREADY_RETRACTED")):
            error = failure.errors.add()
            error.error_code.conversion_adjustment_upload_error = reasons.values_by_name[reason].number
            error.message = f"{reason} message"
            element = error.location.field_path_elements.add()
            element.field_name, element.index = "conversion_adjustments", index
        response = CLIENT.get_type("UploadConversionAdjustmentsResponse")
        response.partial_failure_error.details.add().Pack(failure)
        outcome = ads.partial_failures(CLIENT, response.partial_failure_error, 3)
        self.assertEqual(outcome[1], None)
        self.assertTrue(outcome[0].startswith("conversion_adjustment_upload_error.CONVERSION_ALREADY_RETRACTED: CONVERSION_ALREADY_RETRACTED message"))
        self.assertIn("What to do: This conversion was retracted before", outcome[0])
        self.assertIn("CONVERSION_NOT_FOUND", outcome[2])
        self.assertIn("gads_get_upload_status", outcome[2])
        self.assertEqual(ads.partial_failures(CLIENT, CLIENT.get_type("UploadConversionAdjustmentsResponse").partial_failure_error, 2), [None, None])

    def test_adjustment_upload_always_uses_partial_failure_and_honours_read_only(self):
        service = mock.Mock()
        service.upload_conversion_adjustments.return_value = CLIENT.get_type("UploadConversionAdjustmentsResponse")
        client = mock.Mock(get_type=CLIENT.get_type, get_service=mock.Mock(return_value=service))
        adjustment = conversions.retraction(self.plan()["rows"][0], ACTION["resource_name"], "2026-09-20T09:00:00+00:00")
        self.assertEqual(ads.upload_adjustments(client, settings(read_only=True), CID, [adjustment], validate_only=False), [None])
        request = service.upload_conversion_adjustments.call_args.kwargs["request"]
        self.assertEqual((request.customer_id, request.partial_failure, request.validate_only, len(request.conversion_adjustments)), (CID, True, True, 1))

    def test_status_view(self):
        self.assertEqual(conversions.status_view({})["state"], "NOT_AVAILABLE_YET")
        view = conversions.status_view({"requestStatusPerDestination": [{
            "requestStatus": "PARTIAL_SUCCESS", "eventsIngestionStatus": {"recordCount": "10"},
            "errorInfo": {"errorCounts": [{"reason": "PROCESSING_ERROR_REASON_CLICK_NOT_FOUND", "recordCount": "3"}, {"reason": "PROCESSING_ERROR_REASON_SOMETHING_NEW", "recordCount": "1"}]},
        }]})
        self.assertEqual((view["state"], view["rows_received"], view["rows_rejected"]), ("PARTIAL_SUCCESS", 10, 4))
        self.assertEqual([entry["reason"] for entry in view["rejected_by_reason"]], ["CLICK_NOT_FOUND", "SOMETHING_NEW"])
        self.assertIn("No such click", view["rejected_by_reason"][0]["meaning"])


class ConversionPlanTest(unittest.IsolatedAsyncioTestCase):
    """Upload and retraction plans through a real in-memory MCP session. Google is stubbed: no network."""

    def setUp(self):
        from gads_write_mcp import server

        self.server = server
        self.dir = tempfile.TemporaryDirectory()
        for key in ("GADS_READ_ONLY", "GADS_CONFIRM", "GADS_MAX_CONVERSION_VALUE"):
            os.environ.pop(key, None)
        os.environ.update(GADS_STATE_DIR=self.dir.name, GADS_ALLOWED_CUSTOMER_IDS=CID)
        server._settings.cache_clear()
        server._store.cache_clear()
        self.sent: list[tuple[bool, dict]] = []
        self.adjusted: list[tuple[bool, list]] = []
        self.adjustment_errors: dict[int, str] = {}
        self.action = {"id": "777", "name": "Booked job", "status": "ENABLED", "type_": "UPLOAD_CLICKS", "click_through_lookback_window_days": "90", "value_settings": {"default_value": 1.0, "default_currency_code": "CAD"}}
        tracking = {"accepted_customer_data_terms": True, "enhanced_conversions_for_leads_enabled": True, "google_ads_conversion_customer": f"customers/{CID}"}

        def search(client, customer_id, query):
            if "FROM conversion_action" in query:
                return [{"conversion_action": self.action}] if self.action else []
            return [{"customer": {"conversion_tracking_setting": tracking}}]

        def ingest(settings, payload, *, validate_only):
            self.sent.append((validate_only, payload))
            return {"requestId": "req-123"}

        def upload_adjustments(client, settings, customer_id, adjustments, *, validate_only):
            self.adjusted.append((validate_only, adjustments))
            return [self.adjustment_errors.get(index) for index in range(len(adjustments))]

        status = {"requestStatusPerDestination": [{"requestStatus": "PARTIAL_SUCCESS", "eventsIngestionStatus": {"recordCount": "2"}, "errorInfo": {"errorCounts": [{"reason": "PROCESSING_ERROR_REASON_CLICK_NOT_FOUND", "recordCount": "1"}]}}]}
        self.patches = [
            mock.patch.object(ads, "get_client", lambda login: object()),
            mock.patch.object(ads, "search", search),
            mock.patch.object(ads, "upload_adjustments", upload_adjustments),
            mock.patch.object(datamanager, "ingest", ingest),
            mock.patch.object(datamanager, "request_status", lambda request_id: status),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.dir.cleanup()

    def rows(self):
        return [
            {"gclid": GCLID, "email": RAW_EMAIL, "phone": RAW_PHONE, "conversion_time": ago(days=2), "value": 250, "currency": "CAD", "order_id": "A-1"},
            {"gclid": GCLID + "b", "conversion_time": ago(days=3), "value": 100, "currency": "CAD"},
            {"email": "Second.Lead@Example.com", "conversion_time": ago(days=2, hours=1)},
        ]

    async def call(self, tool: str, arguments: dict, action: str | None = "accept", approve: bool = True):
        from mcp import types
        from mcp.client import Client

        async def on_elicit(context, params):
            self.dialog = params.message
            return types.ElicitResult(action=action, content={"approve": approve} if action == "accept" else None)

        async with Client(self.server.mcp, mode="legacy", elicitation_callback=on_elicit if action else None) as client:
            result = await client.call_tool(tool, arguments)
        return result, "\n".join(block.text for block in result.content if hasattr(block, "text"))

    async def plan(self, **arguments):
        return await self.call("gads_plan_upload_click_conversions", {"customer_id": CID, "conversion_action_id": "777", **arguments})

    def state_text(self) -> str:
        return "\n".join(path.read_text() for path in Path(self.dir.name).rglob("*") if path.is_file()).lower()

    def assert_no_raw_values(self, *texts: str):
        for text in (*texts, self.state_text()):
            for raw in (*RAW_VALUES, "second.lead"):
                self.assertNotIn(raw, text.lower())

    async def test_nothing_raw_reaches_the_state_directory_or_the_result(self):
        result, message = await self.plan(rows=self.rows())
        self.assertFalse(result.is_error, message)
        plan = result.structured_content
        self.assertEqual((plan["kind"], plan["summary"]["rows"], plan["summary"]["total_value"]), ("conversion_upload", 3, {"CAD": 350.0}))
        self.assertEqual(plan["summary"]["identifiers"], {"gclid": 2, "hashed_email": 2, "hashed_phone": 1})
        self.assertTrue(plan["rows"][0]["hashed_email"].startswith("sha256:"))
        self.assertIn("format of all 3 rows", plan["validation"])
        self.assertEqual([validate_only for validate_only, _ in self.sent], [True])
        stored = json.loads((Path(self.dir.name) / "plans" / f"{plan['plan_id']}.json").read_text())
        self.assertEqual(stored["rows"][0]["hashed_email"], hashlib.sha256(b"jane.doe+crm@example.com").hexdigest())
        self.assertIn("plan_validated", self.state_text())
        self.assert_no_raw_values(message, json.dumps(self.sent))

    async def test_a_csv_keeps_personal_data_out_of_the_conversation_and_the_state(self):
        path = Path(self.dir.name) / "crm export.csv"
        path.write_text(f"Google Click ID,Email,Phone Number,Conversion Time,Conversion Value,Conversion Currency,Order ID,Full Name\n{GCLID},{RAW_EMAIL},{RAW_PHONE},{ago(days=2)},250.00,CAD,A-1,Jane Doe\n")
        result, message = await self.plan(csv_path=str(path))
        self.assertFalse(result.is_error, message)
        self.assertIn("'Full Name'", " ".join(result.structured_content["warnings"]))
        path.unlink()
        self.assert_no_raw_values(message, json.dumps(self.sent))
        self.assertNotIn("jane doe", self.state_text())

    async def test_blocked_and_malformed_input_is_never_quoted(self):
        attempts = (
            {"rows": [{"contact": RAW_EMAIL, "conversion_time": ago(days=1)}]},
            {"rows": [RAW_EMAIL]},
            {"rows": RAW_EMAIL},
            {"rows": [{"email": 6475550142, "conversion_time": ago(days=1)}]},
            {"rows": [{"gclid": GCLID, "order_id": RAW_EMAIL, "conversion_time": ago(days=1)}]},
            {"rows": [{"email": RAW_EMAIL, "phone": RAW_PHONE, "conversion_time": ago(days=-1)}]},
            {"rows": self.rows(), "csv_path": "/tmp/x.csv"},
        )
        for arguments in attempts:
            with self.subTest(arguments=str(arguments)[:60]):
                result, message = await self.plan(**arguments)
                self.assertTrue(result.is_error)
                self.assert_no_raw_values(message)
        self.assertEqual(self.sent, [], "nothing reaches Google when local checks fail")
        self.assertIn("plan_blocked", self.state_text())

    async def test_conversion_action_is_checked_before_google_validates(self):
        self.action = {**self.action, "type_": "WEBPAGE"}
        result, message = await self.plan(rows=self.rows())
        self.assertTrue(result.is_error)
        self.assertIn("has type WEBPAGE", message)
        self.action = None
        result, message = await self.plan(rows=self.rows())
        self.assertIn("No conversion action 777", message)
        self.assertEqual(self.sent, [])

    async def test_upload_runs_only_through_the_approval_gate(self):
        plan = (await self.plan(rows=self.rows(), ad_user_data_consent="GRANTED"))[0].structured_content
        for action, approve in (("decline", True), ("accept", False), (None, True)):
            result, message = await self.call("gads_apply_plan", {"plan_id": plan["plan_id"]}, action, approve)
            self.assertTrue(result.is_error, message)
        self.assertEqual([validate_only for validate_only, _ in self.sent], [True], "declined: nothing was uploaded")

        result, message = await self.call("gads_apply_plan", {"plan_id": plan["plan_id"]})
        self.assertFalse(result.is_error, message)
        self.assertIn("upload 3 offline conversions to 'Booked job'", self.dialog)
        self.assertIn("cannot be deleted", self.dialog)
        validate_only, payload = self.sent[-1]
        self.assertFalse(validate_only)
        self.assertEqual((len(payload["events"]), payload["consent"]), (3, {"adUserData": "CONSENT_GRANTED"}))
        self.assertEqual(result.structured_content["results"], {"request_id": "req-123", "rows_sent": 3})

        stored = self.server._store().load(plan["plan_id"])
        self.assertEqual((stored["status"], stored["confirmed_via"]), ("applied", "elicitation"))
        self.assertFalse(any(key.startswith("hashed_") for row in stored["rows"] for key in row), "hashes are dropped once uploaded")
        again, message = await self.call("gads_apply_plan", {"plan_id": plan["plan_id"]})
        self.assertIn("already applied", message)

    async def test_apply_refuses_unlisted_accounts_read_only_and_lowered_caps(self):
        plan = (await self.plan(rows=self.rows()))[0].structured_content
        for env, expected in (({"GADS_ALLOWED_CUSTOMER_IDS": ""}, "not approved for writes"), ({"GADS_READ_ONLY": "true"}, "read-only"), ({"GADS_MAX_CONVERSION_VALUE": "200"}, "exceeds the cap of 200.00")):
            with mock.patch.dict(os.environ, env):
                self.server._settings.cache_clear()
                result, message = await self.call("gads_apply_plan", {"plan_id": plan["plan_id"]})
            self.server._settings.cache_clear()
            self.assertTrue(result.is_error)
            self.assertIn(expected, message)
        self.assertEqual([validate_only for validate_only, _ in self.sent], [True])

    async def test_a_tampered_plan_cannot_reach_another_account(self):
        plan = (await self.plan(rows=self.rows()))[0].structured_content
        stored = self.server._store().load(plan["plan_id"])
        stored["conversion_action"]["resource_name"] = "customers/9999999999/conversionActions/777"
        self.server._store().save(stored)
        result, message = await self.call("gads_apply_plan", {"plan_id": plan["plan_id"]})
        self.assertIn("does not belong to account", message)

    async def test_rollback_of_an_upload_is_a_gated_retraction(self):
        plan = (await self.plan(rows=self.rows()))[0].structured_content
        early, message = await self.call("gads_plan_rollback", {"plan_id": plan["plan_id"]})
        self.assertIn("not applied", message)
        await self.call("gads_apply_plan", {"plan_id": plan["plan_id"]})

        self.adjustment_errors = {1: "conversion_adjustment_upload_error.TOO_RECENT_CONVERSION: too recent"}
        result, message = await self.call("gads_plan_rollback", {"plan_id": plan["plan_id"]})
        self.assertFalse(result.is_error, message)
        retraction = result.structured_content
        self.assertEqual((retraction["kind"], retraction["retractions"], retraction["source_plan_id"]), ("conversion_retraction", 1, plan["plan_id"]))
        self.assertIn("row[1]: conversion_adjustment_upload_error.TOO_RECENT_CONVERSION", retraction["rejected_in_validation"][0])
        self.assertIn("row[2]", retraction["not_reversible"][0])
        self.assertIn("accepted 1 retractions in validation and rejected 1", retraction["validation"])
        self.assertFalse(any("days old" in warning for warning in retraction["warnings"]), "a 2 day old conversion is inside the bidding window")
        validate_only, adjustments = self.adjusted[-1]
        self.assertTrue(validate_only)
        self.assertEqual([("order_id" in a, "gclid_date_time_pair" in a) for a in adjustments], [(True, False), (False, True)])

        self.adjustment_errors = {}
        declined, _ = await self.call("gads_apply_plan", {"plan_id": retraction["plan_id"]}, "decline")
        self.assertTrue(declined.is_error)
        self.assertTrue(all(validate_only for validate_only, _ in self.adjusted))
        applied, message = await self.call("gads_apply_plan", {"plan_id": retraction["plan_id"]})
        self.assertFalse(applied.is_error, message)
        self.assertIn("retract 1 conversions from 'Booked job'", self.dialog)
        self.assertEqual((self.adjusted[-1][0], len(self.adjusted[-1][1])), (False, 1))
        self.assertEqual(applied.structured_content["results"], {"retracted": 1, "failed": []})
        undo, message = await self.call("gads_plan_rollback", {"plan_id": retraction["plan_id"]})
        self.assertIn("cannot undo one", message)

    async def test_late_retractions_say_they_no_longer_reach_bidding(self):
        rows = [{"gclid": GCLID, "conversion_time": ago(days=9), "order_id": "A-9"}]
        plan = (await self.plan(rows=rows))[0].structured_content
        await self.call("gads_apply_plan", {"plan_id": plan["plan_id"]})
        retraction = (await self.call("gads_plan_rollback", {"plan_id": plan["plan_id"]}))[0].structured_content
        self.assertIn("1 of these conversions are more than 7 days old", " ".join(retraction["warnings"]))

    async def test_retraction_that_google_rejects_entirely_is_not_stored(self):
        plan = (await self.plan(rows=self.rows()))[0].structured_content
        await self.call("gads_apply_plan", {"plan_id": plan["plan_id"]})
        self.adjustment_errors = {0: "CONVERSION_NOT_FOUND", 1: "CONVERSION_NOT_FOUND"}
        result, message = await self.call("gads_plan_rollback", {"plan_id": plan["plan_id"]})
        self.assertTrue(result.is_error)
        self.assertIn("accepted none of the 2 retractions", message)
        self.assertEqual(len(list((Path(self.dir.name) / "plans").glob("*.json"))), 1)

    async def test_upload_status_and_listing(self):
        plan = (await self.plan(rows=self.rows()))[0].structured_content
        pending, message = await self.call("gads_get_upload_status", {"plan_id": plan["plan_id"]})
        self.assertIn("not an applied conversion upload", message)
        await self.call("gads_apply_plan", {"plan_id": plan["plan_id"]})
        result, message = await self.call("gads_get_upload_status", {"plan_id": plan["plan_id"]})
        self.assertFalse(result.is_error, message)
        status = result.structured_content
        self.assertEqual((status["state"], status["rows_sent"], status["rows_rejected"]), ("PARTIAL_SUCCESS", 3, 1))
        listed = (await self.call("gads_list_plans", {}))[0].structured_content["plans"][0]
        self.assertEqual((listed["status"], listed["results"]["request_id"]), ("applied", "req-123"))
        self.assertNotIn("rows", listed)

    async def test_only_apply_is_gated_and_status_is_read_only(self):
        tools = {tool.name: tool for tool in await self.server.mcp.list_tools()}
        self.assertEqual(len(tools), 14)
        self.assertEqual([name for name, tool in tools.items() if tool.meta], ["gads_apply_plan"])
        self.assertTrue(tools["gads_get_upload_status"].annotations.read_only_hint)
        rows = tools["gads_plan_upload_click_conversions"].input_schema["properties"]["rows"]
        self.assertIn("ConversionRow", json.dumps(rows))


class ConfigTest(unittest.TestCase):
    def test_customer_ids(self):
        self.assertEqual(clean_customer_id("123-456-7890"), CID)
        for bad in ("12345", "abc", "customers/1234567890"):
            with self.assertRaises(ValueError):
                clean_customer_id(bad)


if __name__ == "__main__":
    unittest.main()
