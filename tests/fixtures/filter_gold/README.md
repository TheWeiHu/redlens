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

Clean factorial — **1 gold set × 3 field orderings × 10 runs** (30 passes),
reproduce with `filter_eval.py grid --runs 10 --by-brand` (raw rows in
`grid_results.csv`). The ordering factor is where the `relevant` boolean sits
relative to its free-text `reason`:

| ordering (relevant @ pos) | recall | precision-on-junk | agreement | overall |
|---------------------------|-------:|------------------:|----------:|---------|
| relevant-first (1) | 0.916 ± 0.013 | 0.416 ± 0.045 | 0.850 ± 0.012 | **FAIL** |
| **reason-first (2, shipped)** | **0.999 ± 0.002** | **0.976 ± 0.050** | **0.911 ± 0.012** | **PASS** |
| relevant-last (3) | 0.995 ± 0.012 | 0.901 (noisy) | 0.883 ± 0.013 | FAIL |

Findings:
- **Ordering is the dominant knob.** Writing `reason` before `relevant` (a
  chain-of-thought nudge) is decisive: agreement 0.850 → 0.911, recall → ~1.0. It
  is *not* monotonic — putting `confidence` between reason and relevant
  (relevant-last) is worse than keeping them adjacent, so `prompts/filter.txt`
  ships reason-first.
- **reason-first clears the ≥0.90 bar on the mean** (recall 0.999 / precision 0.976
  / agreement 0.911), though agreement is marginal — 3 of 10 runs dip to ~0.894.
- **But the overall pass masks the hard homonyms.** Pooled agreement is dominated
  by the easy brands (arc/dove/notion/corona/linear all ~1.0). Per-brand,
  reason-first still fails `conductor` (0.00, keeps the orchestrator metaphor),
  `monday` (0.31, keeps "Monday morning" weekday posts), and `shell` (0.50). Those
  are the documented brands that need the optional `--about` hint.

So: recall-safe and over the overall bar with reason-first, but the homonym brands
(`monday`/`conductor`/`shell`) remain below it without `--about` — exactly the
evidence the task said would justify that flag.
