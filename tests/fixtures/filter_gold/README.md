# Frozen gold set for the topic relevance filter

`scripts/filter_eval.py pull` writes one `<brand>.jsonl` here — every raw arctic
match for that brand, frozen so the eval is reproducible and arctic is hit once.
Each line:

```json
{"brand": "bolt", "keywords": ["bolt"], "about": "",
 "id": "<post id>", "subreddit": "DIY", "title": "...", "selftext": "...",
 "gold": null}
```

`gold` is the **hand label**, filled by Opus 4.8 after the pull:

- `true`  — the post genuinely references the intended subject (on-topic),
- `false` — the matched word is a different/coincidental sense (false positive).

Then `scripts/filter_eval.py score` runs the production gpt-4o-mini filter path
over the labeled rows and reports recall / precision-on-junk / agreement against
the bar (≥0.95 / ≥0.85 / ≥0.90). Rows still `null` are excluded from the metrics.

**Status:** harness committed; the 10-brand pull + Opus labeling (which needs a
live arctic key and is interactive) has not been run yet, so no labeled `*.jsonl`
is committed. Until it is, `track`'s relevance filter is unverified against the
bar — see the PR body.
