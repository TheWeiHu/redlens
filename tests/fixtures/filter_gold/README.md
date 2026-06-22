# Gold set + eval for the topic relevance filter

Proves how well the cheap model (gpt-4o-mini) separates real brand mentions from
coincidental keyword matches, on real Reddit data, through the **production prompt
+ parser** (`scripts/filter_eval.py`, which reuses `redlens/prompts/filter.txt`
and `redlens.filter`).

## The gold (9 brands, balanced)

Built via the **real `track` discovery** — PullPush/Pushshift global search + arctic
name search (`pull --discover`), not hand-picked subreddits — so every brand carries
both its product sense (positives) and its everyday/other senses (negatives). Each
`<brand>.jsonl` row: id, subreddit, title, selftext, and a hand label `gold`
(`true` = on-topic / `false` = off-topic).

**126 on-topic / 143 off-topic, 269 rows.** Balance matters: an earlier all-product-
subreddit set was 87% on-topic, which let a do-nothing "keep everything" filter score
0.87 agreement for free and hid the real weakness. (`conductor` was dropped — multiple
real dev tools share the name, so it can't be labeled cleanly.)

## Reproduce

```
python scripts/filter_eval.py pull --discover     # freeze raw matches (label gold)
python scripts/filter_eval.py grid --runs 10 --batch 50            # infer-only
python scripts/filter_eval.py grid --runs 10 --batch 50 --about    # with descriptions
```

`grid` sweeps 3 JSON key orderings × N runs and writes per-(order,run,brand) CSV.
`grid_results.csv` here holds the headline comparison: batch-50, 10 runs, 3 orderings,
**`about` on vs off** (the `about` column).

## Headline result — a one-line `--about` description is the dominant lever

Shipped ordering (`reason`→`verdict`), whole-topic batch:

| | recall (real kept) | junk caught | agreement |
|---|:---:|:---:|:---:|
| infer only (no `--about`) | 0.98 | 0.62 | 0.79 |
| **with `--about`** | 0.98 | **0.91** | **0.95** |

- `--about` ≈ **+0.30 junk-caught / +0.16 agreement** — clears the ≥0.90 bar. It
  dwarfs the other levers measured: JSON key order ≈ ±0.06, batch size ≈ ±0.13.
- Recall is unaffected (~0.98) — the filter stays recall-safe either way.
- Per-brand it fixes exactly the leaks: `monday` 0.12→0.96 (weekday), `arc`
  0.34→0.80 (the game), `bolt` 0.70→1.00, `square` 0.86→0.99.

**Takeaway:** run blind, the filter is recall-safe but leaky (catches ~half the
junk). Given a 10-second brand description it's strong (~91% junk caught, ~98%
recall). That's why `track` now prompts for `--about` and defaults to a larger batch.
