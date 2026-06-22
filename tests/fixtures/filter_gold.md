# Topic relevance filter — gold set & eval

`filter_gold.csv` (269 rows: `brand, gold, id, subreddit, title, selftext`) is the
hand-labeled gold for the relevance filter: ~270 Reddit posts across 9 ambiguous
brands (**126 on-topic / 143 off-topic**), built once via the real `track` discovery
(PullPush + name search) so each brand carries both its product sense and its
everyday/other senses. `gold` is `1` = on-topic, `0` = off-topic.

`scripts/filter_eval.py` scores the **production** filter path (`prompts/filter.txt`
+ `redlens.filter`) over it, sweeping the 3 JSON key orderings × N runs:

```
python scripts/filter_eval.py --runs 10 --batch 50            # infer only
python scripts/filter_eval.py --runs 10 --batch 50 --about    # with descriptions
```

## Headline result

A one-line `--about` description is the dominant lever (shipped ordering, whole-topic batch):

| | recall | junk caught | agreement |
|---|:---:|:---:|:---:|
| infer only | 0.98 | 0.62 | 0.79 |
| **with `--about`** | 0.98 | **0.91** | **0.95** |

`--about` ≈ +0.30 junk-caught / +0.16 agreement (clears the ≥0.90 bar), dwarfing key
order (±0.06) and batch size (±0.13); recall stays ~0.98. Per-brand it fixes the
leaks (`monday` 0.12→0.96 weekday, `arc` 0.34→0.80 the game). So the filter is
recall-safe but leaky when blind (~half the junk), and strong given a description.
That's why `track` prompts for `--about` and `FILTER_BATCH` defaults large.
