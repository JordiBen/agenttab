# Implementation plan: pay vendors directly (envelope model)

Status: **step 0 FAILED (Oct 18 2026): our app cannot void a hold whose payee is a vendor. Per the reviewer's rule, no code
was written; the plan below needs a new decision (see "Step 0 result").** The rest of this file is the plan as approved in
principle, kept for reference, with the reviewer's change (no "simulated vendor action") applied.
[vendor-payouts-plan.md](vendor-payouts-plan.md) (section "US re-spike", Oct 18 2026). Every fact cites a PayPal page or a
PayPal debug id from our own sandbox calls; anything not exercised yet is marked **UNSURE**. Code references are to `main`
at d4b1dae. Vendor merchant ids never appear in this repo: they come from environment variables (section c).

## What the spike proved (and didn't)
| Fact | Evidence |
|---|---|
| Orders v2 `intent: AUTHORIZE` with `payee.merchant_id` (a US sandbox business account) and our app's Vault card is accepted | `201`, debug ids `f9424082029fe`, `ca4c4b1fb2da2`, `f293760fbbe46` |
| Our app (the API caller) can capture that authorization after the fact | `201`, debug ids `f886941b5bd89`, `f880477cbe985`, `f328695fa0ed5` |
| Our app can read the capture (`GET /v2/payments/captures/{id}`); the capture shows the vendor as payee and the fee on the vendor's side ($10.00 gross, $0.75 fee, $9.25 net) | `200`, debug ids `ca4c4b1f5e9d9`, `f211706d4af5b`, `f45214718fa74` |
| Our app **cannot refund** that capture | `403 NOT_AUTHORIZED / PERMISSION_DENIED`, debug ids `f912700c665d3`, `ca4c4b2199af4`, `f452147396f4e` |
| Pay another account is a documented direct-merchant feature | https://developer.paypal.com/docs/checkout/standard/customize/pay-another-account/ |
| An authorization holds funds for 29 days; capture within the 3-day honor period, reauthorize after it | https://developer.paypal.com/docs/checkout/standard/customize/authorization/ |
| `PayPal-Request-Id` makes a POST idempotent; a repeat returns the latest status of the first request; it must be unique per request **and** per call type; 38-character limit | https://developer.paypal.com/api/rest/reference/idempotency/ |
| **Our app CANNOT void** an authorization whose payee is a vendor: `403 NOT_AUTHORIZED / PERMISSION_DENIED`, although the authorization lists a `void` link | step 0: debug ids `f6677437f0ba0` (hold), `f194959effd1e` (void refused), `ca4c50abfbc0d` (read back: still `CREATED`) |
| **UNSURE: webhooks.** Whether PayPal delivers `PAYMENT.CAPTURE.*` / `PAYMENT.AUTHORIZATION.VOIDED` for vendor-payee payments to *our* app's webhook was not observed (the spike had no webhook listener). Event names: https://developer.paypal.com/api/rest/webhooks/event-names/ | not yet observed |

## Step 0 result: the void spike (approved; run Oct 18 2026, 08:11 Asia/Jerusalem (UTC+3); local, sandbox host only)
Script `notes/scripts/vendor_void_spike.py` (private); redacted log kept privately. One US vendor (SampleBox), $10.00,
our app's Vault card (the test Visa), merchant id from the private env file.

| # | Request | HTTP | PayPal debug id | Result |
|---|---|---|---|---|
| prereq | `POST /v3/vault/setup-tokens` | 201 | `f10800557b219` | setup token |
| prereq | `POST /v3/vault/payment-tokens` | 201 | `f82138096e930` | vault token (a first attempt minutes earlier was refused, `403`, debug id `f886754a2499e`; it succeeded on the re-run 3 s after the setup token; **UNSURE** why) |
| **1** | `POST /v2/checkout/orders` intent=AUTHORIZE, `payee.merchant_id` = SampleBox (US), vault card | **201** | `f6677437f0ba0` | authorization `CREATED`, payee = SampleBox |
| **2** | `POST /v2/payments/authorizations/{id}/void` by our app | **403** | `f194959effd1e` | `NOT_AUTHORIZED` / `PERMISSION_DENIED` on `authorization_id` |
| **3** | `GET /v2/payments/authorizations/{id}` | 200 | `ca4c50abfbc0d` | still `CREATED`; links offered: `self, capture, void, reauthorize, up`; expires 2026-11-16T05:11:21Z |
| cleanup | `DELETE /v3/vault/payment-tokens/<vault token>` | 204 | `f9245691bc030` | test token removed |

