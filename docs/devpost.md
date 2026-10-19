# AgentTab: Devpost draft

<!-- Draft for the Devpost submission (not posted). Every sentence carries a source note: a code file (path:function),
a test (path::test_name) or a result file (path#key). Numbers come only from eval/results.json and
eval/heldout-results.json; tests/test_devpost_draft.py checks all of this. -->

## Inspiration

A small shop's AI agent can order supplier samples, buy software seats and pay for competitor research, but handing it the company card is reckless, and approving every small purchase by hand defeats the point. <!-- source: app/procurement.py | tests/test_realistic_shop.py::test_two_seats_need_approval_one_seat_does_not -->

<!-- DEMAND-DATA: pending from reviewer -->

We wanted the owner to set the budget and the rules once, and then let the agent shop inside them. <!-- source: app/procurement.py:decide | tests/test_realistic_shop.py::test_two_seats_need_approval_one_seat_does_not -->

## What it does

AgentTab lets a small shop's AI purchasing agent buy from approved vendors, each with its own budget, under rules the owner writes in plain English. <!-- source: app/procurement.py:decide | tests/test_hardening.py::test_parallel_purchases_respect_vendor_budget_and_daily_cap -->
Every purchase goes through the same decision path: catalog, unsafe-target check, purpose rule, injection filter, vendor allowlist, spending caps, vendor budget, a policy check by Claude and an approval threshold. <!-- source: app/procurement.py:decide | tests/test_agenttab.py::test_purpose_required_hard_rule -->
Claude answers three separate questions (is the target allowed, does the purpose fit the policy, is this an injected instruction), and a purchase passes only if all three pass. <!-- source: app/guard.py:_verdict | tests/test_claude_guard.py::test_first_text_block_is_parsed_and_injection_flag_blocks -->
If Claude errors or is unavailable, the purchase waits for a human and is never approved automatically. <!-- source: tests/test_claude_guard.py::test_unavailable_claude_sends_the_purchase_to_human_approval | tests/test_claude_guard.py::test_sdk_errors_mean_unavailable_never_allowed -->
A purchase above $60 waits for the owner's approval inside AgentTab, with no PayPal call until the owner approves. <!-- source: app/procurement.py:SHOP_APPROVAL_THRESHOLD | tests/test_vendor_payouts_flow.py::test_above_threshold_reserves_no_paypal -->

AgentTab never touches the money; the vendor is paid by PayPal only after delivery. <!-- source: tests/test_vendor_payouts_ui.py::test_demo_session_is_vendor_funded_with_the_flag_on | tests/test_vendor_payouts_flow.py::test_paypal_only_after_fulfil_and_amount_is_the_reservation -->
Each purchase is one PayPal Orders v2 order with intent CAPTURE, the vendor's sandbox merchant account as the payee and the owner's saved card as the payment source. <!-- source: app/paypal.py:vendor_order_body | tests/test_vendor_payouts_flow.py::test_one_capture_order_straight_to_the_vendor -->
If the delivery step fails, PayPal is never called. <!-- source: tests/test_vendor_payouts_flow.py::test_fulfil_fails_no_paypal_call -->
Agents created in the owner console use a prepaid tab with authorize-then-capture holds. <!-- source: tests/test_paypal_depth.py::test_approval_link_authorizes_then_captures_after_delivery -->
Everything runs on the PayPal sandbox, and card top-ups are refused against live PayPal. <!-- source: tests/test_agenttab.py::test_card_topup_refused_on_live -->

The same guarded tools are available to any MCP client, with the agent key only and no owner tools, so an agent can buy but can never approve. <!-- source: app/mcp.py | tests/test_mcp.py::test_auth_agent_key_only | tests/test_mcp.py::test_no_owner_powers_exposed | tests/test_mcp.py::test_demo_key_works_but_cannot_approve -->
A Bryntum Scheduler timeline shows vendor budgets, pending approvals and refund windows, and signature-verified PayPal webhooks update it. <!-- source: static/schedule.js | app/schedule.py | tests/test_agenttab.py::test_webhook_requires_signature_in_sandbox -->
The scheduling agent proposes refunds before windows close, and a human confirms or dismisses each one. <!-- source: tests/test_agenttab.py::test_refund_suggestions_need_human_confirmation -->

## How we built it

The backend is a FastAPI app with a SQLite ledger that stores amounts as integer micro-dollars and rejects NaN and infinite amounts. <!-- source: app/main.py | app/ledger.py:to_micro | tests/test_hardening.py::test_nan_and_infinite_amounts_rejected -->
Purchases are serialized per agent and re-checked inside one write transaction, so parallel buys can't overspend the balance, a vendor budget or the caps. <!-- source: tests/test_hardening.py::test_parallel_purchases_never_overspend_balance | tests/test_vendor_payouts_flow.py::test_parallel_buys_never_exceed_caps -->
The guard calls Claude through the official Python SDK with a JSON-schema output format. <!-- source: app/llm.py | requirements.txt | tests/test_claude_guard.py::test_first_text_block_is_parsed_and_injection_flag_blocks -->
PayPal is integrated with Orders v2, Payments v2, Vault v3 for the saved sandbox card, and webhooks verified with PayPal's verify-webhook-signature API. <!-- source: app/paypal.py:vault_sandbox_card | app/paypal.py:verify_webhook | tests/test_paypal_depth.py::test_saved_card_topup_and_token_never_exposed -->
Every PayPal call is logged with PayPal's debug id, the purchase id and the PayPal-Request-Id, and never with a token, a card number or a merchant id. <!-- source: tests/test_paypal_debug_log.py::test_vendor_payment_logs_debug_id_purchase_and_request_id -->
The MCP server speaks Streamable HTTP, and a stdio bridge lets local clients connect too. <!-- source: app/mcp.py | clients/agenttab_mcp_stdio.py | tests/test_mcp.py::test_stdio_bridge_round_trip -->
The dashboard uses AG Grid for the tables and Bryntum Scheduler for the timeline. <!-- source: static/dashboard.html | static/schedule.js -->
To measure the guard, we wrote 67 labeled purchases (20 legitimate and 47 attacks) and ran them through the same decision code the server uses, with no guard, with rules only and with rules plus Claude. <!-- source: eval/results.json#cases | eval/results.json#legit | eval/results.json#attacks | eval/run_eval.py -->

## Challenges we ran into

PayPal refused when AgentTab tried to refund a capture that had landed in the vendor's account, so a refund became a request to the vendor that reconcile confirms by reading the capture from PayPal. <!-- source: docs/vendor-payouts-plan.md | tests/test_vendor_payouts_reconcile.py::test_refund_request_moves_to_refund_requested_without_paypal | tests/test_vendor_payouts_reconcile.py::test_refunded_capture_becomes_refunded_and_stops_counting -->
When PayPal gives no clear answer, the purchase becomes payment_unknown, keeps counting against the budget and the agent is frozen until reconcile replays the same PayPal-Request-Id. <!-- source: tests/test_vendor_payouts_flow.py::test_still_unknown_after_one_retry | tests/test_vendor_payouts_reconcile.py::test_reconcile_replays_unknown_with_same_request_id -->
A return URL, a webhook and a double click can all try to finish the same payment, so every state change is claimed atomically and captures once. <!-- source: tests/test_hardening.py::test_return_url_and_webhook_race_captures_once | tests/test_hardening.py::test_double_card_approval_charges_once | tests/test_vendor_payouts_flow.py::test_two_concurrent_approvals_one_payment -->
Rules alone missed instructions copied from web pages: they stopped 2 of 6 indirect prompt injections, while rules plus Claude stopped 6 of 6. <!-- source: eval/results.json#by_category.indirect_injection.keyword | eval/results.json#by_category.indirect_injection.ai -->

## Accomplishments that we're proud of

With Claude, the guard stopped 47 of 47 attacks and handled 20 of 20 legitimate purchases correctly, with 0 false blocks. <!-- source: eval/results.json#modes.ai.attacks_stopped | eval/results.json#modes.ai.legit_correct | eval/results.json#modes.ai.false_blocks -->
Rules alone stopped 37 of 47, and no guard stopped 0 of 47. <!-- source: eval/results.json#modes.keyword.attacks_stopped | eval/results.json#modes.none.attacks_stopped -->
Those 67 cases are not a held-out set, so a separate reviewer wrote 40 more cases that we ran once. <!-- source: eval/results.json#notes | eval/HELDOUT.md | eval/heldout-run.json -->
On that held-out set, Claude stopped 26 of 26 attacks and allowed 14 of 14 legitimate purchases, against 19 of 26 attacks for rules alone. <!-- source: eval/heldout-results.json#ai.summary.attacks_stopped | eval/heldout-results.json#ai.summary.legit_correct | eval/heldout-results.json#keyword.summary.attacks_stopped -->
Every Claude decision in the main run was a complete verdict, with 0 fallbacks, at a median of 978.2 ms per decision. <!-- source: eval/results.json#modes.ai.heuristic_fallbacks | eval/results.json#modes.ai.latency_p50_ms -->
The dashboard's Proof section reads these numbers from the committed result files, so the page can't drift from the measurements. <!-- source: app/main.py:heldout_summary | tests/test_e2e_landing.py::test_proof_table_comes_from_the_results_files_with_previous_catalog_label | tests/test_readme_scorecard.py::test_readme_scorecard_tables_match_results_json -->

## What we learned

Keyword rules are fast but miss personal purchases phrased as business: rules stopped 4 of 7 personal-use attempts, and rules plus Claude stopped 7 of 7. <!-- source: eval/results.json#by_category.personal_use.keyword | eval/results.json#by_category.personal_use.ai -->
An AI judge has to fail closed, so an error, a refusal or an unparseable answer sends the purchase to a human instead of letting it through. <!-- source: tests/test_claude_guard.py::test_sdk_errors_mean_unavailable_never_allowed | tests/test_claude_guard.py::test_unavailable_claude_sends_the_purchase_to_human_approval -->
Payments need an answer for every unclear case, which is why a mismatch between PayPal's reply and the purchase is flagged and freezes the agent. <!-- source: app/vendorpay.py:check_payment | tests/test_vendor_payouts_flow.py::test_response_mismatch_is_flagged_and_freezes -->

## What's next

A real product would onboard each vendor through PayPal's multiparty seller onboarding, which also lets the platform refund on the seller's behalf; this sandbox demo doesn't use it. <!-- source: docs/vendor-payouts-impl-plan.md -->
The shop vendors' delivery step is simulated today, and real vendor integrations would replace it. <!-- source: app/procurement.py -->
The hosted demo loads Bryntum's trial package, so a production deployment would need a Bryntum license. <!-- source: static/dashboard.html -->

## Built with

- PayPal Orders v2, Payments v2, Vault v3 and Webhooks (sandbox) <!-- source: app/paypal.py -->
- Claude, through Anthropic's official Python SDK <!-- source: app/llm.py | requirements.txt -->
- Python, FastAPI and SQLite <!-- source: requirements.txt | app/ledger.py -->
- Model Context Protocol (Streamable HTTP) <!-- source: app/mcp.py -->
- Bryntum Scheduler (trial) and AG Grid Community <!-- source: static/schedule.js | static/dashboard.html -->
- pytest and Playwright <!-- source: requirements-dev.txt -->
- Render <!-- source: render.yaml -->
- MIT licensed, © WebIntel Tools <!-- source: LICENSE -->
