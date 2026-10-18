# Implementation plan: pay vendors directly, capture-only (envelope model)

Status: **plan only, no code on this branch.** The reviewer chose Option 1 (capture-only) on Oct 18 2026, conditional on one
more sandbox spike; **that spike succeeded** (see "Capture-only spike"). Code waits for the go-ahead and will be built behind
`VENDOR_PAYOUTS=1` (off by default). Every fact cites a PayPal page or a PayPal debug id from our own sandbox calls; anything
not exercised yet is marked **UNSURE**. Code references are to `main` at d4b1dae. Vendor merchant ids never appear in this
repo: they come from environment variables (section c). Earlier research and spikes: [vendor-payouts-plan.md](vendor-payouts-plan.md).

## Evidence
| Fact | Evidence |
|---|---|
| One Orders v2 call with `intent: CAPTURE`, `payee.merchant_id` = a US sandbox vendor and our app's Vault card completes the payment immediately: order `COMPLETED`, one capture, **no authorization** | capture-only spike: `201` debug id `f17615595ff9b`; order read back `200` `f1488058ce2f1` |
| The capture is the vendor's: payee = the vendor, `COMPLETED`, $10.00 gross, $0.75 PayPal fee, $9.25 net to the vendor | capture read back `200`, debug id `f7416798d75bf` (same split in the US re-spike, `ca4c4b1f5e9d9`) |
| Our app **cannot refund** a vendor's capture | `403 NOT_AUTHORIZED / PERMISSION_DENIED`: `f729042a2343e` (capture-only spike), `f912700c665d3`, `ca4c4b2199af4`, `f452147396f4e` (US re-spike) |
| Our app **cannot void** a hold whose payee is a vendor, which is why there are no holds in this plan | step 0: `403` debug id `f194959effd1e`; hold read back still `CREATED`, `ca4c50abfbc0d` |
| Our app **can read** the order and the vendor's capture, which reconcile relies on | `f1488058ce2f1`, `f7416798d75bf`, `ca4c4b1f5e9d9` |
| Pay another account is a documented direct-merchant feature | https://developer.paypal.com/docs/checkout/standard/customize/pay-another-account/ |
| `PayPal-Request-Id` makes a POST idempotent: a repeat returns the latest status of the first request; unique per request and per call type; 38-character limit; two simultaneous requests with the same id may fail the second | https://developer.paypal.com/api/rest/reference/idempotency/ |
| Refunding on a seller's behalf is a partner feature (`PayPal-Auth-Assertion`, "Be an approved partner") | https://developer.paypal.com/docs/multiparty/issue-refund/ |
| **UNSURE: webhooks.** Whether PayPal delivers `PAYMENT.CAPTURE.*` events for vendor-payee captures to our app's webhook was not observed. Event names: https://developer.paypal.com/api/rest/webhooks/event-names/ | not observed |
| **UNSURE: card declines.** No spike has seen a decline on a vendor-payee capture order; the decline path is planned from today's code (`capture_declined` handling) and tested with the mock only | not observed |

## Capture-only spike (approved; run Oct 18 2026, 08:45 Asia/Jerusalem (UTC+3); local, sandbox host only)
Script `notes/scripts/vendor_capture_spike.py` (private); redacted log kept privately. One US vendor (SampleBox), $10.00,
the sandbox test Visa saved in the Vault, merchant id from the private env file.

| # | Request | HTTP | PayPal debug id | Result |
|---|---|---|---|---|
| prereq | `POST /v3/vault/setup-tokens` | 201 | `f400986f64122` | setup token |
| prereq | `POST /v3/vault/payment-tokens` | 201 | `f336863c410e9` | vault token (VISA 1111) |
| **1** | `POST /v2/checkout/orders` `intent: CAPTURE`, payee = SampleBox (US), `payment_source.card.vault_id` + `stored_credential` MERCHANT/UNSCHEDULED/SUBSEQUENT | **201** | `f17615595ff9b` | order `COMPLETED`; `payments` contains `captures` only |
| **2** | `GET /v2/checkout/orders/{id}` | 200 | `f1488058ce2f1` | `COMPLETED`; payee = SampleBox; 1 capture, **0 authorizations** |
| **3** | `GET /v2/payments/captures/{id}` | 200 | `f7416798d75bf` | `COMPLETED`; payee = SampleBox; gross $10.00, fee $0.75, net $9.25; links `self, refund, up`, and `up` is the **order** (no authorization behind it) |
| **4** | `POST /v2/payments/captures/{id}/refund` by our app | **403** | `f729042a2343e` | `NOT_AUTHORIZED` / `PERMISSION_DENIED` on `capture_id` (expected) |
| cleanup | `DELETE /v3/vault/payment-tokens/<vault token>` | 204 | `ca4c53c43cad3` | test token removed |

