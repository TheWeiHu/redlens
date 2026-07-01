.PHONY: check lint type test coverage install-hooks

# Mirror CI (.github/workflows/ci.yml) exactly. Run before every push.
check: lint type test

lint:
	ruff check .

type:
	mypy redlens

test:
	pytest -m "not integration"

coverage:
	pytest --cov --cov-report=term-missing

# One-time per clone: route git hooks at the tracked .githooks dir so that
# `git push` runs `make check` first (bypass with `git push --no-verify`).
install-hooks:
	git config core.hooksPath .githooks
	@echo "pre-push hook active -> runs 'make check' before each push"
