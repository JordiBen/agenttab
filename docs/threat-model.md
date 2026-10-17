# Threat model, mapped to tests

Assets: the owner's money (the tab and PayPal captures), the owner's policy, the server's network position, and the
shared demo budgets (PayPal and Gemini quotas). Attackers: a misbehaving or prompt-injected agent, a web page the agent
reads, an anonymous demo visitor, and anyone who can reach the public endpoints.

Every row names the tests that prove the mitigation. `tests/test_docs.py` fails CI if a test named here no longer exists.

## Agent and prompt injection

| Threat | Mitigation | Tests |
|---|---|---|
| Agent buys something the owner's policy forbids | Vendor allowlist, then the Gemini policy judge (three separate checks: target, purpose, injection) | `test_guard_denies_policy_violation_and_logs`, `test_vendor_allowlist_and_budget`, `test_ai_guard_uses_llm_verdict` |
| Instruction injected into the purpose ("ignore your rules…") | Deterministic injection prefilter before any LLM call; the judge prompt treats fields as data | `test_injection_prefilter`, `test_decide_modes_offline` |
| Vague or missing purpose | Purpose-required hard rule (4+ words, no "as discussed") | `test_purpose_required_hard_rule` |
| Model returns garbage, errors or is out of quota | Second model, then **fail closed**: held for human approval, never decided by keywords | `test_ai_guard_falls_back_to_second_model_then_fails_closed`, `test_ai_guard_garbage_fails_closed`, `test_ai_unavailable_routes_to_human_not_heuristic`, `test_ai_budget_exhausted_routes_to_human`, `test_llm_daily_budget_fails_closed` |
| Spend report repeats injected numbers or claims | Numbers come from the ledger template; the model only comments on data marked untrusted | `test_report_numbers_come_from_ledger_not_model` |
| The guard degrades unnoticed | 67-case labeled eval in CI (offline modes must match the committed scorecard) | `test_eval_cases_file_and_scorecard`, `test_eval_runner_offline` |

## Money and concurrency

| Threat | Mitigation | Tests |
|---|---|---|
| Parallel purchases overspend the balance, daily cap or vendor budget | Per-agent lock + re-check inside one `BEGIN IMMEDIATE` write transaction | `test_parallel_purchases_never_overspend_balance`, `test_parallel_purchases_respect_vendor_budget_and_daily_cap`, `test_commit_recheck_blocks_overspend_even_without_the_lock`, `test_caps` |
| Return URL and webhook capture the same order twice | Atomic `CREATED → CAPTURING` claim; PayPal-Request-Id `capture-<order>` | `test_return_url_and_webhook_race_captures_once`, `test_capture_is_idempotent` |
| Double click on "approve" charges twice | Atomic `pending_approval → paying` claim; the PayPal link is retired first | `test_double_card_approval_charges_once`, `test_paypal_link_after_card_payment_is_not_captured` |
| A webhook redelivered concurrently is applied twice | Event id inserted (`INSERT OR IGNORE`) before processing | `test_webhook_event_processed_once_under_concurrency` |
| Unapproved order credited | Credit only after a COMPLETED capture | `test_unapproved_order_not_credited`, `test_402_then_fund_then_charge` |
| Refund returns more than a capture took in | Refunds tracked per capture | `test_tab_refund_tracks_refunded_amount_per_capture`, `test_refund_unused`, `test_tab_refund_inside_and_outside_window` |
| Failed vendor call is charged | Charge only after fulfilment; approval-funded failures are auto-refunded | `test_failed_tool_not_charged` |
| Instantly-delivered data refunded / bought in bulk | Live data is final and quantity 1 | `test_live_items_quantity_one_and_non_refundable` |
| NaN / Infinity / out-of-range amounts | Rejected at validation and in `money()` | `test_nan_and_infinite_amounts_rejected` |
| Retried PayPal call creates a second order/capture/refund | Operation-derived PayPal-Request-Id | `test_paypal_request_ids_are_derived_from_the_operation` |

## Webhooks and PayPal