Void: not applicable. The order has no authorization, so there is no hold to void or leave behind (calls 2 and 3).
The money went from the card straight to the vendor (call 3); AgentTab's account is not on the money path.

## Step 0 (kept for the record): our app can't void a vendor-payee hold
Run Oct 18 2026, 08:11 Asia/Jerusalem: hold `201` (`f6677437f0ba0`), void by our app `403 PERMISSION_DENIED`
(`f194959effd1e`), read back still `CREATED` (`ca4c50abfbc0d`). An authorization is valid for 29 days
(https://developer.paypal.com/docs/checkout/standard/customize/authorization/). This is why the plan places no holds.

**Leftover sandbox state (documented, left alone):**
- The step 0 hold, $10.00, payee SampleBox (US), stays `CREATED` until it expires on **Nov 16 2026, 07:11 Asia/Jerusalem
  (UTC+2)** (`expiration_time` 2026-11-16T05:11:21Z, debug id `ca4c50abfbc0d`). We won't capture it and can't void it.
- Four $10.00 sandbox captures sit in the US vendor accounts, not refunded (three from the US re-spike, one from the
  capture-only spike). Only the vendors could refund them. Sandbox money only.

## (a) Envelope model, capture-only
AgentTab holds no money and places no holds. An agent has an **envelope**: owner-set limits enforced by the same
`procurement.decide()` pipeline. Every purchase that goes through is paid by the owner's saved (Vault) card straight to the
vendor, in **one** PayPal call made **only after the vendor delivered**.

### Below the approval threshold
1. Agent calls `buy`. Deterministic layers and the guard run as today (`do_purchase`, `app/main.py`), plus the new `payee`
   layer (section c).
2. **Reserve:** under the per-agent lock, one SQLite `BEGIN IMMEDIATE` transaction re-checks every cap counting reserved +
   paid amounts and inserts the purchase as `reserved` (see "Atomic caps"). Commit. No PayPal call yet.
3. **Fulfil** (existing `fulfill()`). If it fails: the purchase becomes `failed`, the reservation stops counting, and
   **there is no PayPal call at all**.
4. **Pay**, only if fulfil succeeded: `POST /v2/checkout/orders` with `intent: CAPTURE`, `purchase_units[0].payee.merchant_id`
   = the vendor's id from env, `custom_id` = purchase id, `amount` from the reservation in cents, `payment_source.card.vault_id`
   = the owner's Vault token with `stored_credential` MERCHANT/UNSCHEDULED/SUBSEQUENT (the body that completed in the spike,
   `f17615595ff9b`). `PayPal-Request-Id: at-{pid}-pay`.
5. **Check the response** before calling it paid: order `COMPLETED`, exactly one capture, capture `COMPLETED`, amount equal to
   the reservation in cents, `payee.merchant_id` equal to the configured vendor id (the spike's response carries all of these,
   `f1488058ce2f1`, `f7416798d75bf`). Then `completed` with order id and capture id.
6. **Outcomes after delivery, all fail closed:**
   - Card declined or PayPal refuses the order (a definite 4xx): `payment_failed`, the vendor delivered unpaid, the agent is
     **frozen** for the owner to reconcile. Nothing was charged, so it stops counting against the caps.
   - Capture `PENDING`: `payment_pending`, keeps counting; reconcile resolves it.
   - Timeout or 5xx: retry once with the **same** Request-Id (the idempotency page: a repeat returns the first request's latest
     status). Still unknown: `payment_unknown`, keeps counting, agent frozen until reconcile replays the same Request-Id.
   - Response doesn't match (amount, payee, status, more than one capture): `flagged`, keeps counting, agent frozen, never
     retried automatically.

