# Frozen gold set for the topic relevance filter

`scripts/filter_eval.py pull` writes one `<brand>.jsonl` here — every raw arctic
match for that brand, frozen so the eval is reproducible and arctic is hit once.
Each line:

```json
{"brand": "bolt", "keywords": ["bolt"], "about": "",
 "id": "<post id>", "subreddit": "DIY", "title": "...", "selftext": "...",
 "gold": true}
```

`gold` is the hand label (Opus 4.8): `true` = the post references the intended
subject (the sense implied by the brand's subreddits), `false` = the matched word
is a different/coincidental sense (e.g. "Monday morning" for monday.com, a
lightning-bolt icon for a bolt fastener, "shell company" for Shell).

`scripts/filter_eval.py score` runs the production gpt-4o-mini filter path over
the labeled rows and reports recall / precision-on-junk / agreement against the
bar (≥0.95 / ≥0.85 / ≥0.90). `--runs N` reports mean ± stdev; `--order
relevant-first` rewrites the prompt example to the pre-tuning field order.

## Committed gold (10 brands, 226 rows, last 30 days)

| brand | rows | on-topic | junk (false positive) |
|-------|-----:|---------:|----------------------:|
| arc | 40 | 40 | 0 |
| bolt | 32 | 30 | 2 |
| conductor | 2 | 0 | 2 |
| corona | 5 | 5 | 0 |
| dove | 40 | 40 | 0 |
| linear | 7 | 7 | 0 |
| monday | 16 | 5 | 11 |
| notion | 40 | 40 | 0 |
| shell | 6 | 3 | 3 |
| square | 38 | 27 | 11 |
| **total** | **226** | **197** | **29** |

## Measured result — gpt-4o-mini does NOT clear the bar (pure inference)

Run live on a clean box (`filter_eval.py score --runs 3`):

| prompt order | recall | precision-on-junk | agreement | verdict |
|--------------|-------:|------------------:|----------:|---------|
| relevant-first (old) | 0.94 ± 0.02 | 0.51 ± 0.10 | 0.87 ± 0.02 | FAIL |
| reason-first (shipped) | 0.99 ± 0.01 | ~0.59 (noisy) | 0.89 ± 0.02 | FAIL |

Findings:
- **Field ordering matters a lot.** Asking for `reason` before `relevant` (a
  chain-of-thought nudge) lifts recall 0.94 → ~1.0 — so `prompts/filter.txt` ships
  reason-first.
- **Run-to-run variance is real** (the small 29-item junk set makes
  precision-on-junk swing 0.41–1.0 across identical prompts).
- **The hard cases inference can't crack:** `monday` weekday mentions (model keeps
  ~10/11 "Monday morning" posts) and `conductor` orchestrator-metaphor (0/2). This
  is the evidence that justifies the optional `--about` sense hint.

So the filter ships **unverified against the ≥0.90 bar**: it is recall-safe
(reason-first ≈ 1.0, so real mentions are rarely hidden) but does not yet hit the
precision/agreement bar on the hardest homonyms without an `--about` hint.
