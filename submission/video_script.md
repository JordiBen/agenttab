# Demo video (≤ 2:50). Faceless screen recording plus TTS voice-over. No music, no third-party logos beyond the UI.
How it gets produced, with no human work:
- Screen capture with headless Chrome or the box desktop and ffmpeg.
- Voice: a free TTS engine (e.g. piper / espeak-ng, or Edge TTS).
- Edit with ffmpeg.
- Upload to the existing WebIntel Tools YouTube channel as a public video.

| Time | Screen | Voice-over |
|---|---|---|
| 0:00–0:15 | Title card "AgentTab", then the dashboard | "AI agents are starting to buy things on their own. Give one a card and you're one hallucination away from a surprise bill. AgentTab gives agents a PayPal-funded tab with rules." |
| 0:15–0:40 | Create agent: type a policy, set caps | "I create a research agent and write its spending policy in plain English: competitor research only, never social media, two dollars a day." |
| 0:40–1:05 | Terminal: buyer agent runs and gets 402, prints a PayPal link | "The agent plans its purchases with Gemini, then tries to buy data. Its tab is empty, so the API answers 402 Payment Required. The agent asks me for five dollars and sends me a PayPal approval link." |
| 1:05–1:25 | PayPal sandbox checkout, approve, redirect | "I approve once in PayPal. AgentTab captures the order through the Orders API and credits the tab instantly." |
| 1:25–1:55 | Agent rerun: two purchases succeed; Facebook denied (red row in AG Grid) | "Now the agent spends by itself, two cents per call. When it tries to pull a Facebook page, the AI guard checks the policy and blocks it, and the denial shows up in the live ledger with the reason." |
| 1:55–2:20 | AG Grid: filter, inline-edit cap; AI report | "Every charge, top-up and denial is in the AG Grid ledger. I can tighten caps inline and read an AI-written spend report." |
| 2:20–2:40 | Click Refund; refund row appears | "When the job is done, one click refunds the unused credit to my PayPal." |
| 2:40–2:50 | README / GitHub | "AgentTab: trusted PayPal money, autonomous agents, AI-enforced rules. Open source, MIT." |