### Above the approval threshold
1. `decide()` returns `approval` (as today). Reserve as `pending_approval` (counts against the caps), expiry 24 h. **No hold,
   no PayPal call.** Response `202` with a link into the AgentTab owner console.
2. The owner approves in the console (admin token, or the demo-session owner; never a plain agent key). Atomic claim
   `pending_approval → completing` (`ledger.claim_purchase`, already used against double completion), then fulfil → pay →
   check, exactly as steps 3-6 above. The amount paid is the reserved amount.
3. Reject or expiry: `rejected` / `expired`, no PayPal call, the reservation stops counting.

### No saved card
A vendor-funded agent without a Vault card gets `402 card_required` with the owner's save-card link. No fallback to a payment
to AgentTab.

### Agents without VENDOR_PAYOUTS keep today's flows
With the flag off (the default), or for an agent still on `funding_model = 'tab'`, nothing changes: the tab, top-ups, auto
top-up, and the existing above-threshold approval where the owner approves a PayPal hold paid to AgentTab's sandbox account
(`create_order(..., intent="AUTHORIZE")` in `do_purchase`, completed by `complete_authorized`) stay exactly as they are, with
their tests.

### Top-up and balance for vendor-funded agents: reframed as a budget
- For `funding_model = 'vendor'` agents: `POST /api/agents/{aid}/topup`, `/topup-sandbox-card`, `/topup-saved-card`,
  `/api/me/topup-request`, the MCP tool `request_topup`, auto top-up and the tab refund `POST /api/agents/{aid}/refund`
  answer `410 Gone` ("AgentTab doesn't hold money for this agent; the owner sets an envelope budget").
- New agent field `envelope_cents_30d` (rolling 30-day total the owner allows). For these agents `balance` in API/UI/MCP becomes
  `envelope: {budget_30d, reserved, paid, remaining}`. The MCP tool `get_tab` keeps its name and returns the envelope.
- Kept as they are: daily cap, per-purchase max, per-vendor 30-day budgets, approval threshold.

### Migration
- `ALTER TABLE agents ADD COLUMN funding_model TEXT DEFAULT 'tab'` and `envelope_cents_30d INTEGER`; purchases get
  `amount_cents INTEGER`, `payee_merchant_id`, `paypal_order` (exists), `paypal_capture` (exists). Old rows keep `amount_micro`.
- Per-agent cutover, never automatic: an agent with a positive tab balance is first refunded with the existing tab refund,
  then switched to `vendor`. Old purchases keep `funding = 'tab'/'direct'` and their old refund path.
- With the flag on, new demo sessions are vendor-funded: the sandbox test card saved in the Vault (the app already does this,
  `paypal.py`) and a $100 envelope instead of a $100 top-up.
- `VENDOR_PAYOUTS=1` stays **off** on the hosted site until the reviewer approves an exact sha and a deploy.

### Atomic caps (reserved + paid), per-agent lock, whole cents, derived Request-Id
- **Counted** statuses: `reserved, pending_approval, completing, payment_pending, payment_unknown, flagged, completed,
  refund_requested`. **Not counted:** `failed`, `rejected`, `expired`, `payment_failed` (PayPal definitely charged nothing),
  `refunded` (vendor refund confirmed by reconcile). `refund_requested` keeps counting until the refund is confirmed.
- New `Ledger.reserve_vendor_purchase(...)`: one `BEGIN IMMEDIATE` transaction computes the daily (24 h), per-vendor 30-day and
  envelope 30-day totals from the counted set, checks the per-purchase max, and inserts the purchase as `reserved` or
  `pending_approval`, or rolls back with the reason. Same pattern as today's `commit_tab_purchase` (`app/ledger.py`), but
  written **before** fulfilment, so a parallel buy sees the reservation at once.
