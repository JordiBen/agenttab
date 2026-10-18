# Held-out set

- **Cases:** `eval/heldout.json`, 40 purchases (26 attacks, 14 legitimate). They were written by a separate reviewer, not the builder, and kept out of the repo until they were run. The builder never saw them before the run.
- **Run:** exactly once, on 2026-10-18 (04:12 to 04:13 UTC), on commit `1ed93eb` with a clean tree (`git_dirty: false`). Nothing was changed before or after it: no cases, no guard prompt, no code.
- **Command** (the reviewer's script `run_heldout.py`, run from the repo root with the script and output kept outside the tree):
  `python run_heldout.py heldout.json --modes none,keyword,ai --out heldout-results.json`
- **Model:** `claude-haiku-5-5` (GUARD_PROVIDER=anthropic), for `ai` mode only. `none` and `keyword` make no AI calls.
- **Results:** `eval/heldout-results.json`, the script's output byte for byte. That output has no git fields, so the sha, the clean-tree check, timestamps and file hashes are in `eval/heldout-run.json`.

| Mode | Attacks stopped | Legit buys handled as labeled | Wrongly blocked |
|---|---|---|---|
| none | 0 / 26 | 13 / 14 | 0 |
| keyword | 19 / 26 | 14 / 14 | 0 |
| ai | 26 / 26 | 14 / 14 | 0 |

This set is small (40 cases), so read the numbers as indicative, not as a benchmark.
