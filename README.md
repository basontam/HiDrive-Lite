# HiDrive-Lite

HiDrive-Lite is a self-hosted personal media-resource library. It combines a
searchable local index with optional TMDB/IMDb/TVmaze metadata and a guarded
115 one-click transfer flow. Other supported providers remain available as
open/copy actions.

The repository contains code and fictional demo data only. You bring your own
media links, provider accounts, API keys and storage configuration.

## What it does

* imports user-owned XLSX media lists and merges links by title and edition;
* serves a CJK/Latin search, filters, recommendations and detail pages;
* caches and rate-limits optional TMDB enrichment and stores source ratings;
* encrypts application secrets and library links at rest with a Fernet key;
* validates a 115 session, resolves a target directory server-side, and submits
  one guarded share receive request;
* supports QR-based 115 re-authorisation without returning the Cookie;
* optionally browses OpenList/STRM paths without changing the underlying files.

The 115 web endpoints used for share receive and QR re-authorisation are not a
stable public API. Read `docs/115-integration.md` and verify the provider's
current terms before deploying the adapter.

## Quick start

```bash
git clone <repository-url>
cd hidrive-lite
cp .env.example .env
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/bootstrap.py --demo
python app.py
```

Open <http://127.0.0.1:12367>. The demo contains fictional titles and fake
share codes; it never calls an external provider.

For a container:

```bash
export HIDRIVE_UID="$(id -u)"
export HIDRIVE_GID="$(id -g)"
docker compose up --build -d
```

See `docs/quickstart.md` for production and reverse-proxy setup.

## Configuration and data

Local mode is the default. Set `HIDRIVE_AUTH_MODE=access` only when a compatible
reverse proxy is already configured. Runtime paths and non-secret settings are
documented in `docs/configuration.md`.

Provider credentials are entered through Settings and encrypted in:

```text
HIDRIVE_DATA_DIR/hidrive.db
HIDRIVE_DATA_DIR/media-library.db
```

The Fernet master key is outside Git at `HIDRIVE_MASTER_KEY_FILE`. Keep the key
and encrypted databases in separate protected backups. Never commit `data/`,
`secrets/`, databases, workbooks, provider Cookies, access codes or logs.

The public status API is redacted and the optional HDHive compatibility routes
do not reflect upstream share URLs or access codes. See
`docs/configuration.md` for Docker UID/GID and OpenList connectivity details.

## Importing your library

The importer is dry-run by default and accepts source files outside the
repository:

```bash
.venv/bin/python scripts/import_media_library.py \
  --source-dir /path/to/your/workbooks \
  --output .local-index \
  --report build/library-import-report.json

.venv/bin/python scripts/import_media_library.py \
  --source-dir /path/to/your/workbooks \
  --output .local-index --write

python app.py --library-install .local-index/library-bundle.sqlite
```

Reports contain counts and hashes only. Import details and the 115 flow are in
`docs/media-library-import.md` and `docs/115-integration.md`.

## Development

```bash
scripts/run_tests.sh
python scripts/scan_secrets.py
```

Tests block real outbound HTTP and use fictional links. CI should run the same
tests, syntax check, secret scan and a demo bootstrap without provider secrets.

## License and notices

Code is released under the MIT License. See `NOTICE` and
`docs/third-party-notices.md` for upstream attribution and vendored icon
licenses. Provider names and marks do not imply affiliation or endorsement.
