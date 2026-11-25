# gads-write-mcp

I built this to run Google Ads for my clients with Claude without giving the model a free hand: Claude drafts every change, Google validates it, and nothing reaches an account until I approve it, with a rollback for each change. 89 offline tests cover the guardrails.

Write access to Google Ads for Claude, owned end to end. Replaces Pipeboard for mutations;
Google's official `google-ads-mcp` stays in place for reads.

Every change is two steps:

1. A `gads_plan_*` tool checks the change against local guardrails, reads the current
   values, and has Google validate it (`validate_only`, which also runs ad policy checks).
   Nothing is executed. It returns a plan with a `from -> to` diff.
2. `gads_apply_plan` executes a stored plan, only for allowlisted accounts, and only after
   you confirm in your client. The model cannot answer that confirmation.

Facts below were checked against Google's and Anthropic's documentation on 2026-09-20.

## Auth: nothing to set up, no secrets in config

The server uses the same Application Default Credentials as `google-ads-mcp`
(`~/.config/gcloud/application_default_credentials.json`). The `adwords` OAuth scope covers
writes as well as reads; the official server is read-only by tool design, not by credential.

Since 2026-09-09 Google decides API access by the Cloud project that owns the OAuth client.
Developer tokens are optional and ignored. My project has **Explorer** access:
<https://console.cloud.google.com/google/ads-apis/overview>.

| | Explorer (now) | Basic |
|---|---|---|
| Production operations per sliding 24 h | 2,880 | 15,000 |
| Campaign management and reporting | yes | yes |
| Keyword ideas and forecasts, invoices and billing, user access, account creation | blocked | yes |
| How to get it | have it | brand verification, then an automated review |

Quota: every mutated item is one operation, every search is one, and rejected requests
count. A plan of N changes costs about 3N (snapshot reads, validation, apply), so Explorer
covers roughly 900 single changes a day across all accounts, shared with `google-ads-mcp` reads.
Conversions are cheap: an upload plan costs 2 operations (two reads; the Data Manager API has its
own quota, 100,000 requests a day), and a retraction costs 2 however many rows it holds, because a
non-mutate request counts once. Neither service is on Explorer's restricted list.

If credentials expire (this command already carries the extra scope conversion uploads need, see below):

```bash
gcloud auth application-default login --scopes https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/datamanager --client-id-file=YOUR_OAUTH_CLIENT.json
```

### Conversion uploads need two one-time steps

Since 2026-06-15 Google refuses `ConversionUploadService.UploadClickConversions` from projects that
had not imported conversions between 2025-12-17 and 2026-06-15
(`CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE`), and names the **Data Manager API** as the replacement.
This project never imported, so uploads go through the Data Manager API. It needs:

1. The API enabled on the project:
   ```bash
   gcloud services enable datamanager.googleapis.com --project YOUR_PROJECT_ID
   ```
2. The `datamanager` OAuth scope on the credentials: run the login command above. Google classes it
   as a sensitive scope, so add "Data Manager API" under Google Auth Platform > Data Access first.
   If the OAuth app's publishing status is "Testing", Google expires its refresh tokens after 7 days.

Until both are done every upload plan fails with an error that says which one is missing. Everything
else keeps working on the current credentials, retractions included (they use the Google Ads API).

Both were done on 2026-09-20, and the live check (validate-only, on a real account) confirmed:
the Data Manager API validates upload requests from this setup; the legacy `UploadClickConversions`
answers `CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE` for this project; the adjustment service is open
to it, and its validation checks that the conversion exists (a made-up one came back
`CONVERSION_NOT_FOUND`, by row). Not yet proven, because only a real upload can prove it: that a
conversion imported through the Data Manager API can later be retracted through the Google Ads API.

## Register

Registered at user scope as `google-ads-write`, so it loads in every Claude Code session,
in the terminal and in the desktop app's Code tab. Keep that name out of any project
`.mcp.json` and out of `claude_desktop_config.json`: a same-named entry in either one can
replace this registration, allowlist included (which one wins differs between the CLI and
the desktop app).

Settings live in the registration, so changing one means removing and re-adding it:

```bash
claude mcp remove google-ads-write --scope user
```

```bash
claude mcp add --scope user google-ads-write --env GADS_ALLOWED_CUSTOMER_IDS=ID1,ID2 -- /path/to/gads-write-mcp/.venv/bin/gads-write-mcp
```

With an empty allowlist every plan validates and nothing can be applied. Servers connect
when a session starts, so new settings and new code take effect in the next session.

Not available in Cowork or on claude.ai (web, phone): those cannot reach a local server.

## Tools

