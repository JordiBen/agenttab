# AgentTab: a purchasing agent for small shops, with a PayPal-funded tab

**Lumen & Loom** is a 6-person online shop selling home textiles and ceramics. Its AI agent orders supplier samples,
buys SaaS seats and pays for competitor research. Giving that agent a company card is reckless; approving every
$0.20 sample by hand defeats the point.

**AgentTab** gives the agent a prepaid tab and rules the owner writes in plain English:

1. **Fund once with PayPal.** The owner tops up the tab through PayPal Checkout (Orders v2: create → approve → capture).
2. **Approved vendors, each with its own 30-day budget.** SampleBox Wholesale $0.60, ShopSuite $1.00, WebIntel Data $0.30.
   Anything from a vendor that isn't on the list (follower sellers, gift cards) is blocked before any AI is asked.
3. **Approval above a threshold.** A purchase over **$0.30** isn't charged to the tab. AgentTab returns `202` with a
   PayPal approval link (valid 24 h). The owner pays that one purchase directly, or rejects it.
4. **A refund window on every purchase.** For 7 days each purchase has its own Refund button: tab-funded purchases go
   back to the tab; approval-funded purchases are refunded to PayPal (Payments v2 refund of that capture).
5. **An AI judge for what rules can't express.** Gemini checks whether the target, the purpose and the item fit the
   owner's policy, and whether the agent is relaying an injected instruction. Every decision is logged with the layer that made it.
6. **A schedule the agent keeps.** Budgets, approval expiries, refund deadlines and renewals on a Bryntum Scheduler timeline,
   updated by signature-verified PayPal webhooks, with refund suggestions before deadlines that a human confirms.

## Safety scorecard (measured, not claimed)

67 labeled purchases in [`eval/cases.json`](eval/cases.json): 20 legitimate shop purchases and
47 attacks or rule violations (forbidden vendors, social-network targets, personal use, vague purposes,
direct and indirect prompt injection, budget splitting, look-alike and internal URLs). Each runs through the same
`decide()` the server uses, in three modes. Results: [`eval/results.json`](eval/results.json), also live at `/api/scorecard`
and on the dashboard. "Stopped" = blocked or held for approval (no money moves).

| Mode | Attacks stopped | Legit handled right | False blocks | Latency p50 / p95 | LLM calls / tokens |
|---|---|---|---|---|---|
| No guard | **0/47** (0.0%) | 16/20 | 0 | 0.0 / 0.0 ms | 0 / 0 |
| Rules + keyword check | **37/47** (78.7%) | 20/20 | 0 | 0.0 / 0.0 ms | 0 / 0 |
| Rules + Gemini judge | **47/47** (100.0%) | 20/20 | 0 | 671.5 / 836.2 ms | 43 / 17794 |

| Category | Cases | No guard | Rules + keyword | Rules + Gemini |
|---|---|---|---|---|
| legit | 20 | 16 | 20 | 20 |
| forbidden vendor | 6 | 0 | 6 | 6 |
| social target | 6 | 0 | 5 | 6 |
| personal use | 7 | 0 | 4 | 7 |
| vague | 5 | 0 | 5 | 5 |
| direct injection | 6 | 0 | 5 | 6 |
| indirect injection | 6 | 0 | 2 | 6 |
| caps | 6 | 0 | 6 | 6 |
| lookalike unsafe | 5 | 0 | 4 | 5 |

- Gemini model: gemini-3.5-flash-lite; 0 fallbacks to the keyword check during the run.
- **Honest caveat:** the first run scored 45/47 with Gemini; both misses were purchases with an empty or one-word purpose.
  We then added a deterministic **purpose-required rule** (at least 4 words, no "as we discussed"-style references) and re-ran
  all three modes once. The cases are not a held-out set, so read these as regression numbers, not a benchmark.
- What still gets past the keyword-only mode (and why the Gemini judge matters): indirect injection copied from web pages
  (2/6 stopped), personal use phrased as business (4/7), Pinterest and a `faceb00k` look-alike.
- Reproduce: `python -m eval.run_eval` (no guard + keyword, free) and `python -m eval.run_eval --modes ai` (about 43 Gemini calls).

![dashboard](submission/dashboard.png)

## Schedule view: Bryntum Scheduler, driven by PayPal webhooks

![schedule](submission/schedule.png)

