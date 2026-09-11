# Contributing

Thanks for your interest in contributing. Small guidance:

- Fork and branch from `main` using a descriptive branch name.
- Run tests and lint before submitting a PR:

```bash
pip install -r requirements-dev.txt
python -m pytest
python -m ruff check src tests
```

- Open a PR targeting `main`. CI checks must pass before merge.
- For security-related fixes, avoid referencing secrets in PRs; open the issue privately if needed.