- The per-agent lock (`agent_lock_or_busy`, bounded wait, `429` when busy) serializes one agent's purchases in a process;
  `BEGIN IMMEDIATE` covers several processes. No DB transaction is held open across fulfilment or a PayPal call. Status
  changes after the reservation are single conditional `UPDATE`s (`claim_purchase` style), so two workers can't pay one
  purchase twice.
- Whole cents: `amount_cents` is an integer; `to_cents()` refuses a Decimal with more than 2 decimal places; PayPal `value`
  strings are formatted from cents (`f"{c // 100}.{c % 100:02d}"`) and responses are parsed back to cents before comparing.
- `PayPal-Request-Id` comes from the action, never random: `at-{pid}-pay` for the one create-order call (`pid` = `pu_` + 12
  hex, so 18 characters, under the 38 limit). Today's ids are random (`secrets.token_hex` in `do_purchase`), so a retry after a
  timeout could pay twice; with the derived id a retry or a reconcile replay returns the first result.

## (b) Refunds
- There is never a hold, so nothing to void.
- After payment our app can't refund (`403`, debug ids `f729042a2343e`, `f912700c665d3`, `ca4c4b2199af4`,
  `f452147396f4e`); refunding on the seller's behalf is a partner feature (https://developer.paypal.com/docs/multiparty/issue-refund/).
  So `POST /api/purchases/{pid}/refund` on a vendor-paid purchase never calls PayPal. It moves `completed → refund_requested`
  (atomic claim), records who asked and why, and the owner console and the agent both see "Refund requested: with SampleBox.
  The vendor refunds through PayPal; AgentTab never holds this money."
- **Reconcile** confirms it: `GET /v2/payments/captures/{id}` (allowed, `f7416798d75bf`) showing `REFUNDED` moves it to
  `refunded`, and the amount stops counting. `PARTIALLY_REFUNDED` becomes `partially_refunded` and the full amount keeps
  counting. **UNSURE:** how to read the refunded amount from the capture (not exercised); until then partial refunds are shown,
  not credited.
- A `PAYMENT.CAPTURE.REFUNDED` webhook, if PayPal delivers it (**UNSURE**), does the same.
- Demo (reviewer decision, Oct 18): no simulated vendor action and no per-vendor REST apps. The demo shows `refund_requested`
  with the explanation; no fake "refunded" state.
- Refund windows and the non-refundable rule for live data stay (`refund_one`).

## (c) Vendor merchant ids from env; no id, no payment
- `VENDOR_SAMPLEBOX_MERCHANT_ID`, `VENDOR_SHOPSUITE_MERCHANT_ID`, `VENDOR_SITESCOPE_MERCHANT_ID` (Render env and the private
  chmod-600 file; never in git, logs or API responses; the UI shows only "PayPal account connected: yes/no").
