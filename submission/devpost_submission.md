**Repo:** https://github.com/JordiBen/agenttab · **Live demo:** https://agenttab.onrender.com

# Devpost submission: PayPal AI Hackathon (deadline Nov 12, 2026, 12:00 PT = 22:00 Israel time)

**Project name:** AgentTab
**Tagline:** PayPal-funded spending tabs for AI agents, guarded by AI.
**Prizes to tick:** Grand Prize, Best Use of Agentic Commerce, Best Use of PayPal + AI, Best Use of AG Grid, Best Use of Render (credits)
**Built with:** paypal-orders-api, paypal-payments-api, python, fastapi, ag-grid, gemini, render, sqlite
**Try it out:** https://agenttab.onrender.com · https://github.com/JordiBen/agenttab

## Inspiration
Agents are starting to buy things on their own, such as data, API calls and compute. Today there are only
two options:
- Give the agent a card, which is reckless.
- Approve every two-cent purchase by hand, which defeats the point of having an agent.

We wanted the same thing a parent gives a teenager: a prepaid tab, clear rules, and a statement they can read.

## What it does
- **Fund with PayPal.** The owner tops up an agent's tab with PayPal Checkout. When the agent runs out
  of money it gets an HTTP 402. It then calls `topup-request` and forwards a PayPal approval link to its human.
  There's no card on file and no blank cheque.
- **Spend autonomously.** The agent buys metered web-intelligence calls (page → Markdown, tech-stack
  detection, SEO audit) at $0.01–$0.03 each from a machine-readable catalog. Failed calls cost nothing.
- **AI spend guard.** The owner writes the rules in plain English. Every purchase first passes hard
  limits (per-call cap, daily cap, balance). An LLM then judges whether the tool, the target and the agent's
  stated purpose fit the policy. Denials are explained and logged.
- **See everything and get your money back.** A live AG Grid ledger shows every charge, denial, top-up and refund,
  and policies and caps can be edited inline. One click refunds unused credit to the payer's PayPal (Payments v2
  refunds). An AI spend report summarises behaviour and flags risks.

## How we built it
- FastAPI backend.
- PayPal REST: OAuth client credentials, Orders v2 create and capture with `payer-action` approval,
  idempotent `PayPal-Request-Id`, and Payments v2 capture refunds.
- SQLite ledger in integer micro-dollars.
- AG Grid Community dashboard: row styling for denials, inline editing that PATCHes policies.
- LLM through any OpenAI-compatible endpoint (Gemini 3.5 Flash-Lite by default, falling back to Gemini 3.8 Flash) for the guard, the buyer agent's
  purchase planning and the owner report.
- Deterministic fallback, so the guard never fails open.
- Deployed with a Render Blueprint.

## Challenges
- Making the guard safe when the model is unavailable or returns garbage: it fails closed on bad output.
- Keeping capture idempotent when PayPal redirects twice.
- Ensuring a failed tool call is never billed.

## Accomplishments
- An end-to-end agentic purchase loop: plan → 402 → human PayPal approval → autonomous spend → AI denial
  of an off-policy purchase → refund of leftover credit.
- Nine automated tests cover each step.

## What we learned
Agent commerce needs a policy layer more than it needs new payment rails. PayPal already provides the
trusted funding and refund primitives.

## What's next
- PayPal Vault for pre-approved top-ups under a ceiling.
- Payouts to pay third-party tool sellers.
- An MCP server so Claude and Cursor agents can use AgentTab natively.
- Multi-agent team budgets.

## Testing instructions (for judges)
1. `pip install -r requirements.txt && uvicorn app.main:app`
2. Open http://localhost:8000 (admin token `dev-admin`).
- No credentials are needed: mock PayPal is the default.
- For the real sandbox, set `PAYPAL_MODE=sandbox` and your sandbox client ID and secret (see the README).
- **Live demo (no login):** https://agenttab.onrender.com. Click **Start demo session**, which funds your own agent with $1.00 through a real PayPal *sandbox* capture. Then follow buttons 2–5:
  - Allowed purchase.
  - Facebook blocked by the Gemini guard.
  - AI report.
  - Refund back to PayPal.
- The free Render instance sleeps when idle, so the first load can take about 1 minute.
- **Repo:** https://github.com/JordiBen/agenttab (MIT)
