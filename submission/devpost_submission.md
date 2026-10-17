**Repo:** https://github.com/JordiBen/agenttab · **Live demo:** https://agenttab.onrender.com

# Devpost submission: PayPal AI Hackathon (deadline Nov 12, 2026, 12:00 PT = 22:00 Israel time)

**Project name:** AgentTab
**Tagline:** A purchasing agent for small shops: PayPal-funded tab, vendor budgets, approvals, refund deadlines on a Bryntum timeline.
**Prizes to tick:** Grand prizes (Best Use of Agentic Commerce, Best Use of PayPal + AI, Most Impactful), **Best Use of Bryntum** (primary sponsor prize), Best Use of Render
**Built with:** paypal-orders-api, paypal-payments-api, paypal-webhooks, bryntum-scheduler, gemini, python, fastapi, sqlite, ag-grid, render
**Try it out:** https://agenttab.onrender.com · https://github.com/JordiBen/agenttab

## Inspiration
Lumen & Loom is the kind of shop we built for: six people selling home textiles and ceramics online. An AI agent could
order supplier samples, renew the inventory app and pull competitor research. But a company card is reckless, and approving
every $0.20 sample by hand defeats the point. Small shops need what finance teams have: budgets, approvals and refund deadlines.

## What it does
- **PayPal-funded tab.** The owner tops up once with PayPal Checkout (Orders v2). On HTTP 402 the agent asks its human for more.
- **Approved vendors with their own 30-day budgets.** Supplier samples $0.60, SaaS $1.00, research $0.30 (sandbox prices).
  Follower sellers and gift cards are blocked before any AI is asked.
- **Approval above $0.30.** The purchase isn't charged to the tab; AgentTab returns 202 with a PayPal approval link (24 h).
- **A refund window on every purchase (7 days).** Tab purchases go back to the tab; approval purchases are refunded to PayPal.
- **Gemini judges what rules can't:** does the target, purpose and item fit the owner's plain-English policy, and is the agent
  relaying an injected instruction? Deterministic rules run first (allowlist, budgets, caps, unsafe URLs, purpose required).
- **A Bryntum Scheduler timeline the agent keeps.** Rows for the agent and each vendor; events for budget periods, pending
  approvals, refund deadlines and SaaS renewals. Signature-verified **PayPal webhooks** move it: an approved order completes the
  waiting purchase, captures get a ✓, refunds made in the PayPal dashboard are applied, reversals are flagged. The scheduling
  agent expires stale approvals and **proposes refunds before windows close** (duplicates, unused items, reversed captures).
  A human confirms each one with a right-click.

## Safety scorecard (measured)
67 labeled purchases (20 legitimate, 47 attacks or violations), same decision code as production, three modes:

| Mode | Attacks stopped | Legit handled right | False blocks |
|---|---|---|---|
| No guard | 0/47 | 16/20 | 0 |
| Rules + keyword check | 37/47 (78.7%) | 20/20 | 0 |
| Rules + Gemini judge | **47/47 (100%)** | 20/20 | 0 |

Gemini p50 672 ms / p95 836 ms, 43 calls, ~18k tokens for the whole set. Honest caveat: the first Gemini run scored 45/47
(two vague purposes); we added a purpose-required rule and re-ran once. The set isn't held out. Results are committed
(`eval/results.json`) and shown live on the dashboard.

## How we built it
- FastAPI + SQLite ledger in integer micro-dollars.
- PayPal REST (sandbox): OAuth, Orders v2 create/capture with idempotent `PayPal-Request-Id`, Payments v2 refunds per capture,
  Webhooks with `verify-webhook-signature` and event-id deduplication.
- Gemini 3.5 Flash-Lite (OpenAI-compatible endpoint) with a structured three-check verdict, fallback model, and a keyword
  fallback that never fails open.
- Bryntum Scheduler (official 45-day trial from npm via jsDelivr): custom event/resource models, event renderer, tooltips,
  context-menu actions, current-time line.
- AG Grid for the purchases and ledger tables. Render Blueprint deploy with a rate-limited public sandbox demo.

## Challenges
- Making webhooks and the return URL race-safe: whichever arrives first captures, the other is a no-op.
- Keeping the human in the loop: the scheduler proposes, never refunds on its own.
- Measuring safety honestly instead of claiming it.

## Accomplishments
- Full loop on the live demo with real sandbox money: fund → buy → approval → refund, with blocks you can try yourself.
- 39 automated tests.

## What's next
PayPal Vault for pre-approved top-ups, Payouts to vendors, an MCP server so coding agents can buy through AgentTab.

## Testing instructions (for judges)
- **Live demo (no login):** https://agenttab.onrender.com (free instance; first load can take ~1 min).
  1. **Start demo session**: your own agent, funded with $1.00 through a real PayPal *sandbox* capture (test card).
  2. Buy a mug sample twice, buy competitor research, request the $0.40 SaaS seat (goes to approval), try the gift card and the prompt injection.
  3. In the **Schedule**, right-click the yellow approval → *Approve (pay with sandbox card)*; see the PayPal webhook ⚡ arrive.
  4. Click **Demo clock: jump 6 days ahead**: refund windows turn red and the scheduler proposes refunds. Right-click → *Confirm suggested refund*.
- **Local:** `pip install -r requirements.txt && uvicorn app.main:app` (mock PayPal by default; the admin token is printed to `./.admin_token`).
  For the sandbox, set `PAYPAL_MODE=sandbox`, the client id/secret and `PAYPAL_WEBHOOK_ID` (see README).
- **Repo:** https://github.com/JordiBen/agenttab (MIT; Bryntum trial loaded from npm, not redistributed)
