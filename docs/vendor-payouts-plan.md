# Plan: pay vendors directly (AgentTab holds no money)

Status: **research and plan only, no code on this branch.** Every claim links to a PayPal developer page.
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

## Proposed design (Option A)
1. **Vendor registry:** add `payee_merchant_id` per vendor. The values come from env (`VENDOR_PAYEE_SITESCOPE_DATA`, …) and are
   never committed. Mock mode uses fake 13-character ids.
2. **Envelope instead of balance:** the agent's spendable amount is `envelope_budget − (held + captured)` in whole cents.
   `decide()` and the reservation stay in one SQLite transaction (the existing atomic hold).
3. **Charge:** the existing `charge_vault` / `authorize_vault` add `purchase_units[0].payee.merchant_id`, and the
   `PayPal-Request-Id` stays derived from the action (`vault-{op_id}`).
4. **Approval path:** an approval-required purchase authorizes (hold) toward the vendor payee and captures on approval. If PayPal
   rejects AUTHORIZE with a payee (UNSURE above), fall back to "no PayPal call until approved, then a CAPTURE order".
5. **Top-up removed** from the demo (or kept as "fund the envelope = raise the limit", with no money moving). This is a README change.
6. **Failure handling:** a PayPal error releases the reservation, and the purchase becomes `failed` (never auto-approved).
7. **Tests (failing first):** the body carries the correct payee per vendor; an unknown vendor has no payee and blocks; the envelope can't go
   negative under parallel buys; a refund restores the envelope; mock mode never calls the network.

## Sandbox spike (needs Yarden's go-ahead, about 3 calls, sandbox only, no live site)
1. Create one order with the vault test card and `payee.merchant_id` = vendor sandbox account → expect `COMPLETED`.
2. The same with `intent: AUTHORIZE`, then capture.
3. Refund the capture from step 1.
Record the results here and remove the UNSURE markers that they resolve.

## Access needed from Yarden
- Three sandbox **business** accounts (Developer Dashboard → Testing tools → Sandbox accounts), one per vendor
  (SiteScope Data, SampleBox, ShopSuite), and their **merchant ids** (Account ID). Put them in Render and `.secrets` as env vars, not in git.
- Confirm the sandbox REST app still has **Vault** enabled (it does today, because the vault tests run against sandbox).
- Approval to run the 3-call sandbox spike above locally.
- Optional, only if we choose B: request Payouts access on the sandbox/live account.
- Optional, only if we choose C: the multiparty contact form (https://developer.paypal.com/docs/multiparty/).
