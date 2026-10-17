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
| A slow guard/vendor/PayPal step ties up server threads behind one agent's lock | The lock wait is bounded (`AGENT_LOCK_TIMEOUT_S`, 2.5 s): a second purchase for the same agent gets `429` busy with `Retry-After`; other agents are never blocked; MCP `buy` reports it as a tool error | `test_second_purchase_waits_briefly_then_gets_429_busy`, `test_busy_lock_is_per_agent_and_released_after_errors`, `test_mcp_buy_reports_busy_instead_of_hanging` |
| Return URL and webhook capture the same order twice | Atomic `CREATED → CAPTURING` claim; PayPal-Request-Id `capture-<order>` | `test_return_url_and_webhook_race_captures_once`, `test_capture_is_idempotent` |
| Double click on "approve" charges twice | Atomic `pending_approval → paying` claim; the PayPal link is retired first | `test_double_card_approval_charges_once`, `test_paypal_link_after_card_payment_is_not_captured` |
| A webhook redelivered concurrently is applied twice | Event id inserted (`INSERT OR IGNORE`) before processing | `test_webhook_event_processed_once_under_concurrency` |
| Unapproved order credited | Credit only after a COMPLETED capture | `test_unapproved_order_not_credited`, `test_402_then_fund_then_charge` |
| Refund returns more than a capture took in | Refunds tracked per capture | `test_tab_refund_tracks_refunded_amount_per_capture`, `test_refund_unused`, `test_tab_refund_inside_and_outside_window` |
| Failed vendor call is charged | Charge only after fulfilment; approval purchases are only *held* (PayPal authorization) and the hold is voided if delivery fails | `test_failed_tool_not_charged`, `test_vendor_failure_voids_hold_and_charges_nothing` |
| Instantly-delivered data refunded / bought in bulk | Live data is final and quantity 1 | `test_live_items_quantity_one_and_non_refundable` |
| NaN / Infinity / out-of-range amounts | Rejected at validation and in `money()` | `test_nan_and_infinite_amounts_rejected` |
| Retried PayPal call creates a second order/capture/refund | Operation-derived PayPal-Request-Id | `test_paypal_request_ids_are_derived_from_the_operation` |

## PayPal holds, declines, reconciliation and saved cards

