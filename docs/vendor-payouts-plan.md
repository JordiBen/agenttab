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
- **UNSURE (the key risk):** whether a vault token created by AgentTab's app can be charged with a `payee` that is a *different*
  sandbox business account. The docs say the vault "is accessible only by the billing agreement holder" (same page), and nothing
  we have found forbids a different payee. A 1-call sandbox spike settles it.
- **UNSURE:** whether `intent: AUTHORIZE` combined with `payee` behaves the same way (we use auth/capture for approval holds).
- **UNSURE:** refunds of a capture whose payee is another merchant, and who bears the PayPal fee.

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
   - **UNSURE:** that `intent: AUTHORIZE` with a `payee` and a vault card is accepted in sandbox (spike step 2). If it isn't, the
     fallback is a ledger-only reservation (no PayPal call until approval, then a CAPTURE order). That still counts toward the
     caps, but no money is actually held.
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

## Sandbox spike (approved, local only, sandbox only; runs once Yarden provides the 3 merchant ids; NOT run yet)
1. Create one order with the vault test card and `payee.merchant_id` = vendor sandbox account → expect `COMPLETED`.
2. The same with `intent: AUTHORIZE`, then capture.
3. Refund the capture from step 1.
Record the results here and remove the UNSURE markers that they resolve.

## Access needed from Yarden
- Three sandbox **business** accounts (Developer Dashboard → Testing tools → Sandbox accounts), one per vendor
  (SiteScope Data, SampleBox, ShopSuite), and their **merchant ids** (Account ID). Put them in Render and `.secrets` as env vars, not in git.
- Confirm the sandbox REST app still has **Vault** enabled (it does today, because the vault tests run against sandbox).
- (Done: the reviewer approved the 3-call spike, local only, once the merchant ids exist.)
- Optional, only if we choose B: request Payouts access on the sandbox/live account.
- Optional, only if we choose C: the multiparty contact form (https://developer.paypal.com/docs/multiparty/).