- A vendor without an id is **never paid**: a new deterministic layer `payee` in `decide()` blocks it ("vendor has no PayPal
  account configured") before the guard (no AI call spent) and before fulfilment, with **no fallback** to AgentTab's account.
  It applies only to vendor-funded agents. Forbidden vendors (gift cards, ad boosting) never get ids and stay blocked by policy.
- `/healthz` adds `vendors_payable: n` (a count only).
- The payee check in (a) step 5 means a wrong env value freezes the agent instead of paying someone else silently.

## (d) The judges' story, and when it may be said
- The sentence: **"AgentTab never touches the money; the vendor is paid by PayPal only after delivery."**
- It appears in README, `docs/video-script.md` and the landing copy **only after** the code and its tests are merged, and only
  where it is true: on the hosted demo, only once `VENDOR_PAYOUTS=1` is on there (reviewer approval). Until then the README keeps
  "a real product would use multiparty".
- Refund wording must stay honest next to it: "refunds are requested from the vendor".
- A docs test enforces it: the sentence (and close variants such as "vendor is paid by PayPal") may appear in README, `docs/` or
  `static/` only if `tests/test_vendor_payouts.py` exists.
- Landing copy belongs to the other worker's branch; this plan hands them the sentence and the test and doesn't edit it.

## (e) Tests (each must fail before the change)
Mock PayPal (the existing mock client, extended to record every request body, header and `PayPal-Request-Id`) unless noted.
1. **One capture order to the vendor:** a below-threshold buy by a vendor-funded agent sends exactly one `POST
   /v2/checkout/orders` with `intent: CAPTURE`, `payee.merchant_id` = the env id, the owner's `vault_id`, `custom_id` = pid,
   amount from cents; no `fund` ledger entry, no authorization call. (Fails today: no payee, tab funding.)
2. **Order of events:** the PayPal call happens only after `fulfill()` returned; the paid amount equals the reservation.
3. **Fulfil fails → zero PayPal calls**, status `failed`, the amount no longer counts.
4. **Decline after delivery → `payment_failed`**, agent frozen, amount not counted; the next buy gets `423` (frozen).
5. **Two parallel buys can't exceed caps:** 10 threads buy for one agent with a slow mock `fulfill`; the counted total never
   exceeds daily cap, vendor budget or envelope; the rest get `403 caps`. Also across two `Ledger` instances on one DB file.
   (Fails today for vendor agents: nothing reserves before fulfilment.)
6. **Double approval:** two concurrent approvals of one pending purchase → one fulfilment, one PayPal call, the other `409`.
7. **Idempotent payment:** the mock completes the order, then times out; the retry reuses `at-{pid}-pay` and the mock returns the
   first order; one `completed` row, one capture id. A reconcile replay of `payment_unknown` does the same.
8. **Request-Id derivation:** deterministic per (pid, action), never random, ≤ 38 chars.
9. **Whole cents:** a sub-cent price is refused; `value` strings round-trip to the same cents.
10. **No id, no payment:** a vendor without `VENDOR_*_MERCHANT_ID` → `403` layer `payee`, zero PayPal calls, zero AI calls,
    zero fulfilment.
11. **Response checks:** wrong payee, wrong amount, two captures, or a non-`COMPLETED` status → `flagged`/`payment_pending`,
    agent frozen where specified, amount still counted.
12. **Above threshold:** no PayPal call at request time; reject → no PayPal call; expiry → no PayPal call; approve → fulfil →
    one capture order. Only the owner (admin token or demo-session owner) can approve; a plain agent key gets `401`.
13. **No card → `402 card_required`,** never a payment to AgentTab.
14. **Refund → `refund_requested`,** no PayPal refund call; owner and agent views say it's with the vendor; a capture `GET`
    showing `REFUNDED` moves it to `refunded` and frees the envelope; `PARTIALLY_REFUNDED` keeps it counted.
15. **Flag off / tab agents unchanged:** with `VENDOR_PAYOUTS` unset, the existing suite passes untouched, including the
    PayPal-hold approval to AgentTab; vendor-only endpoints and fields don't appear.
16. **Top-up endpoints `410`** for vendor agents only.
17. **Webhooks:** `PAYMENT.CAPTURE.COMPLETED/PENDING/DECLINED/REFUNDED` map to the purchase by `custom_id` or capture id,
    idempotently (`webhook_events` table).
18. **Reconcile:** `payment_pending`/`payment_unknown` → replay with the same Request-Id or `GET` the order; a capture whose
    amount, status or payee disagrees with the ledger → freeze.
19. **Claim gate:** the docs test from (d) fails if the sentence appears without `tests/test_vendor_payouts.py`.
20. **Live sandbox test (skips without keys, run locally only, never the hosted site):** one real capture order to a US vendor,
    then `GET` order and capture: `COMPLETED`, payee = vendor, no authorization.
21. **UI e2e:** envelope instead of balance for vendor agents; statuses paid to vendor / payment failed / refund requested with
    the vendor; `enc()` on every vendor-supplied string; design audit as for the other panels.

## (f) Timeline (ready for review Nov 2, feature freeze Nov 5, deadline Nov 12)
Start Oct 19 after the go-ahead. Tests are written first in every step.

| Dates | Step | Days |
|---|---|---|
| Oct 19-20 | Ledger: `reserve_vendor_purchase`, cents, migration, flag, `payee` layer | 2 |
| Oct 21-23 | Capture-only buy + approval flows, derived Request-Id, response checks, decline/unknown paths, concurrency tests | 3 |
| Oct 26 | Refund requests + reconcile (capture `GET`, replay) | 1 |
| Oct 27 | Webhooks | 0.5 |
| Oct 27-29 | UI (envelope, statuses), MCP descriptions, e2e | 2 |
| Oct 30 | Live sandbox test (local), docs, claim gate | 1 |
| Nov 2 | **Ready for review** (full suite with keys stripped, screenshots, push) | 0.5 |
| Nov 3-5 | Review rounds, fixes; merge/deploy only on the reviewer's exact-sha approval | slack |

## Webhooks and reconcile changes
- **Webhooks** (`handle_webhook`, `app/main.py`): match `PAYMENT.CAPTURE.COMPLETED/PENDING/DECLINED/REFUNDED` to vendor
  purchases by capture id or `custom_id` = pid (today `custom_id` is the agent id, `paypal.py`). `REFUNDED` on a vendor
  purchase → `refunded`, with no ledger credit (there is no tab). `CHECKOUT.ORDER.APPROVED` and authorization events aren't
  used by vendor agents (no checkout page, no holds). Legacy handling stays for tab agents. Nothing depends on delivery
  (**UNSURE**); reconcile is the source of truth.
- **Reconcile** (`/api/agents/{aid}/reconcile`): for vendor agents it walks vendor purchases instead of `fund` entries:
  `payment_pending`/`payment_unknown` → replay create-order with the same Request-Id (returns the first result) or `GET` the
  order; `completed`/`refund_requested`/`partially_refunded` in the last 30 days → `GET` the capture: amount, status, payee.
  Mismatch freezes the agent, as today. `reserved`/`completing` rows older than 10 minutes (a crash mid-flow) → `GET` by
  `custom_id` isn't available, so the replay with the same Request-Id decides; if PayPal has no such order, the row becomes
  `failed` (but the vendor may have delivered: flagged for the owner).

## Risks
1. **Vendor delivers unpaid on a decline:** the card isn't checked before delivery. Mitigations: the card is verified when it is
   saved (Vault setup), the agent freezes on the first `payment_failed`, and our vendors are sandbox/instant. Decline handling
   is **UNSURE** in the real sandbox (only mocked).
2. **Refunds depend on the vendor:** the product only requests them. Real fix: multiparty partner onboarding
   (https://developer.paypal.com/docs/multiparty/issue-refund/,
   https://developer.paypal.com/docs/multiparty/seller-onboarding/build-onboarding/), after the hackathon.
3. **Country/capability:** Cyprus vendor accounts were refused (`PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING`, `f259063a191ee`); US
   accounts work. **UNSURE** what decides it for real vendors.
4. **Webhook delivery** for vendor-payee captures is unknown; reconcile polling covers it.
5. **Disputes and seller protection land on the vendor:** the US re-spike capture showed `seller_protection: NOT_ELIGIBLE`
   (`ca4c4b1f5e9d9`); AgentTab has no dispute role.
6. **Production terms:** paying third parties from our app is documented for direct merchants; whether that is acceptable at
   scale without the partner program is **UNSURE**. The README keeps naming multiparty as the production route.
7. **Vault flakiness:** creating a vault token was refused once (`403`, `f886754a2499e`) and worked on a re-run. A vault failure
   means no saved card and no purchase (fail closed); the save-card step retries once with a fresh Request-Id.
8. **The live demo belongs to the judges:** the flag stays off on the hosted site until the reviewer approves; any deploy is
   checked only with `/`, `/healthz` and MCP 401/initialize.
9. **Scope vs dates:** about 10 working days before Nov 2; review rounds use the Nov 3-5 slack.
10. **Copy drift:** landing copy is on another worker's branch; the claim gate (d) catches a premature claim.

## Decisions needed
1. Go-ahead to start the code on feature/vendor-payouts (behind `VENDOR_PAYOUTS=1`, off).
2. (Decided Oct 18: Option 1 capture-only; no simulated vendor action; demo refunds show `refund_requested`, reconcile
   detects `REFUNDED`; the PayPal-hold approval to AgentTab stays for agents without vendor payouts.)