| Threat | Mitigation | Tests |
|---|---|---|
| Approved purchase is charged but never delivered | Approval links use `intent=AUTHORIZE`: the hold is captured only after delivery, for exactly the approved amount; otherwise voided | `test_approval_link_authorizes_then_captures_after_delivery`, `test_vendor_failure_voids_hold_and_charges_nothing`, `test_authorized_amount_must_match_approved_amount` |
| Approval link paid after it expired, or while the agent is frozen | The hold is voided, nothing charged | `test_late_paid_link_on_expired_approval_is_voided`, `test_frozen_agent_hold_is_voided_not_captured` |
| Return URL and webhook both authorize the same link | Same atomic order claim; one hold, one capture | `test_return_url_and_webhook_race_authorizes_and_captures_once` |
| Forged "capture completed" webhook completes an approval | Authorize-intent purchases complete only through AgentTab's own capture | `test_capture_webhook_for_authorize_order_does_not_complete_purchase`, `test_order_capture_endpoint_refuses_authorize_order` |
| Card or checkout capture declined | Nothing credited; the order is released for a retry | `test_card_topup_decline_credits_nothing`, `test_checkout_capture_decline_releases_order_for_retry` |
| Capture declined *after* delivery | Hold voided, purchase flagged `capture_declined`, agent frozen | `test_capture_decline_after_delivery_freezes_agent` |
| Refund declined by PayPal | Purchase stays `completed`, no ledger refund; a retry works | `test_refund_decline_keeps_purchase_completed_then_retry_works` |
| Decline drills abused or leaking to live | `X-Simulate-Decline` needs the admin token, an allowlisted code and the sandbox; it uses a fresh PayPal-Request-Id | `test_simulated_decline_is_owner_and_allowlist_only`, `test_mock_response_header_only_in_sandbox_and_fresh_request_id` |
| Ledger and PayPal drift apart (refund/reversal in the dashboard, missed webhook, forged credit) | `POST /api/agents/{id}/reconcile` compares each capture's amount, status and refunds with PayPal; any mismatch or a negative tab freezes the agent (`423` on purchases and top-up requests) until the owner unfreezes | `test_reconcile_clean_then_outside_refund_freezes`, `test_reconcile_flags_each_paypal_side_change`, `test_reconcile_unknown_capture_and_amount_mismatch`, `test_reconcile_lookup_error_is_unverified_not_frozen` |
| Reversal or outside refund leaves the tab negative | The webhook freezes the agent | `test_reversal_webhook_freezes_and_negative_balance_reported`, `test_outside_topup_refund_into_negative_freezes` |
| Agent or demo visitor freezes/unfreezes, or uses the saved card | Owner-only endpoints; the first freeze reason is kept | `test_freeze_endpoints_are_admin_only`, `test_demo_agent_cannot_use_vault_or_freeze_endpoints` |
| Agent drains the owner's saved card | Auto top-up runs only after the guard allowed the purchase, never for a frozen agent, within the owner's 30-day cap (manual saved-card top-ups count), max 5 a day, serialized per agent; the Vault token never leaves the server | `test_auto_topup_only_after_guard_allows_and_within_cap`, `test_parallel_purchases_never_auto_topup_past_cap`, `test_manual_saved_card_topups_count_toward_cap`, `test_auto_topup_never_runs_for_frozen_agent_and_delete_turns_it_off`, `test_saved_card_topup_and_token_never_exposed` |
| Saved card declined during auto top-up | `402` with the reason; nothing bought, held or credited | `test_auto_topup_card_decline_is_402_and_credits_nothing`, `test_auto_topup_declined_authorization_is_402_and_nothing_held` |
| Saved card charged for a purchase that then fails | Auto top-up is a hold (authorize), captured only after the purchase commits; vendor failure or a `409` at commit voids it, and the credit is written in the purchase's own transaction | `test_auto_topup_card_is_only_held_during_delivery_and_captured_after_commit`, `test_auto_topup_vendor_failure_voids_hold`, `test_auto_topup_commit_conflict_409_voids_hold_and_writes_no_credit` |
| Saved-card capture declined after delivery | The credit is reversed (`fund_reversal`), the purchase flagged, the agent frozen (`423`) for reconciliation | `test_auto_topup_capture_declined_after_delivery_reverses_credit_and_freezes` |
| Free hosting wipes SQLite, leaving a stale demo key in the browser | The first `401` clears `sessionStorage.dkey` and shows "session expired, start a new demo"; a new session works right away | `test_wiped_db_on_next_action_clears_key_and_says_expired`, `test_stale_key_on_page_load_is_cleared_by_the_background_refresh` (browser e2e) |

## MCP server (agents connecting through `/mcp`)

| Threat | Mitigation | Tests |
|---|---|---|
| MCP agent approves, refunds or unfreezes its own spending | No owner tools exist; unknown tools are rejected | `test_no_owner_powers_exposed`, `test_demo_key_works_but_cannot_approve` |
| MCP path bypasses the guard, caps or rate limits | `buy` calls the same `do_purchase()` as the REST API (guard, budgets, approval, limits, ledger) | `test_buy_outcomes_match_rest_semantics`, `test_guard_blocks_via_mcp_and_is_logged`, `test_mcp_purchases_share_rest_rate_limits`, `test_frozen_and_invalid_args` |
| Admin token used over MCP | Only agent keys authenticate | `test_auth_agent_key_only` |
| Gateway routes on one tool while the server runs another | `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` must match the body (`-32020`); unsupported versions get `-32022` | `test_header_validation_modern` |
| Cross-site browser or DNS-rebinding calls, oversized or batched bodies | Foreign `Origin` → 403, 64 KB cap, batches rejected, no sessions | `test_transport_errors` |
| Agent touches another agent's purchases | Tools are scoped to the key's agent | `test_mark_used_only_own_purchase_and_topup_link` |
| Malformed arguments (extra fields, NaN, huge amounts) | Strict schemas (`additionalProperties: false`); errors returned as tool errors | `test_frozen_and_invalid_args` |
| stdio bridge leaks errors or answers notifications | Bridge maps transport errors to JSON-RPC errors with the request id, never answers notifications | `test_stdio_bridge_round_trip` |
| Version negotiation breaks older clients | Handshake revisions supported alongside the stateless one | `test_legacy_handshake_2025`, `test_discover_and_tools_list_modern` |

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
