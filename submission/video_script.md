# Demo video (≤ 2:50). Faceless screen recording plus TTS voice-over. No music, no third-party logos beyond the UI.
Production (no human work): screen capture of the hosted demo with headless Chrome + ffmpeg, free TTS (piper / Edge TTS),
ffmpeg edit, upload to the WebIntel Tools YouTube channel (public).

| Time | Screen | Voice-over |
|---|---|---|
| 0:00–0:12 | Title "AgentTab", then the dashboard header | "Lumen and Loom is a six-person home-goods shop. Its AI agent buys samples, software and research. AgentTab gives it a PayPal-funded tab with rules." |
| 0:12–0:30 | Demo card: policy text, Start demo session, $1.00 sandbox capture | "The owner wrote the rules in plain English. I start a session: the tab is funded with one dollar through a real PayPal sandbox capture." |
| 0:30–0:55 | Buy mug sample (allowed), competitor research (allowed), vendor budget bars move | "The agent buys a supplier sample and some competitor research. Gemini checks each purchase against the policy, and each vendor's thirty-day budget fills up." |
| 0:55–1:15 | SaaS seat → "needs your approval"; gift card and prompt injection → blocked with layer | "A forty-cent software seat is above the approval threshold, so it waits for me. A gift card is blocked by the vendor list, and an instruction copied from a supplier's web page is caught as prompt injection." |
| 1:15–1:50 | Bryntum Schedule: rows agent + vendors; right-click approval → Approve; ⚡ webhook milestone, ✓ on the bar | "Everything with a deadline lives on a Bryntum Scheduler timeline: budgets, approvals, refund windows and renewals. I approve the seat with PayPal. PayPal's webhook arrives, signature-verified, and the schedule updates itself." |
| 1:50–2:15 | Demo clock +6 days → red refund windows, 💡 suggestions; right-click → Confirm refund | "Six days later the refund windows are closing. The scheduling agent spots a duplicate mug and an unused sample and proposes refunds. Nothing moves until I confirm." |
| 2:15–2:38 | Safety scorecard panel | "We measured it. Sixty-seven labeled purchases: with no guard, every attack goes through. Rules plus keywords stop thirty-seven of forty-seven. Rules plus Gemini stop all forty-seven, with zero false blocks." |
| 2:38–2:50 | GitHub README | "AgentTab: PayPal money, autonomous agents, rules that hold. Open source on GitHub." |
