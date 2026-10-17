# Judging AgentTab in one minute

Hosted demo: **https://agenttab.onrender.com** (Render free tier: the first request after idle can take ~50 s to wake up).
Nothing to install, no account, no real money: every payment is in the **PayPal sandbox**.

## The 1-minute path

| Time | Do this | What you should see |
|---|---|---|
| 0:00 | Open the page. | The **Live Gemini** badge in the top bar (green = the AI guard is configured and reachable). |
| 0:10 | Click **Run the attack Gauntlet live**. | 8 attacks (one per category) + 2 legitimate buys go through the production `decide()` with live Gemini. Each row shows the verdict, the layer that decided, the model and the latency. Dry run: no charge, no PayPal call. |
| 0:25 | Click **Open live demo** → **1. Start demo session**. | A fresh agent funded with **$1.00 through a real PayPal sandbox capture** (test card). Balance and vendor budgets appear. |
| 0:35 | Click **2** → **6** in order. | 2 mug sample bought from the tab · 3 competitor research bought (a real HTTP fetch) · 4 SaaS seat over $0.30 → **held for approval** (202) · 5 gift card **blocked** (vendor not allowed) · 6 instruction injected into the purpose **blocked by Gemini**. |
| 0:50 | In **Purchases**, click **Approve (sandbox card)** on the SaaS seat, then **Refund** on the mug. | The seat is paid by its own PayPal sandbox capture; the mug credit returns to the tab. The **Schedule** (Bryntum) shows budgets, the refund windows and the ⚡ PayPal events. |
| 1:00 | Optional: **Demo clock: jump 6 days ahead**. | Refund windows near their end; the scheduler *proposes* refunds and waits for a human to confirm. |

## If you have five more minutes

- **Scorecard:** the proof section shows the committed 67-case eval ([`eval/results.json`](../eval/results.json)):
  no guard 0/47 attacks stopped, rules + keywords 37/47, rules + Gemini 47/47, with 20/20 legitimate buys handled correctly.
- **Code you may want to read:**
  - The decision pipeline: [`app/procurement.py`](../app/procurement.py) `decide()`, guard prompt in [`app/guard.py`](../app/guard.py).
  - Money: [`app/ledger.py`](../app/ledger.py) `commit_tab_purchase()` (re-checks limits in one `BEGIN IMMEDIATE` transaction).
  - PayPal: [`app/paypal.py`](../app/paypal.py) (Orders v2, Payments v2 authorize/capture/void and refunds, Vault v3,
    negative testing, webhook signature verification). Holds, the freeze and reconciliation are in
    [`app/main.py`](../app/main.py) `complete_authorized()` and `reconcile()`.
- **Threat model mapped to tests:** [`docs/threat-model.md`](threat-model.md).
- **Run it locally, offline** (mock PayPal, no keys): `pip install -r requirements.txt -r requirements-dev.txt && pytest -q`.

## What is real and what is simulated

| Part | Status | Details |
|---|---|---|
| PayPal Orders v2 (create, approve, capture) | **Real, sandbox** | `api-m.sandbox.paypal.com`; owner top-ups and approval links open real sandbox checkout. |
| Demo funding and "Approve (sandbox card)" | **Real, sandbox** | Orders v2 with PayPal's public sandbox test card, so judges don't have to log in to a sandbox buyer account. Refused against live PayPal. |
| Authorize → capture / void | **Real, sandbox** | Approvals place a hold; captured after delivery, voided if delivery fails. |
| Forced declines | **Real, sandbox** | PayPal's `PayPal-Mock-Response` negative testing, via the owner-only `X-Simulate-Decline` header. |
| Reconciliation + freeze | **Real, sandbox** | Ledger vs `GET /v2/payments/captures/{id}`; mismatches freeze the agent. |
| Saved card (Vault v3) + auto top-up | **Real, sandbox** | PayPal's generic sandbox test Visa saved in Vault; merchant-initiated charges within an owner cap. |
| Refunds | **Real, sandbox** | Payments v2 refund of the specific capture (tab refunds, approval-funded purchases). Tab-funded purchase refunds are ledger credits back to the tab. |
| Webhooks | **Real, sandbox** | `POST /paypal/webhook`, every event checked with PayPal's `verify-webhook-signature`; unverified events are rejected. |
| AI spend guard | **Real** | Live Gemini (`gemini-3.5-flash-lite`, then a second model). If Gemini is unavailable the purchase is held for a human. |
| Gauntlet | **Real guard, no money** | Live Gemini on sampled eval cases; nothing is charged and PayPal is not called. |
| WebIntel Data purchases (tech stack, Markdown, links…) | **Real** | Real HTTP fetches of public sites through the SSRF-guarded fetcher. |
| SampleBox Wholesale, ShopSuite, GiftCards Now | **Simulated vendors** | Fulfilment returns a `SIM-…` vendor order. The money side (tab, approvals, refunds) is real sandbox PayPal. |
| Money | **Sandbox only** | No real funds anywhere. |
| Demo clock | **Simulated time** | Shifts only your demo agent's timestamps back so deadlines approach. |
| Mock PayPal (`PAYPAL_MODE=mock`) | **Offline dev/tests only** | Refuses to start with a public URL or with demo mode; never used on the hosted demo. |
| Storage | **Real, ephemeral** | SQLite on Render's free disk: a restart resets demo data. |
| Bryntum Scheduler | **Real, trial build** | Official 45-day trial package (watermark); evaluation-only license. |