| Threat | Mitigation | Tests |
|---|---|---|
| Forged webhook | `verify-webhook-signature`; unsigned requests rejected before they use the budget | `test_webhook_requires_signature_in_sandbox`, `test_webhook_junk_without_headers_does_not_consume_budget` |
| Refund or reversal made in the PayPal dashboard | Webhook applies it to the ledger and flags the purchase | `test_webhook_refund_outside_agenttab_and_reversal`, `test_webhook_completes_approval_and_confirms` |
| Test-card shortcut used against live PayPal | Refused unless the base URL is the sandbox | `test_card_topup_refused_on_live` |
| Offline mock PayPal exposed publicly | Mock refuses a public URL or demo mode; mock checkout page escapes input | `test_mock_mode_refused_for_public_or_demo`, `test_mock_approve_rejects_html` |

## Demo visitors and public endpoints

| Threat | Mitigation | Tests |
|---|---|---|
| Regular agent key used to act as owner on a demo server | Only agents created by `/api/demo/session` (flagged) act as owner, only on their own purchases | `test_demo_mode_does_not_turn_regular_agents_into_owners`, `test_demo_owner_actions_scoped_to_own_agent`, `test_demo_flag_is_ignored_if_server_leaves_demo_mode`, `test_demo_fast_forward_scoped` |
| Demo routes reachable with demo mode off | 404 | `test_demo_routes_closed_when_demo_mode_off` |
| Weak admin token on the public demo | Demo mode requires 24+ random chars; no default | `test_demo_requires_strong_admin_token`, `test_admin_required` |
| One visitor burns the shared PayPal / Gemini quota | Per-IP and per-agent limits, separate Gemini budgets for guard, reports and Gauntlet | `test_demo_rate_limits`, `test_paypal_daily_budget`, `test_per_agent_limits_on_topup_report_refund_purchases`, `test_guard_and_report_have_separate_llm_budgets`, `test_gauntlet_dry_run_and_rate_limit`, `test_gauntlet_global_daily_cap` |
| Spoofed client IP to dodge limits | `True-Client-IP` ignored; Cloudflare's `CF-Connecting-IP` (client values rejected at the edge) | `test_true_client_ip_not_trusted` |
| Gauntlet moves money or hides an AI outage | Dry run (no ledger, no PayPal); unavailable AI shows as held | `test_gauntlet_dry_run_and_rate_limit`, `test_gauntlet_ai_unavailable_is_held_not_allowed` |
| Arbitrary files served from `static/` | Explicit allowlist | `test_static_files_are_an_allowlist` |
| Admin token leaked through storage or a tampered CDN script | `sessionStorage` only; pinned scripts with Subresource Integrity | `test_dashboard_has_no_inline_admin_token_storage` |

## SSRF (the agent picks URLs, the server fetches them)

| Threat | Mitigation | Tests |
|---|---|---|
| Target is internal (loopback, private, metadata, CGNAT, IPv6 tricks) | Every resolved address must be global; mapped/6to4/compat/unspecified forms rejected | `test_ssrf_blocked_by_default`, `test_ip_is_public_rejects_internal_forms`, `test_ip_is_public_accepts_public`, `test_literal_mapped_and_unspecified_targets_blocked` |
| Public URL redirects to an internal one | Each hop re-validated, max 4 hops | `test_redirect_to_metadata_ip_is_blocked`, `test_redirect_hops_are_capped` |
| DNS rebinding between check and connect | Connect to the validated IP (Host header and TLS SNI keep the hostname) | `test_connects_to_the_validated_ip` |
| Huge response exhausts memory | Streamed, 2 MB cap | `test_body_is_capped_at_2mb` |

## Known limits (not mitigated)

- Rate limits and per-agent locks are in memory and per process; the hosted app runs as one instance. The SQLite
  transaction re-check is the cross-process backstop for money.
- The eval cases are not a held-out set; treat the scorecard as regression numbers.
- SQLite lives on Render's ephemeral free disk; a restart resets demo data.
- Settings import side effects and config reading: `test_settings_read_env_at_construction`.
