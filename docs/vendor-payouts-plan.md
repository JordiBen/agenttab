# Plan: pay vendors directly (AgentTab holds no money)

Status: **research and plan only, no code on this branch.** Option A is approved in principle by the reviewer (2026-10-17). Every claim links to a PayPal developer page.
Anything not yet exercised in our own sandbox is marked **UNSURE**.

**Update, Oct 18 2026 (US re-spike, see the section of that name):** with US sandbox vendor accounts, Option A's payment works:
a vault card paid to each vendor was authorized and captured by our app (HTTP 201, debug ids below), and PayPal took its fee
from the vendor's side. **Our app could not refund those captures** (`403 NOT_AUTHORIZED / PERMISSION_DENIED` for all three).
The PayPal-wallet vault variant can't be set up headless: it needs a one-time buyer approval in a browser.

## Goal
An approved purchase is paid by PayPal **straight to the vendor**, and each vendor is a sandbox **business** account.
AgentTab never holds a balance. A "tab" turns into a **budget envelope**: a set of limits (budget, per-purchase max, daily cap,
approval threshold) enforced by the same `procurement.decide()` pipeline, and nothing more.

## What we have today (from the code on main)
- `app/paypal.py` already saves the sandbox test card in the Vault (`/v3/vault/setup-tokens` then `/v3/vault/payment-tokens`)
  and charges or authorizes it with Orders v2 (`payment_source.card.vault_id`). Right now the payee is the API caller,
  so the money lands in AgentTab's own sandbox business account.

## Options compared

| | A. Orders v2 + `purchase_units[].payee` + Vault card | B. Payouts | C. Multiparty (Complete Payments Platform) |
|---|---|---|---|
| Money path | Owner's saved card → vendor directly | AgentTab balance → vendor | Buyer → seller, platform acts on the seller's behalf |
| Does AgentTab hold money? | **No** | **Yes** (payouts are funded from our balance), which breaks the goal | No |
| Works in sandbox without partner onboarding? | Likely yes (see the UNSURE notes) | Yes, sandbox testing allowed before approval; live needs approval | Sandbox works with a standard developer account, but each seller must go through the onboarding flow |
| Code change | Small: add `payee` to the existing vault-charge body | New API (`/v1/payments/payouts`), and the top-up stays | Large: Partner Referrals, seller onboarding, auth-assertion headers |
| Refunds | `/v2/payments/captures/{id}/refund`: **refused for the API caller** on a capture paid to another payee (`403 PERMISSION_DENIED`, US re-spike); the vendor or an approved partner must refund | Only an unclaimed item can be cancelled; there is no refund of a claimed payout | Supported for the partner, with permissions |

