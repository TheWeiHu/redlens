## What & why

<!-- One or two sentences: what this changes and the motivation. Link issues with "Fixes #N". -->

## Demo — terminal log of the feature in action

<!--
REQUIRED for any behavior change. Paste a real terminal transcript showing the
feature doing what it's supposed to — the command(s) you ran and their output
(a track/sync run, a rendered page, a new flag, the bug now fixed, etc.).
The point is evidence it works, not a description that it should.

For docs-only / pure-refactor PRs with no observable behavior change, write
"N/A — no behavior change" and say why.
-->

```
$ # paste the command(s) and output here
```

## Checks

- [ ] `pytest` (and `pytest -m integration` if touching the arctic client)
- [ ] `ruff check .`
- [ ] `mypy redlens`
