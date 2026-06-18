# Security Policy

## Supported versions

redlens is pre-1.0; only the latest release on
[PyPI](https://pypi.org/project/redlens/) receives fixes.

## Reporting a vulnerability

Please report security issues privately via GitHub's
[private vulnerability reporting](https://github.com/TheWeiHu/redlens/security/advisories/new)
rather than opening a public issue. You can expect an initial response within a
few days.

## Scope notes

redlens only reads **public** Reddit data and writes to a local SQLite file you
own — it stores no credentials of its own. An optional bring-your-own LLM API
key (for `summarize`/`track`) is read from your environment or config file and
is never written to the database or transmitted anywhere except your chosen LLM
provider.