### A. Orders v2 with `payee` and a Vault card (recommended)
- "By default, money is paid to the application owner… To specify a different payee… include the `email_address` or `merchant_id`"
  (https://developer.paypal.com/docs/checkout/standard/customize/pay-another-account/).
  The page is labelled **DIRECT MERCHANT**, so no partner onboarding is involved.
- `payee.merchant_id` is the 13-character PayPal account ID; `email_address` is the alternative
  (https://developer.paypal.com/api/orders/v2/definitions/payee).
- Saved cards are charged with Orders v2 and a vault id. In sandbox: Developer Dashboard → app → App Feature Options → Accept
  payments → Advanced options → Vault (https://developer.paypal.com/docs/checkout/save-payment-methods/during-purchase/orders-api/cards/).
  Live card processing needs Expanded Checkout approval (same page).
- **Spike result (Oct 17 2026, sandbox, see "Sandbox spike" below): a vault card charged to a different payee was REJECTED**
  with `422 PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING` ("Payee account is not setup to be able to process card payments"),
  PayPal debug id `f259063a191ee`. So it isn't the vault ownership that blocks it, it's the *payee's* card-processing capability.
- **US re-spike (Oct 18 2026):** with US sandbox vendor accounts the same call **succeeds** for all three vendors:
  `intent: AUTHORIZE` + payee, then our app's capture, both `201`. Our app's refund of those captures is refused (`403`).
  See "US re-spike" below.

### B. Payouts
- Requires a business account, "Access to PayPal Payouts" (you must request it), and "Sufficient funds in your PayPal business
  account". You can test in sandbox while the request is pending (https://developer.paypal.com/docs/payouts/standard/).
- API: `POST /v1/payments/payouts` with `sender_batch_header.sender_batch_id` for dedup, `PayPal-Request-Id` for idempotency,
  and async batch status `PENDING`/`SUCCESS`/... (https://developer.paypal.com/docs/payouts/standard/integrate-api/,
  https://developer.paypal.com/docs/api/payments.payouts-batch/v1/).
- Verdict: it contradicts "AgentTab holds no money", because we would collect funds first and then pay out. It is also asynchronous, so
  "check + charge in one atomic step" becomes harder. Not recommended.

### C. Multiparty
- "Test the integration in the PayPal sandbox with a standard developer account. Apply for live credentials and go live after your
  platform or marketplace has been approved" (https://developer.paypal.com/docs/multiparty/).
- Sellers must be onboarded (link → login → grant permission) before they can be paid in the recommended flow
  (https://developer.paypal.com/docs/multiparty/seller-onboarding/). Partner fees are "not supported for first-party integrations"
  (https://developer.paypal.com/docs/multiparty/).
- Verdict: correct for a real marketplace, but too much for the hackathon. Keep it as the "production path" in the README.
  **UNSURE:** whether sandbox seller onboarding works end to end without a PayPal representative.

## Proposed design (Option A, approved in principle)
1. **Vendor registry:** add `payee_merchant_id` per vendor. The values come from env (`VENDOR_PAYEE_SITESCOPE_DATA`, …) and are
   never committed. Mock mode uses fake 13-character ids.
2. **The envelope creates the order up front with a hold.** For every purchase that passes `decide()`, AgentTab creates the
   Orders v2 order immediately with `intent: AUTHORIZE`, the owner's vaulted card, and `payee.merchant_id` = the vendor.
   - The authorization is the hold, and **held amounts count toward every cap**: per-purchase max, daily cap, vendor 30-day
     budget and the envelope budget all use `held + captured`, in whole cents.
   - `decide()`, the cap check and the reservation row are written in one SQLite transaction. The PayPal authorize call is
     keyed by `PayPal-Request-Id = vaultauth-{op_id}` (derived from the action), so a retry can never create a second hold.
   - If the authorize fails, the reservation is released in the same code path and the purchase becomes `failed`
     (never auto-approved, never retried silently).
3. **Auto-approved purchases** (under the $60 threshold) are captured as soon as the vendor delivers. **Approval-required
   purchases** stay on hold until the owner approves (capture) or rejects (void), and the hold counts toward the caps while it waits.
   An authorization that expires releases its reservation.
   - **BLOCKED as designed (spike, Oct 17 2026):** the order with a vault card and a vendor `payee` is refused with
     `PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING` before AUTHORIZE vs CAPTURE even matters. Until the vendor accounts can take cards
     (see "What would unblock Option A"), the only working variant is the fallback: a ledger-only reservation (no PayPal call
     until approval, then an order). That still counts toward the caps, but no money is actually held at PayPal.
4. **No top-up into AgentTab:** "funding the tab" becomes "setting the envelope limit"; no money moves to AgentTab. This is a README change.
5. **Tests (failing first):**
   - the body carries the right payee per vendor; an unknown vendor has no payee and is blocked;
   - held amounts count toward the daily, per-purchase and vendor caps;
   - parallel buys can't exceed the envelope;
   - a void or a refund restores the envelope;
   - a failed authorize releases the reservation;
   - the Request-Id is stable across retries;
   - mock mode never calls the network.
6. **README:** state plainly that this is a sandbox demo of direct-to-vendor payment, and that **a real product would use
   PayPal multiparty (Complete Payments Platform) with vendor onboarding** (https://developer.paypal.com/docs/multiparty/,
   https://developer.paypal.com/docs/multiparty/seller-onboarding/).

## Sandbox spike: results (run Oct 17 2026, 23:58 Asia/Jerusalem (UTC+3); local script, sandbox host only)
Scope: approved 3-call spike, run from the box against `https://api-m.sandbox.paypal.com` only, never against the hosted app.
Merchant ids and e-mails come from an untracked env file (chmod 600, outside every git repo) and are shown here as placeholders.
Secrets redacted: bearer token, client id/secret, vault token id. Full redacted log is kept privately (not in this repo).

| # | Request | HTTP | PayPal debug id | Result |
|---|---|---|---|---|
| prereq | `POST /v3/vault/setup-tokens` (standard sandbox test Visa) | 201 | `f81464249ae51` | setup token |
| prereq | `POST /v3/vault/payment-tokens` from the setup token | 201 | `ca4c238f269b1` | vault token (VISA 1111) |
| **1** | `POST /v2/checkout/orders` intent=AUTHORIZE, vault card, `payee.merchant_id` = SampleBox | **422** | `f259063a191ee` | `PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING` |
| 2 | capture the authorization | — | — | **not run**: no authorization exists |
| 3 | refund the capture | — | — | **not run**: no capture exists |
| cleanup | `DELETE /v3/vault/payment-tokens/<vault token>` | 204 | `ca4c238ff2b22` | test token removed |

Call 1, exact request (headers: `Authorization: Bearer <REDACTED>`, `PayPal-Request-Id: spike-vaultauth-e9a34769`,
`Prefer: return=representation`):
```json
{"intent": "AUTHORIZE",
 "purchase_units": [{"reference_id": "spike", "custom_id": "spike-e9a34769",
   "description": "AgentTab Option A spike: SampleBox mug sample",
   "amount": {"currency_code": "USD", "value": "10.00"},
   "payee": {"merchant_id": "<SAMPLEBOX_MERCHANT_ID>"}}],
 "payment_source": {"card": {"vault_id": "<REDACTED>",
   "stored_credential": {"payment_initiator": "MERCHANT", "payment_type": "UNSCHEDULED", "usage": "SUBSEQUENT"}}}}
```
Call 1, exact response (`422`, `paypal-debug-id: f259063a191ee`):
```json
{"name": "UNPROCESSABLE_ENTITY",
 "details": [{"issue": "PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING",
   "description": "Payee account is not setup to be able to process card payments. Please contact PayPal customer support."}],
 "message": "The requested action could not be performed, semantically incorrect, or failed business validation.",
 "debug_id": "f259063a191ee",
 "links": [{"href": "https://developer.paypal.com/api/rest/reference/orders/v2/errors/#PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING",
   "rel": "information_link", "method": "GET"}]}
```
No order id was returned, so nothing was authorized, captured or held. The same vault-card flow with no `payee` (money to
AgentTab's own sandbox account) works today: that is the existing auto top-up, covered by the sandbox tests.

### Answers
1. **Can a Vault card be charged to an order whose `payee.merchant_id` is a different sandbox business account?**
   **No, not with these accounts.** PayPal rejects it at order creation with `PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING`. PayPal's
   own explanation: "the payee's account is not set up to receive card payments… may also show up if the payee has not been fully
   onboarded to accept payments with PayPal Complete Payments"
   (https://developer.paypal.com/api/rest/troubleshooting/rest_unprocessable_entity). The error list puts it under
   Orders v2 create/capture (https://developer.paypal.com/api/rest/reference/orders/v2/errors/).
   **UNSURE:** whether enabling Advanced Credit and Debit Card Payments on the vendor sandbox accounts would make the same call
   succeed. PayPal says accounts made through sandbox.paypal.com may need "the sandbox onboarding steps"
   (https://developer.paypal.com/platforms/checkout/advanced/integrate). We tested one vendor (SampleBox); the other two were
   created the same way, so we expect the same result, but that is **UNSURE** (not called, to stay within the 3 calls).
2. **Does `intent=AUTHORIZE` work with a payee, and can our app capture it?** **UNSURE (inconclusive).** The request was
   refused for the payee's card capability before any hold existed, so this was never exercised. The docs list
   `AUTH_CAPTURE_NOT_ENABLED` ("make sure that the recipient of the funds is a verified business account") as a separate
   possible error (same errors page); our vendor accounts are Verified, which is necessary but not proven sufficient.
3. **Who can refund the capture, and who pays the fee (`seller_receivable_breakdown`)?** **UNSURE (inconclusive):** no capture,
   so no `seller_receivable_breakdown` to read. From the docs only: refunding a payment that landed in *another* merchant's
   account is a third-party call that needs the `PayPal-Auth-Assertion` header and "explicit consent from each merchant you
   represent" (https://developer.paypal.com/api/make-api-requests, https://developer.paypal.com/api/payments/v2/captures-refund).
   Without that permission only the vendor itself could refund. The fee is expected to be deducted from the payee's
   (vendor's) receivable, because that account receives the funds, but that is **UNSURE** until a real capture shows its
   `seller_receivable_breakdown.paypal_fee`.

### What this means for the plan
- Option A as written (a vault card paid straight to the vendor's own account) **does not work with ordinary sandbox
  business accounts.** Any vendor would need card processing enabled, which in a real product is exactly the
  multiparty / Complete Payments onboarding (Option C). That is stronger evidence for the README line we already have.
- Refunds would need per-vendor consent (third-party permission) in any case, so the envelope can't promise "AgentTab refunds
  unused items" for vendor-paid purchases without onboarding.
- **Nothing in the app changes yet.** The current auto top-up flow (money to AgentTab's own sandbox account, the ledger enforces the
  caps) is unaffected.

### What would unblock Option A (each needs a decision; none run)
1. **Enable cards on the vendor sandbox accounts** (Developer Dashboard → the vendor's sandbox business account / its app →
   Advanced Credit and Debit Card Payments, or the sandbox onboarding steps), then re-run the same 3-call spike unchanged
   (`notes/scripts/vendor_spike.py`, private). Cheapest test; it answers Q2 and Q3 if call 1 then succeeds. Needs the project owner in
   the dashboard. **UNSURE** whether the dashboard allows it for these accounts.
2. **Payee + PayPal wallet instead of a card** (`payment_source.paypal`, buyer approves each order): avoids the payee
   card-capability check (**UNSURE**), but every purchase needs the owner to click through PayPal, so it isn't autonomous.
   A new spike, not covered by the current approval.
3. **Option C (multiparty, seller onboarding)**: the production answer; sandbox works with a standard developer account
   (https://developer.paypal.com/docs/multiparty/), but it means a new integration. Not for the hackathon.
(Superseded by the US re-spike below: new US vendor accounts were used instead of changing the Cyprus ones.)
Recommendation at the time: try 1 first (dashboard only, no code). If it can't be enabled, keep the shipped design (ledger caps, money to
AgentTab's sandbox account) and leave A/C as the documented production path.

## US re-spike: results (run Oct 18 2026, 07:10 Asia/Jerusalem (UTC+3); local script, sandbox host only)
Scope: approved by the reviewer; run from the box against `https://api-m.sandbox.paypal.com` only (hard-coded), never against
the hosted app, with our app's sandbox REST credentials. Vendors are three **new US** sandbox business accounts (Verified,
country US; the dashboard doesn't say whether card processing is enabled). Their merchant ids live only in the untracked
chmod-600 env file (`US_*` variables, next to the old Cyprus ones) and appear here as placeholders. Script:
`notes/scripts/vendor_spike_us.py` (private, an extended copy of the first spike). Full redacted log kept privately.
No app code changed.

### 1. Option A: Orders v2 + `payee.merchant_id` + Vault card, the same 3 calls, per US vendor

| # | Request | HTTP | PayPal debug id | Result |
|---|---|---|---|---|
| prereq | `POST /v3/vault/setup-tokens` (standard sandbox test Visa) | 201 | `ca4c4b1d78ead` | setup token, `APPROVED` |
| prereq | `POST /v3/vault/payment-tokens` from the setup token | 201 | `ca4c4b1d99db1` | vault token (VISA 1111) |
| **1** | `POST /v2/checkout/orders` intent=AUTHORIZE, vault card, payee = `<US_SAMPLEBOX_MERCHANT_ID>` | **201** | `f9424082029fe` | order `COMPLETED`, authorization `CREATED` |
| **2** | `POST /v2/payments/authorizations/{id}/capture` (our app) | **201** | `f886941b5bd89` | capture `COMPLETED`, $10.00 |
| read | `GET /v2/payments/captures/{id}` | 200 | `ca4c4b1f5e9d9` | payee = SampleBox; fee $0.75, net $9.25; `disbursement_mode: INSTANT` |
| **3** | `POST /v2/payments/captures/{id}/refund` (our app) | **403** | `f912700c665d3` | `NOT_AUTHORIZED` / `PERMISSION_DENIED` |
| **1** | same order, payee = `<US_SHOPSUITE_MERCHANT_ID>` | **201** | `ca4c4b1fb2da2` | order `COMPLETED`, authorization `CREATED` |
| **2** | capture (our app) | **201** | `f880477cbe985` | capture `COMPLETED` |
| read | `GET` capture | 200 | `f211706d4af5b` | payee = ShopSuite |
| **3** | refund (our app) | **403** | `ca4c4b2199af4` | `NOT_AUTHORIZED` / `PERMISSION_DENIED` |
| **1** | same order, payee = `<US_SITESCOPE_MERCHANT_ID>` | **201** | `f293760fbbe46` | order `COMPLETED`, authorization `CREATED` |
| **2** | capture (our app) | **201** | `f328695fa0ed5` | capture `COMPLETED` |
| read | `GET` capture | 200 | `f45214718fa74` | payee = SiteScope Data |
| **3** | refund (our app) | **403** | `f452147396f4e` | `NOT_AUTHORIZED` / `PERMISSION_DENIED` |
| cleanup | `DELETE /v3/vault/payment-tokens/<vault token>` | 204 | `f218553a2c335` | test token removed |

Order body: identical to the first spike's call 1 above (USD 10.00, `stored_credential` MERCHANT/UNSCHEDULED/SUBSEQUENT),
with the US merchant id as `payee.merchant_id`. Refund body: `{"amount": {"currency_code": "USD", "value": "10.00"}}`.
Refund response (`403`, debug id `f912700c665d3`; the other two are the same):
```json
{"name": "NOT_AUTHORIZED", "message": "Authorization failed due to insufficient permissions.",
 "details": [{"issue": "PERMISSION_DENIED", "field": "capture_id", "location": "path",
   "description": "You do not have permission to access or perform operations on this resource."}],
 "links": [{"href": "https://developer.paypal.com/docs/api/payments/v2/#error-PERMISSION_DENIED", "rel": "information_link"}]}
```

**Answers (US accounts):**
1. **Can a Vault card be charged to another sandbox business account as payee?** **Yes** for all three US accounts (debug ids
   `f9424082029fe`, `ca4c4b1fb2da2`, `f293760fbbe46`). The same request against the Cyprus accounts was refused with
   `PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING` (`f259063a191ee`). **UNSURE** why: the visible difference is the country (US vs CY);
   that US sandbox business accounts come with card processing enabled is our inference, not something PayPal's pages state.
   Pay-another-account is a documented DIRECT MERCHANT feature
   (https://developer.paypal.com/docs/checkout/standard/customize/pay-another-account/).
2. **Does `intent=AUTHORIZE` work with a payee, and can our app capture it?** **Yes.** The authorization is placed on the
   vendor's account and our app (the API caller that created the order) captured it, `201` three times (`f886941b5bd89`,
   `f880477cbe985`, `f328695fa0ed5`). So the hold-first envelope works: hold at approval, capture on delivery. Authorization
   expiry reported by PayPal: 29 days after creation (`expiration_time` in the order response, debug id `f9424082029fe`).
3. **Who pays the fee?** **The vendor (payee).** `seller_receivable_breakdown` on the SampleBox capture: gross $10.00,
   `paypal_fee` $0.75, net $9.25 (debug id `ca4c4b1f5e9d9`). `seller_protection` is `NOT_ELIGIBLE` and the card type reads
   `UNKNOWN` (same response).
4. **Who can refund?** **Not our app.** All three refunds were refused with `403 PERMISSION_DENIED` (`f912700c665d3`,
   `ca4c4b2199af4`, `f452147396f4e`), even though the capture response carries a `refund` link. PayPal documents refunding a
   seller's capture on their behalf as a partner feature: "Be an approved partner. Onboard sellers before you begin", with the
   `PayPal-Auth-Assertion` and `PayPal-Partner-Attribution-Id` headers
   (https://developer.paypal.com/docs/multiparty/issue-refund/). Without that, only the vendor can refund from its own
   account. **UNSURE:** whether a non-partner "third-party permission" grant from each vendor would also work for REST refunds
   (not tried; the only REST path documented is the partner one above).

**Left over in sandbox:** three $10.00 sandbox captures sit in the three US vendor accounts, not refunded (sandbox money only;
capture ids in the private log). A vendor can refund them from its own sandbox account at https://www.sandbox.paypal.com
(Activity → the payment → Refund). **UNSURE:** exact menu labels in the current sandbox UI (not opened).

### 2. PayPal-wallet Vault variant (`payment_source.paypal.vault_id` + `payee`)
- **No wallet vault token exists.** The app only ever vaults the sandbox test *card* (`app/paypal.py`, `authorize_vault`); no
  PayPal-wallet token has been created on this sandbox app.
- **It can't be created headless.** PayPal's flow: `POST /v3/vault/setup-tokens` with `payment_source.paypal` "Returns a
  `PAYER_ACTION_REQUIRED` status… Redirects to the `approve` URL"; "The payer must authenticate and approve the creation of a
  billing agreement"; the setup token "expires after 3 days" by default
  (https://developer.paypal.com/docs/checkout/save-payment-methods/purchase-later/payment-tokens-api/paypal/). Our run:

| Request | HTTP | PayPal debug id | Result |
|---|---|---|---|
| `POST /v3/vault/setup-tokens` with `payment_source.paypal` (usage_type MERCHANT, customer_type CONSUMER, return/cancel URLs on example.com) | 201 | `ca4c4b2467bf7` | status `PAYER_ACTION_REQUIRED`, `approve` link to `https://www.sandbox.paypal.com/agreements/approve` |
| `POST /v3/vault/payment-tokens` from that setup token, without approval (headless attempt) | **422** | `f220813879512` | `UNPROCESSABLE_ENTITY` / `PAYER_ACTION_REQUIRED`: "Please make sure setup up token is approved." |

- **Stopped here, as agreed.** The orders with `payment_source.paypal.vault_id` + payee were not attempted.
- **The one browser step needed** (sandbox only, before the setup token expires: 3 days after creation, about Oct 21 2026
  07:10 Asia/Jerusalem by the docs' default; **UNSURE** exact expiry, PayPal didn't return one):
  1. Open the private approve link (kept in `notes/spike/wallet-approve-PRIVATE.txt`, chmod 600, not in this repo) in a browser.
  2. Log in with a sandbox **Personal** (buyer) account: Developer Dashboard → Testing Tools → Sandbox Accounts → the
     `…@personal.example.com` account → View/Edit account for its password. Not a vendor account, not a real PayPal login.
  3. Click the agree/continue button to save PayPal for "AgentTab sandbox". The browser then lands on
     `https://example.com/agenttab-spike/return…`, which is a placeholder page. That's expected; nothing needs to load there.
  4. Tell the agent; it runs `vendor_spike_us.py wallet-finish <setup token id>`: payment token, then for each US vendor
     order(AUTHORIZE, `payment_source.paypal.vault_id`, payee) → capture → refund, and records status and debug id per call.
- **Expected but UNSURE:** even if the wallet orders go through, the refund will most likely be refused the same way
  (`PERMISSION_DENIED`), because the refusal is about who owns the capture (the payee), not the payment source. And live use
  of saved PayPal wallets needs PayPal's approval: "You must be approved and have your account configured for billing
  agreements to set up a reference transaction" (same PayPal page).

### 3. Option 3: multiparty / partner onboarding (the production route)
Option A now *pays* vendors, but it fails one of our rules (refunds, which the envelope promises for undelivered items) unless
the vendor refunds by hand. The documented way to do both on the vendor's behalf is multiparty:
- **Onboard each vendor** with the Partner Referrals API (`POST /v2/customer/partner-referrals`), asking for the `PAYMENT` and
  `REFUND` features; the vendor completes a PayPal sign-up link; requires being "an approved partner"
  (https://developer.paypal.com/docs/multiparty/seller-onboarding/build-onboarding/).
- **Refund on the vendor's behalf** with `PayPal-Auth-Assertion` (JWT naming our client id and the vendor's payer id) plus
  `PayPal-Partner-Attribution-Id` on `/v2/payments/captures/{id}/refund`
  (https://developer.paypal.com/docs/multiparty/issue-refund/).
- Overview and partner contact form: https://developer.paypal.com/docs/multiparty/. **UNSURE:** whether sandbox partner
  onboarding works end to end before partner approval (not tried).
- For the hackathon: not built. It is a new integration (onboarding flow, per-vendor consent, assertion headers).

### What this means now
- **Pay-to-vendor works in sandbox (US accounts):** hold at approval, our app captures on delivery, the vendor receives the
  money and pays the fee. **Refunds do not work from our app.**
- **Decision needed before any app code (none written):**
  (a) ship pay-to-vendor for sandbox purchases with refunds marked "vendor refunds" (the owner can't refund from AgentTab
  after capture; before capture, the hold can still be voided, **UNSURE**: void of a payee authorization by our app was not
  tested), or
  (b) keep the shipped design (money to AgentTab's sandbox account, refunds work) and document Option A's result plus
  Option 3 as the production route.
- Either way the README keeps saying that a real product would use multiparty with vendor onboarding.

## Access needed from the project owner
- Three sandbox **business** accounts (Developer Dashboard → Testing tools → Sandbox accounts), one per vendor
  (SiteScope Data, SampleBox, ShopSuite), and their **merchant ids** (Account ID). Put them in Render and `.secrets` as env vars, not in git.
- Confirm the sandbox REST app still has **Vault** enabled (it does today, because the vault tests run against sandbox).
- (Done: the 3 sandbox vendor accounts exist; merchant ids live in an untracked chmod-600 env file. The 3-call spike ran on Oct 17 2026; results above.)
- (Done Oct 18 2026: 3 new US sandbox vendor accounts, ids in the same env file as `US_*`; re-spike results above.)
- Next, optional: the one browser approval for the PayPal-wallet variant (steps in the US re-spike section), and a decision
  between (a) and (b) under "What this means now".
- Optional, only if we choose B: request Payouts access on the sandbox/live account.
- Optional, only if we choose C: the multiparty contact form (https://developer.paypal.com/docs/multiparty/).
