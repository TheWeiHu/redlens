# Contributing

Thanks for your interest in redlens. It's a small, simplicity-first tool — the
bar for changes is "does this make the core job (archive a Reddit user's public
history into a local SQLite DB you own, and analyze it) cleaner?"

## Development

```bash
pip install -e ".[dev]"   # install with dev extras
make install-hooks        # one-time: pre-push runs `make check` automatically
make check                # the exact checks CI runs (ruff + mypy + pytest)
```

`make check` mirrors CI (`.github/workflows/ci.yml`) one-for-one: `ruff check`,
`mypy --strict`, then `pytest -m "not integration"`. Run it before pushing —
`make install-hooks` wires it to a `git push` pre-push hook so a red build never
reaches GitHub (bypass with `git push --no-verify`). On `main`, the `ci-gate`
status check must pass before a PR can merge. Tests are offline by default — the
`integration` marker (network/arctic) is deselected unless you opt in with
`pytest -m integration`.

See [DESIGN.md](DESIGN.md) for the architecture, module map, and design
principles before making a non-trivial change.

## Pull requests

- Branch, make the change, open a PR against `main`.
- One coherent change per PR; prefer the smallest slice that stands on its own.
- Note user-facing changes in `CHANGELOG.md` under `## [Unreleased]`.

## Versioning

redlens follows [Semantic Versioning](https://semver.org/). The version has a
**single source of truth**: the `version` field in `pyproject.toml`. There is no
`VERSION` file — do not add one.

## Releasing

Releases are tag-driven and publish to PyPI via trusted publishing (OIDC — no
token secrets). To cut version `X.Y.Z`:

1. Bump `version = "X.Y.Z"` in `pyproject.toml` (semver).
2. Move the `## [Unreleased]` notes in `CHANGELOG.md` under a new
   `## [X.Y.Z]` heading.
3. Commit and merge to `main`.
4. Tag and push:
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

Pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds the
sdist + wheel and publishes them to [PyPI](https://pypi.org/project/redlens/).

> One-time PyPI setup (already done for this repo): add a "pending publisher"
> on pypi.org pointing at workflow `release.yml` and environment `pypi`.

## License

By contributing you agree your contributions are licensed under the project's
[MIT License](LICENSE).