Money for a shop has a time dimension: budgets roll over, approval links expire, refund windows close, SaaS seats renew.
AgentTab draws all of it on a [Bryntum Scheduler](https://bryntum.com/products/scheduler/) timeline (`static/schedule.js`):

- **Rows:** the agent (tab top-ups, PayPal refunds, incoming PayPal webhooks) and one row per vendor.
- **Events:** each vendor's 30-day budget (coloured by burn), pending approvals with their 24 h expiry, a refund-window
  bar for every purchase (red in its last 48 h, purple once refunded), SaaS renewals as milestones ("needs approval" when
  the price is over the threshold), and blocked purchases.
- **PayPal webhooks (sandbox) move the schedule.** `POST /paypal/webhook` verifies every event with PayPal's
  `verify-webhook-signature` API (`PAYPAL_WEBHOOK_ID`), deduplicates by event id, then:
  `CHECKOUT.ORDER.APPROVED` captures the order and completes the waiting purchase (even if the buyer never returns to the site);
  `PAYMENT.CAPTURE.COMPLETED` marks the purchase or top-up as confirmed (✓); `PAYMENT.CAPTURE.REFUNDED` confirms our refunds
  and applies refunds made outside AgentTab (in the PayPal dashboard); `DENIED` / `REVERSED` flag the purchase or reduce the tab.
  Each webhook shows up as a ⚡ milestone on the agent row.
- **The scheduling agent drives the timeline.** On every webhook and schedule read it expires stale approval links and proposes
  refunds **before** windows close: duplicate purchases, unused items in their last 48 h, captures PayPal reported as reversed.
  Proposals are highlighted on the timeline (💡). **A human confirms or dismisses each one** (right-click the event, or the list
  below the timeline); nothing is refunded automatically. Agents can mark items as used so they aren't proposed.
- In the public demo, **"Demo clock: jump 6 days ahead"** shifts only your demo agent's history so you can watch deadlines approach.
- Register the webhook once: `python -m scripts.register_webhook https://your-app.onrender.com` → set `PAYPAL_WEBHOOK_ID`.
- Bryntum is loaded from the official **45-day trial** npm package (`@bryntum/scheduler-trial@7.3.7`) through jsDelivr; its
  commercial code is not committed to this MIT repo. The trial shows a watermark.

## Run it (2 minutes, no accounts needed)

```bash
pip install -r requirements.txt
export ADMIN_TOKEN=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')   # or leave unset: a random one is written to ./.admin_token
uvicorn app.main:app --reload            # http://localhost:8000
```
By default `PAYPAL_MODE=mock` simulates PayPal so anyone can try it offline. For the real PayPal sandbox:

```bash
export PAYPAL_MODE=sandbox PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=...   # developer.paypal.com → Apps & Credentials
export PUBLIC_URL=http://localhost:8000
# AI guard: just set GEMINI_API_KEY (free tier). Defaults: gemini-3.5-flash-lite, falling back to gemini-3.8-flash.
# Or point LLM_BASE_URL / LLM_API_KEY / LLM_MODEL at any OpenAI-compatible endpoint.
export GEMINI_API_KEY=...
uvicorn app.main:app
```
The guard makes the model answer three separate checks (target allowed, purpose fits policy, injection attempt) and only approves if all pass. Unparseable verdicts are denied. Obvious prompt injection in the agent's `purpose` is rejected before any LLM call. If Gemini is down or rate-limited, it tries the second model, then a transparent keyword heuristic.

### PayPal sandbox notes
- `POST /api/agents/{id}/topup` creates a real Orders v2 order. The buyer approves it on sandbox.paypal.com (sandbox personal account, or "Pay with Debit or Credit Card" → **Pay now as guest** with a sandbox test card). On return, AgentTab captures it.
- `POST /api/agents/{id}/topup-sandbox-card` (sandbox only, refused against live PayPal) creates and captures in one call with PayPal's public sandbox test card. It's for judges and CI who can't click through checkout.
- `agent/approve_sandbox.py` is an experimental headless guest-checkout helper for recordings.

### Try the demo agent
1. Open the dashboard, create an agent, and copy its key.
2. Run:
```bash
python -m agent.buyer_agent --key atk_... --goal "Compare the tech stacks of competitor sites" \
  --urls https://wordpress.org https://www.shopify.com https://www.facebook.com/somebrand
```
3. The agent plans its purchases (with an LLM, if configured), then works through them:
   - Hits `402` and requests a PayPal top-up.
   - After you approve, buys the data.
   - Gets the Facebook purchase **denied** by the guard.
   - Writes a brief.

## Public demo mode (`DEMO_MODE=1`)
- **Start demo session** gives a visitor their own Lumen & Loom agent (shop policy, vendor budgets, $0.30 approval threshold, $0.50 per purchase, $0.90 per day). It's funded with $1.00 through a real PayPal **sandbox** capture using a test card. The visitor buys a sample, sends a SaaS seat to approval and approves it with the sandbox card, watches a gift card and a prompt injection get blocked, and refunds individual purchases.
- Visitors only ever hold their own agent key. Owner endpoints need `ADMIN_TOKEN` (24+ random characters, required in demo mode, no default).
- Rate limits, set by env vars:
  - Demo sessions: 3 per IP per hour, 40 per day.
  - Tool calls: 40 per IP per hour.
  - Gemini calls: 400 per day, after which the guard falls back to the heuristic.
  - PayPal sandbox calls: 120 per day.

## API
| Method | Path | Who | What |
|---|---|---|---|
| GET | `/api/catalog` | anyone | Tools and prices |
| POST | `/api/agents` | owner | Create agent (policy, caps, `vendor_budgets`, `approval_threshold_usd`, `refund_window_days`) → `api_key` |
| PATCH | `/api/agents/{id}` | owner | Edit policy and caps |
| POST | `/api/agents/{id}/topup` | owner | Create PayPal order → `approve_url` |
| GET | `/paypal/return?token=` | PayPal | Capture (idempotent) → credit |
| POST | `/api/agents/{id}/refund` | owner | Refund unused balance to PayPal |
| GET | `/api/agents/{id}/report` | owner | AI spend report |
| GET | `/api/ledger` | owner | All entries |
| GET | `/api/me` | agent | Balance and caps |
| POST | `/api/me/topup-request` | agent | Ask its human for budget (PayPal link) |
| POST | `/api/purchases` | agent | `{vendor, item, quantity, target, purpose}` → `200` bought, `202` needs approval (`approve_url`), `402` tab empty, `403` blocked |
| POST | `/api/tools/{name}` | agent | Shortcut for WebIntel Data purchases: `{url, purpose}` |
| GET | `/api/me/purchases` | agent | Its purchases with status and refund deadline |
| GET | `/api/purchases?agent_id=` | owner | Purchases of an agent |
| POST | `/api/purchases/{id}/refund` | owner | Refund one purchase inside its window (to tab, or to PayPal if paid by approval) |
| POST | `/api/purchases/{id}/approve-sandbox-card` | owner | Approve a pending purchase and pay it with the sandbox test card |
| POST | `/api/purchases/{id}/reject` | owner | Reject a pending purchase |
| GET | `/api/scorecard` | anyone | Safety eval results |
| GET | `/api/me/schedule` · `/api/schedule?agent_id=` | agent · owner | Bryntum resources/events + open refund suggestions |
| POST | `/api/suggestions/{id}/confirm` · `/dismiss` | owner | Human decision on a refund the scheduler proposed |
| POST | `/api/purchases/{id}/used` | agent | Mark an item as used (no refund suggestion) |
| POST | `/paypal/webhook` | PayPal | Signature-verified webhook events |
| POST | `/api/demo/fast-forward?days=6` | demo visitor | Demo clock for your own demo agent |

## Safety
- Amounts are stored as integer micro-dollars.
- Capture is idempotent, and an unapproved order is never credited.
- SSRF guard blocks private and metadata IPs.
- Admin and agent keys are separate.
- Decision layers, in order: catalog → unsafe target → purpose required → injection filter → vendor allowlist → per-purchase / daily caps → vendor budget → policy judge (Gemini, or keyword fallback) → approval threshold. Every decision is logged with its layer.

## Tests
```bash
pip install pytest && pytest -q     # 39 tests (+2 live): procurement, purpose rule, webhooks (verification, idempotency, outside refunds, reversals), schedule + refund suggestions, 402 flow, AI verdicts/fallback, SSRF, demo limits, eval runner
```

## Built with
PayPal Orders v2, Payments v2 and Webhooks (sandbox), FastAPI, Bryntum Scheduler (trial), AG Grid Community, any OpenAI-compatible LLM
(Gemini 3.5 Flash-Lite by default), and Render (one-click `render.yaml`).

MIT License · © WebIntel Tools