Void response (`403`, debug id `f194959effd1e`): `{"name": "NOT_AUTHORIZED", "message": "Authorization failed due to
insufficient permissions.", "details": [{"issue": "PERMISSION_DENIED", "field": "authorization_id", "location": "path",
"description": "You do not have permission to access or perform operations on this resource."}]}`.

**What it means:** with a vendor as payee our app can create the hold and capture it (US re-spike), but can neither void
it (step 0) nor refund the capture (US re-spike). Every "on failure: void" path of the plan below (failed delivery,
rejection, expiry, freeze, capture problems) would leave a hold on the owner's card until it expires (29 days:
https://developer.paypal.com/docs/checkout/standard/customize/authorization/). The plan as written is not safe to build.

**Leftover:** the step 0 hold ($10.00 sandbox, payee SampleBox) stays `CREATED` until it expires, Nov 16 2026 07:11
Asia/Jerusalem (UTC+2). We won't capture it. **UNSURE** whether the vendor can void it from its sandbox account.

**Options for the reviewer (none started):**
1. **Capture-only, no holds.** Reserve under the lock → fulfil → one `intent: CAPTURE` order (payee + vault card) only after
   delivery succeeds; nothing ever needs a void. Above the threshold: no hold at request time; owner approves → fulfil →
   capture. If the card declines after delivery, the purchase is `payment_failed`, the vendor delivered unpaid, the agent is
   frozen. All our vendors deliver instantly (simulated goods or live data), so this fits the demo. **UNSURE:** `intent:
   CAPTURE` with payee + vault card wasn't tried (the spikes used AUTHORIZE); it needs one more approved sandbox call.
   Trade-off: the owner's card isn't checked before delivery.