| Tool | Does |
|---|---|
| `gads_plan_set_status` | Pause or enable campaigns, ad groups, ads, keywords |
| `gads_plan_update_budget` | New daily budget; warns on shared budgets |
| `gads_plan_add_keywords` | Add keywords; blocks broad match on manual bidding |
| `gads_plan_add_negative_keywords` | Campaign or ad group level |
| `gads_plan_update_keyword_bids` | New max CPCs, capped absolutely and by % increase |
| `gads_plan_create_rsa` | Responsive search ad, always PAUSED; 30/90 char limits, no `!` in headlines |
| `gads_plan_mutate` | Anything else `GoogleAdsService.Mutate` supports (64 resource types) |
| `gads_get_payload_guide` | Checked payloads for `gads_plan_mutate`: Search campaign, sitelinks/callouts/snippets, locations, network settings, shared negative lists, editing RSA text, image assets, Performance Max |
| `gads_plan_upload_click_conversions` | Offline conversions for one account and one conversion action: gclid, gbraid, wbraid, and enhanced conversions for leads (email, phone). Rows inline or from a local CSV |
| `gads_get_upload_status` | How Google processed an applied upload: rows rejected, by reason. Read-only |
| `gads_plan_rollback` | Plan restoring the values an applied plan overwrote. For an applied upload: plan retracting its conversions |
| `gads_apply_plan` | The only tool that changes an account. Executes all three kinds of plan behind the same approval |
| `gads_list_plans`, `gads_get_guardrails` | Local state, no API call |

### Offline conversions

`gads_plan_upload_click_conversions` reads rows (prefer `csv_path`: the file is read by the server,
so emails and phones never enter the conversation), checks them locally, checks the conversion
action and the account with two GAQL reads, then has the Data Manager API validate the request
(`validateOnly`). `gads_apply_plan` uploads. Google's own import template headers work as CSV
headers (`Google Click ID`, `Conversion Time`, `Conversion Value`, `Conversion Currency`, `Email`,
`Phone Number`, `Order ID`), French and Excel-semicolon exports included; other columns are ignored
and never read into the plan.

Privacy: emails and phones are normalized by Google's rules (lowercase, no whitespace; gmail and
googlemail also lose dots and any `+suffix`; phones become E.164) and SHA-256 hashed in memory as the
first thing that happens to a row. Plan files, the audit log, tool results and error messages only
ever hold hashes, and the hashes are deleted from the plan once it is applied or has failed. Error
messages about those fields name the row, never the value; a value that looks like an email or phone
in any other column blocks the plan without being shown. The normalization is tested against the
vectors of Google's own formatter.

What differs from every other plan, because the API differs:

- **Validation covers format and destination only.** Whether a row matches a click is decided after
  the upload. Google then reports rejected rows as counts per reason (`CLICK_NOT_FOUND`,
  `TOO_RECENT_CLICK`, `DUPLICATE_TRANSACTION_ID`...), never by row number. Ask
  `gads_get_upload_status` about 30 minutes after applying; processing can take 24 hours. Row-level
  precision therefore lives in the local checks, which name the row and the CSV line.
- **Not atomic, not deletable.** One bad row fails the whole validation, but once uploaded, rows
  succeed or fail one by one, and an imported conversion can only be retracted.
- **Undo is a retraction** through the Google Ads API (`ConversionAdjustmentUploadService`, type
  `RETRACTION`), planned with `gads_plan_rollback`, validated row by row by Google, and approved like
  any plan. Google only adjusts conversions between 24 hours and 54 days old, and Smart Bidding only
  takes a retraction into account within 7 days of the conversion. Rows are found by `order_id`, or by
  gclid and time; rows with neither, and wbraid rows, cannot be retracted, and the upload plan says
  how many those are before you approve it. A retraction cannot be undone.
- **The account must own the conversion action.** With cross-account conversion tracking that is
  the manager account; the plan says so and names it.
- **Consent is your statement.** `ad_user_data_consent` and `ad_personalization_consent` are sent
  only if given. The server never assumes `GRANTED`.

### What it cannot do

| | Why |
|---|---|
| Conversion value restatements, call conversions, store sales | Not built. Click conversion uploads and retractions are |
| Per-row results for an upload | The Data Manager API reports rejected rows as counts per reason only |
| Custom audiences | Separate service. Could be added |
| Keyword ideas, invoices, user invitations | Blocked at Explorer access; Basic unlocks them, and they would still need their own tools |
| Auction insights | Allowlist-only metrics. Not available to ordinary projects at any access level |
| Language targeting on Search campaigns | Google rejects these edits through the API from late September 2026 |
| Billing, account linking | Not part of `GoogleAdsService.Mutate`; out of reach by construction |

Account-wide trap: since 2026-04-01, one campaign without an EU political advertising
declaration makes Google reject every campaign-management change in that account. The
server explains this when it happens. The declaration is a legal statement, so the server
tells the model to ask you rather than pick a value.

## Guardrails

Set as environment variables on the server. The model cannot change them.

