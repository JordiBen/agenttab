# Plan: pay vendors directly (AgentTab holds no money)

Status: **research and plan only, no code on this branch.** Option A is approved in principle by the reviewer (2026-10-17). Every claim links to a PayPal developer page.
Anything not yet exercised in our own sandbox is marked **UNSURE**.

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
| Refunds | `/v2/payments/captures/{id}/refund`; whether the API caller may refund a capture paid to another payee is **UNSURE** | Only an unclaimed item can be cancelled; there is no refund of a claimed payout | Supported for the partner, with permissions |

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
- **UNSURE (not reached):** `intent: AUTHORIZE` + payee capture, and refunds/fees on such a capture. See the spike section.

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
   (`notes/scripts/vendor_spike.py`, private). Cheapest test; it answers Q2 and Q3 if call 1 then succeeds. Needs Yarden in
   the dashboard. **UNSURE** whether the dashboard allows it for these accounts.
2. **Payee + PayPal wallet instead of a card** (`payment_source.paypal`, buyer approves each order): avoids the payee
   card-capability check (**UNSURE**), but every purchase needs the owner to click through PayPal, so it isn't autonomous.
   A new spike, not covered by the current approval.
3. **Option C (multiparty, seller onboarding)**: the production answer; sandbox works with a standard developer account
   (https://developer.paypal.com/docs/multiparty/), but it means a new integration. Not for the hackathon.
Recommendation: try 1 first (dashboard only, no code). If it can't be enabled, keep the shipped design (ledger caps, money to
AgentTab's sandbox account) and leave A/C as the documented production path.

## Access needed from Yarden
- Three sandbox **business** accounts (Developer Dashboard → Testing tools → Sandbox accounts), one per vendor
  (SiteScope Data, SampleBox, ShopSuite), and their **merchant ids** (Account ID). Put them in Render and `.secrets` as env vars, not in git.
- Confirm the sandbox REST app still has **Vault** enabled (it does today, because the vault tests run against sandbox).
- (Done: the 3 sandbox vendor accounts exist; merchant ids live in an untracked chmod-600 env file. The 3-call spike ran on Oct 17 2026; results above.)
- Next: check whether Advanced Credit and Debit Card Payments can be enabled on the 3 vendor sandbox accounts (unblock option 1).
- Optional, only if we choose B: request Payouts access on the sandbox/live account.
- Optional, only if we choose C: the multiparty contact form (https://developer.paypal.com/docs/multiparty/).