2. **Keep the shipped design for the hackathon** (money to AgentTab's sandbox account, where void and refund both work),
   document Option A's results, and leave vendor payouts as the documented production route.
3. **Multiparty / partner onboarding**, which acts on the seller's behalf with `PayPal-Auth-Assertion`
   (https://developer.paypal.com/docs/multiparty/issue-refund/). **UNSURE** whether that also covers void (the page covers
   refunds). Not feasible before Nov 5.

## (a) Envelope model
AgentTab holds no money. An agent has an **envelope**: owner-set limits enforced by the same `procurement.decide()` pipeline,
plus the new rule that every approved purchase is paid by the owner's saved (Vault) card **straight to the vendor**.

### Flow below the approval threshold
1. Agent calls `buy`. Deterministic layers and the guard run as today (`do_purchase`, `app/main.py`).
2. **Reserve** (one SQLite `BEGIN IMMEDIATE` transaction under the per-agent lock): re-check caps counting held + captured
   amounts, insert the purchase as `authorizing`. Commit. (Details under "Atomic caps".)
3. **Authorize**: `POST /v2/checkout/orders`, `intent: AUTHORIZE`, `purchase_units[0].payee.merchant_id` = the vendor's id from
   env, `custom_id` = purchase id, `payment_source.card.vault_id` = the owner's Vault token with `stored_credential`
   MERCHANT/UNSCHEDULED/SUBSEQUENT (the body the spike used, debug id `f9424082029fe`). `PayPal-Request-Id: at-{pid}-auth`.
   Success → `held` (store order id, authorization id). PayPal error → `failed` (reservation released; nothing held).
4. **Fulfil** (existing `fulfill()`). Failure → **void** (`at-{pid}-void`) → `failed_voided`.
5. **Capture**: `POST /v2/payments/authorizations/{id}/capture` with `final_capture: true` and the exact amount,
   `PayPal-Request-Id: at-{pid}-cap`. Check the response: `status` COMPLETED/PENDING, amount equals the reserved amount, and
   `payee.merchant_id` equals the configured vendor id (the spike's capture shows that field, debug id `ca4c4b1f5e9d9`).
   Success → `completed`. Declined → void (if still possible), `capture_declined`, agent frozen (today's behaviour in
   `complete_authorized`). Payee mismatch → `flagged`, agent frozen, never retried.

### Flow above the approval threshold
1. Steps 1-3 as above, but the purchase goes to `pending_approval` **with the hold already placed** (`held_for_approval`), so the
   owner approves money that is already reserved on their card for that vendor. Response `202` with an approval link into the
   AgentTab owner console (no PayPal checkout page any more).
2. Owner approves (admin token, or the demo-session owner) → atomic claim `pending_approval → completing`
   (`ledger.claim_purchase`, already used against double completion) → fulfil → capture (as above).
3. Owner rejects, or the approval window (24 h today) expires → void → `rejected_voided` / `expired_voided`. 24 h is inside the
   3-day honor period (authorization page above), so capture never needs a reauthorization.
4. No saved card → `402 card_required` with the owner's save-card link. **No fallback** to a PayPal checkout paid to AgentTab.
   (A buyer-approved checkout order with `payee` is documented, pay-another-account page, but **UNSURE** in our sandbox and it
   needs a browser per purchase; not in scope.)

### Top-up and balance: removed and reframed as a budget
- Removed for vendor-paid agents: `POST /api/agents/{aid}/topup`, `/topup-sandbox-card`, `/topup-saved-card`,
  `/api/me/topup-request`, the MCP tool `request_topup`, auto top-up (`auto_topup_hold`), and the tab refund
  `POST /api/agents/{aid}/refund`. They answer `410 Gone` with "AgentTab no longer holds money; the owner sets an envelope
  budget". The `402 payment_required` "tab empty" answer becomes `402 card_required` (no saved card) or `403` with layer `caps`
  (envelope used up).
- New agent field `envelope_cents_30d` (rolling 30-day total the owner allows). `balance` in API/UI/MCP becomes
  `envelope: {budget_30d, held, captured, remaining}`. The MCP tool `get_tab` keeps its name (clients depend on it) and returns
  the envelope; its description changes.
- Kept as they are: daily cap, per-purchase max, per-vendor 30-day budgets, approval threshold.

### Migration
- `ALTER TABLE agents ADD COLUMN funding_model TEXT DEFAULT 'tab'` and `envelope_cents_30d INTEGER`; purchases get
  `amount_cents INTEGER`, `paypal_authorization` (exists), `payee_merchant_id`, `request_seq`. Old rows keep `amount_micro`.
- Per-agent cutover, never automatic: an agent with a positive tab balance is first refunded through the existing tab refund
  (to the owner's PayPal captures), then switched to `funding_model='vendor'`. Old purchases keep `funding='tab'/'direct'` and
  their old refund path; new ones are `funding='vendor'`.
- Demo sessions created after the merge are `vendor` agents with the sandbox test card saved in the Vault (the app already
  vaults it: `paypal.py`, `/v3/vault/setup-tokens` with the sandbox card) and an envelope of $100 instead of a $100 top-up.
- Feature flag `VENDOR_PAYOUTS=1`. Default **off** until the reviewer approves the merge and a deploy; the live site keeps
  today's behaviour until then.

### Atomic caps (held + captured), per-agent lock, whole cents, derived Request-Id
- **Counted amount** = sum of `amount_cents` of purchases in `authorizing, held, held_for_approval, pending_approval,
  completing, completed, refund_requested, capture_declined(flagged)` within the window. `failed`, `*_voided` and `refunded`
  (vendor refund confirmed) don't count. `refund_requested` keeps counting until the vendor's refund is confirmed:
  conservative, never over budget.
- New `Ledger.reserve_vendor_purchase(...)`: one `BEGIN IMMEDIATE` transaction that computes daily (24 h), per-vendor 30-day
  and envelope 30-day totals from that set, checks per-purchase max, and inserts the purchase as `authorizing`, or rolls back
  with the reason. The same pattern as today's `commit_tab_purchase` (`app/ledger.py`), but the reservation is written
  **before** the PayPal call, so a parallel buy sees the hold immediately.
- The per-agent lock (`agent_lock_or_busy`, bounded wait, 429 when busy) still serializes one agent's purchases in a
  process; `BEGIN IMMEDIATE` covers several processes. No DB transaction is held open across a PayPal call.
- Money is whole cents: `amount_cents` is an integer; `to_cents()` refuses any Decimal with more than 2 decimal places;
  PayPal `value` strings are formatted from cents (`f"{c // 100}.{c % 100:02d}"`) and parsed back to cents when checking
  responses. No floats in money paths (the JSON `*_usd` fields are display only, as today).
- `PayPal-Request-Id` is derived from the action, never random: `at-{pid}-auth`, `at-{pid}-cap`, `at-{pid}-void`
  (`pid` = `pu_` + 12 hex, so the ids are 18-19 characters, under the 38 limit; unique per request and per call type, as the
  idempotency page requires). Today's ids are random (`secrets.token_hex` in `do_purchase`/`auto_topup_hold`); a retry after
  a timeout would create a second order. With derived ids, a retry or a reconcile replay returns the first result.
- Crash safety: a purchase left in `authorizing` (crash between reserve and PayPal's answer) keeps counting against the caps;
  reconcile repeats the authorize call with the same Request-Id to learn its outcome, then voids it (or marks it `failed`).

## (b) Refunds
- **Before capture: void.** Failed delivery, rejection, expiry, freeze and capture problems release the hold with
  `POST /v2/payments/authorizations/{id}/void` (`at-{pid}-void`). Nothing is charged. **Refused by PayPal for vendor-payee holds (step 0, debug id `f194959effd1e`)**, so this
  path does not work as planned; see "Step 0 result".
- **After capture: a refund request to the vendor.** Our app gets `403 PERMISSION_DENIED` on the vendor's capture (debug ids
  `f912700c665d3`, `ca4c4b2199af4`, `f452147396f4e`). Refunding on a seller's behalf is a partner feature ("Be an approved
  partner", `PayPal-Auth-Assertion`): https://developer.paypal.com/docs/multiparty/issue-refund/. So:
  - `POST /api/purchases/{pid}/refund` on a vendor-paid purchase never calls PayPal's refund. It moves `completed →
    refund_requested` (atomic claim) and records who asked and why. The owner console shows "Refund requested: with
    SampleBox. The vendor refunds through PayPal; AgentTab can't move this money." Agents see the same status.
  - The request is confirmed when the capture shows `REFUNDED`/`PARTIALLY_REFUNDED` (reconcile `GET`, which our app is allowed
    to do, debug ids above) or a `PAYMENT.CAPTURE.REFUNDED` webhook arrives (**UNSURE** it reaches us, see webhooks). Then
    `refund_requested → refunded`, and the amount stops counting against the envelope.
  - Refund windows and the non-refundable rule for live data stay as they are (`refund_one`).
- **Demo refunds (reviewer decision, Oct 18):** no simulated vendor action and no per-vendor REST apps. The demo shows
  `refund_requested` with the explanation above; when the vendor refunds, reconcile reads the capture as `REFUNDED` and moves
  the purchase to `refunded`. No fake "refunded" state.

## (c) Vendor merchant ids from env; no id, no payment
- `VENDOR_SAMPLEBOX_MERCHANT_ID`, `VENDOR_SHOPSUITE_MERCHANT_ID`, `VENDOR_SITESCOPE_MERCHANT_ID` (Render env and the
  private chmod-600 file; never in git, logs or API responses; the UI shows only "PayPal account connected: yes/no").
- A vendor without an id is **never paid**: a new deterministic layer `payee` in `decide()` blocks with "vendor has no PayPal
  account configured", before the guard (no AI call spent), with **no fallback** to AgentTab's account. Forbidden vendors
  (gift cards, ad boosting) never get ids and stay blocked by policy as today.
- Startup logs how many vendors are payable (count only). `/healthz` adds `vendors_payable: n`.
- Every capture response is checked against the configured id (a), so a wrong env value freezes the agent instead of paying
  someone else silently.

## (d) Claims only after code and tests
- README, `docs/video-script.md` and the landing copy keep today's wording until the feature is merged with its tests. Only
  then: "the vendor is paid through PayPal". Before that, the README keeps "a real product would use multiparty".
- A docs test enforces it: the phrase "vendor is paid through PayPal" (and close variants) may appear in README / docs /
  `static/` only if `tests/test_vendor_payouts.py` exists and `VENDOR_PAYOUTS` is documented as on.
- Landing copy is owned by another worker on `feature/landing-redesign`; this plan hands them the exact sentence and the
  test, and doesn't edit that branch.

## (e) Tests (each must fail before the change)
Mock PayPal (existing mock client, extended to record every request body, header and `PayPal-Request-Id`) unless noted.
1. **Payee on the order:** a below-threshold buy sends `intent: AUTHORIZE`, `payee.merchant_id` = the env id, the owner's
   `vault_id`, `custom_id` = pid; no `fund` ledger entry is written. (Fails today: no payee, money funds the tab.)
2. **Authorize → fulfil → capture order:** capture happens only after `fulfill()` returns; the captured amount equals the
   reservation in cents.
3. **Void on fulfil failure:** `fulfill()` raises → void called once with `at-{pid}-void`, status `failed_voided`, the amount
   no longer counts against any cap.
4. **Two parallel buys can't exceed caps:** 10 threads buy for one agent with a slow mock authorize; the sum of held + captured
   never exceeds daily cap, vendor budget or envelope; the rejected ones get `403 caps`. Also across two `Ledger` instances on
   one DB file (two processes). (Fails today: holds aren't counted before capture.)
5. **Double approval:** two concurrent approvals of one pending purchase → exactly one capture call, the other gets `409`.
6. **Capture idempotency after delivery:** the mock captures, then times out; the retry reuses `at-{pid}-cap` and the mock
   returns the first capture; one `completed` row, one capture id. A reconcile replay does the same.
7. **Request-Id derivation:** deterministic per (pid, action), distinct across actions, ≤ 38 chars, never random.
8. **Whole cents:** a price with sub-cent precision is refused; PayPal `value` strings round-trip to the same cents.
9. **No id, no payment:** a vendor without `VENDOR_*_MERCHANT_ID` → `403` layer `payee`, zero PayPal calls, zero AI calls.
10. **Payee mismatch:** the capture response names another merchant → `flagged`, agent frozen.
11. **Approval:** above threshold the hold is placed at request time; reject → void; expiry → void; approve → fulfil → capture.
12. **No card → 402 `card_required`,** never a checkout to AgentTab.
13. **Refund after capture → `refund_requested`,** no PayPal refund call, owner and agent views say it's with the vendor; a
    later capture `GET` showing `REFUNDED` moves it to `refunded` and frees the envelope.
14. **Refund stays with the vendor:** no code path calls PayPal's refund for a vendor-paid purchase; the demo has no
    button that refunds as the vendor.
15. **Top-up endpoints `410`** for vendor agents; legacy tab agents unchanged (existing tests keep passing).
16. **Webhooks:** `PAYMENT.AUTHORIZATION.VOIDED`, `PAYMENT.CAPTURE.COMPLETED/DECLINED/REFUNDED` map to the purchase by
    `custom_id`/capture id, idempotently (existing `webhook_events` table).
17. **Reconcile:** stale `authorizing` → replay with the same Request-Id then void; stale `held_for_approval` past expiry →
    void; a capture whose amount/status/payee disagrees with the ledger → freeze.
18. **AI unavailable still holds for a human** (existing tests, re-run in vendor mode).
19. **Live sandbox tests (skip without keys, run locally only):** one real authorize → capture to a US vendor; one authorize →
    void; never against the hosted site.
20. **UI e2e:** envelope instead of balance; statuses held / refund requested with the vendor; enc() on every vendor-supplied string.

## (f) Time estimate (feature freeze Nov 5, deadline Nov 12)
Today is Oct 18: 13 working days to the freeze if work starts Oct 20.

| Step | Days |
|---|---|
| 0. Void spike (approval, 3 sandbox calls) | 0.5 |
| Ledger: reservation, cents, migration, flag | 1.5 |
| Purchase + approval flows, derived Request-Ids, payee layer | 2 |
| Refund requests (refund_requested + reconcile) | 1 |
| Webhooks + reconcile | 1 |
| UI (envelope, statuses), MCP descriptions | 1.5 |
| Tests (written first, alongside each step) | 2 |
| Docs/README/video claims, review rounds | 1.5 |
| **Total** | **11** (about 2 days of slack before Nov 5) |

If step 0 fails (void refused), add 1-2 days for the fulfil-first fallback, which eats the slack; then the recommendation is
to keep the shipped design for the hackathon and ship this behind the flag, off.

## Webhooks and reconcile changes
- **Webhooks** (`handle_webhook`, `app/main.py`): add `PAYMENT.AUTHORIZATION.VOIDED` (also fired when an authorization
  reaches its validity limit, event-names page) → `*_voided`/`expired_voided`; match `PAYMENT.CAPTURE.*` to vendor purchases by
  capture id or `custom_id` = pid (today `custom_id` is the agent id, `paypal.py`); `PAYMENT.CAPTURE.REFUNDED` on a vendor
  purchase → `refunded` (no ledger credit: there is no tab). `CHECKOUT.ORDER.APPROVED` is no longer used by vendor agents.
  **UNSURE** whether these events are delivered for vendor-payee payments, so nothing depends on them: reconcile is the source
  of truth.
- **Reconcile** (`/api/agents/{aid}/reconcile`): today it walks `fund` entries; for vendor agents it walks vendor purchases
  instead: `authorizing` older than 2 minutes → replay authorize with the same Request-Id, then void; `held*` past expiry →
  void; `completed`/`refund_requested` in the last 30 days → `GET /v2/payments/captures/{id}` (allowed, debug ids above):
  amount, status, payee. Mismatch freezes the agent, as today. Auth `GET` on a vendor-payee authorization is **UNSURE** (only the
  capture `GET` was exercised).

## Risks
1. **Void refused** (step 0, debug id `f194959effd1e`): failed purchases would leave holds on the owner's card for up to 29 days.
2. **Refunds depend on the vendor.** The product can only request them; the owner experience is weaker than today's
   automatic tab refunds. Real fix: multiparty partner onboarding (https://developer.paypal.com/docs/multiparty/issue-refund/,
   https://developer.paypal.com/docs/multiparty/seller-onboarding/build-onboarding/), not in hackathon scope.
3. **Country/capability:** the Cyprus vendor accounts were refused (`PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING`, debug id
   `f259063a191ee`); US accounts worked. Real vendors need card processing; **UNSURE** what decides it.
4. **Webhook delivery** for vendor-payee payments is unknown; mitigated by reconcile polling.
5. **Disputes and seller protection land on the vendor:** the capture showed `seller_protection: NOT_ELIGIBLE` (debug id
   `ca4c4b1f5e9d9`). AgentTab has no dispute role.
6. **Production terms:** paying third parties from our app is documented for direct merchants (pay-another-account page);
   whether that is acceptable at scale without the partner program is **UNSURE**. The README keeps saying multiparty is the
   production route.
7. **The live demo belongs to the judges:** the flag stays off on the hosted site until reviewer approval; the first deploy is
   checked only with `/`, `/healthz` and MCP 401/initialize, as always.
8. **Scope vs freeze:** 11 days of work in 13; any extra review round uses the slack.
9. **Copy drift:** landing copy is on another worker's branch; the docs test in (d) catches a premature claim on merge.

## Decisions needed
1. Step 0 failed: choose option 1 (capture-only; needs one more approved sandbox call for `intent: CAPTURE` + payee), option 2
   (keep the shipped design for the hackathon) or option 3 (multiparty, after the hackathon). See "Step 0 result".
2. (Decided Oct 18: no simulated vendor action; demo refunds show `refund_requested`, reconcile detects `REFUNDED`.)
