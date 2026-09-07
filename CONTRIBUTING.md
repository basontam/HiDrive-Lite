# Contributing

## Development setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/python scripts/bootstrap.py --demo
```

Run the local checks with:

```bash
scripts/run_tests.sh
```

All network calls in the test suite are mocked. Do not use a personal HDHive,
TMDB, OpenList, or 115 credential in tests, fixtures, screenshots, or commit
messages.

## Pull requests

Keep changes focused, explain the user-visible effect, and add a regression
test for behaviour changes. Provider integrations should fail closed, avoid
logging raw links or credentials, and preserve the server-side target-path
validation used by the 115 transfer flow.

Before opening a pull request, run the tests and the repository secret scan.
Never add generated databases, Excel files, matching exports, browser caches,
or release artifacts.