| Variable | Default | Effect |
|---|---|---|
| `GADS_ALLOWED_CUSTOMER_IDS` | empty | Accounts `gads_apply_plan` may write to. Empty = none |
| `GADS_READ_ONLY` | `false` | `true` forces every request to validate-only |
| `GADS_CONFIRM` | `auto` | How `gads_apply_plan` gets your approval. See below |
| `GADS_MAX_DAILY_BUDGET` | `100` | Cap on any daily budget, account currency |
| `GADS_MAX_CPC_BID` | `10` | Cap on any CPC bid or bid ceiling |
| `GADS_MAX_BID_INCREASE_PCT` | `50` | Max single-step bid increase |
| `GADS_MAX_OPS_PER_PLAN` | `50` | Operations per plan (Google's own limit is 10,000) |
| `GADS_MAX_CONVERSIONS_PER_PLAN` | `500` | Conversions per upload plan. Capped at 2,000, Google's limit for one request |
| `GADS_MAX_CONVERSION_VALUE` | `10000` | Cap on one conversion's value, account currency. Catches cents or micros sent as units |
| `GADS_ALLOW_REMOVE` | criteria, asset links, labels | Resource types that may be removed. Campaigns, ad groups, ads and budgets cannot be restored, so they are excluded |
| `GADS_BLOCKED_RESOURCES` | empty | Resource types that may not be touched at all |
| `GADS_PLAN_TTL_MINUTES` | `30` | Plans expire; stale plans must be rebuilt |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | unset | Manager id, only for accounts reached through the MCC |

Also always on: new campaigns and ads are created PAUSED; a plan can only touch the account
it names; campaign plans are atomic; guardrails run again at apply time; everything attempted is
appended to `~/.google-ads-ai/audit.jsonl` with the prior values.

Conversion uploads are also blocked, by row, for: a conversion action that is missing, not ENABLED or
not of type `UPLOAD_CLICKS`; a conversion time in the future, or older than the action's click-through
window (90 days at most); a repeated row (same identifier and time) or a repeated `order_id`; more than
one click id on a row, or no identifier at all; a time without a UTC offset when no `timezone` is given;
a value that is not a plain number; a CSV row labelled for another conversion action; customer data
terms not accepted when rows carry emails or phones. Warned, not blocked: rows without `order_id`, rows
that could never be retracted, dates without a time, conversions under 6 hours old, no consent given.

### The approval step

`gads_apply_plan` is published with `anthropic/requiresUserInteraction`. Claude Code
2.1.199 and later answers that flag with a permission prompt on every call, in every
permission mode; allow rules and hooks cannot skip it, and it has no "don't ask again".

- `auto` (default): when the client identifies as Claude Code 2.1.199+, that prompt is the
  approval. Any other client must show a server-driven confirmation dialog (MCP
  elicitation), and the call fails closed if it cannot.
- `elicit`: always the dialog. Not usable in the desktop app today: open bug reports say its
  Code tab, chat and Cowork decline these dialogs on their own without showing them
  (anthropics/claude-code #94450, #89858, #88901, #94806).
- `client`: always the client's own prompt, whatever the client is.

`gads_get_guardrails` reports the client it sees and which gate is active. If
`gads_apply_plan` ever runs without asking you first, stop and add an explicit rule to
`~/.claude/settings.json`, which prompts even in auto-accept and bypass modes:
`"permissions": {"ask": ["mcp__google-ads-write__gads_apply_plan"]}`.

Never allow-list `gads_apply_plan`, and do not configure `Elicitation`, `ElicitationResult`
or `PermissionRequest` hooks that match this server: they can answer for you.

## Tests

Offline, no credentials, no network:

```bash
.venv/bin/python -m unittest discover tests
```

Live, against the real API. Changes nothing: plans are validate-only, the server runs
read-only, and the one executed request targets a deactivated account where Google rejects
everything. It also has Google validate the payload guide's recipes and reports which pass, and
runs the conversion tools validate-only with a made-up click id: it reports whether the Data Manager
API accepts this setup, whether the adjustment service is open to this project, and whether the
legacy upload service is closed to it as Google announced.
Arguments: account id, a campaign id in it, a deactivated account id.

```bash
.venv/bin/python tests/smoke_live.py ACCOUNT_ID CAMPAIGN_ID DEACTIVATED_ACCOUNT_ID
```

## Upkeep

- API v25 (this library's default) is retired in August 2027; v26 is due October 2026.
  Twice a year: `.venv/bin/pip install -U "google-ads"`, bump the pin in `pyproject.toml`,
  run both test suites. The recipes in `guide.py` are parsed against the installed protos
  in the offline tests, so renamed fields show up there first.
- The MCP SDK is pinned to 2.x (2.2.0 is current). A 3.x release will need a migration pass.
- Conversion uploads call the Data Manager API's REST endpoint directly (`v1`, no client library:
  one POST and one GET did not justify a pre-1.0 dependency). Its error and rejection reason lists
  are declared open-ended by Google; unknown reasons are passed through as they come.
- Pending and expired upload plans keep hashed emails and phones in `~/.google-ads-ai/plans` until the
  files are deleted. Applied and failed ones do not.
- This is a local stdio server. Reaching it from claude.ai on the web or phone would mean
  hosting it remotely behind OAuth, which is a separate project with a larger attack surface.
