# Configuration

Copy `.env.example` to `.env` for a local installation. The values below are
non-secret runtime settings; provider credentials are entered through Settings
and encrypted in `HIDRIVE_DATA_DIR`.

| Variable | Local default | Purpose |
|---|---|---|
| `HIDRIVE_AUTH_MODE` | `local` | `local`, `disabled` (tests), or `access` |
| `HIDRIVE_PUBLIC_ORIGIN` | `http://127.0.0.1:12367` | Origin used for OAuth and CSRF |
| `HIDRIVE_DATA_DIR` | `./data` | Encrypted application and library databases |
| `HIDRIVE_MASTER_KEY_FILE` | `./secrets/master.key` | Fernet key, mode 0600 |
| `HIDRIVE_BIND` / `HIDRIVE_PORT` | `127.0.0.1` / `12367` | Development listener |
| `OPENLIST_URL` | `http://127.0.0.1:5244` | Optional OpenList API; in Docker use a reachable host/LAN URL |
| `OPENLIST_115PAN_PATH` | `/115pan` | Allowed 115 target root |
| `OPENLIST_115STRM_PATH` | `/115strm` | Read-only STRM path |
| `STRM_ROOT` | `./data/strm` | Optional local STRM root |
| `TMDB_DAILY_BUDGET` | `300` | Upper cap for TMDB requests |
| `HIDRIVE_UID` / `HIDRIVE_GID` | `1000` / `1000` | Compose bind-mount process identity; set to the owner of `./data` and `./secrets` |

`app.py` loads a repository-local `.env` on startup; explicitly exported
process variables take precedence. Never put these credential values in `.env`,
systemd units, or CI logs:

* `HDHIVE_APP_SECRET` and HDHive OAuth access/refresh tokens in a production
  `access` deployment (local mode may use the environment fallback for a
  private development instance);
* `TMDB_API_KEY`;
* 115 web Cookie and 115 Open Platform access/refresh tokens;
* OpenList API tokens;
* reverse-proxy identity secrets.

Use the first-run bootstrap to create the master key, then use the Settings
page to save provider credentials. The key is not recoverable from the database;
back it up through the operator's private secret-management process.

The public `/api/status` response is deliberately redacted: it reports
configured/healthy booleans and the logical OpenList mount labels, but does not
echo provider URLs, absolute filesystem paths, target PIDs/CIDs, Cookies, or
tokens. The browser always submits a logical target path; the server resolves
the real 115 CID during transfer. The HDHive compatibility endpoints also
sanitize free-text fields and OAuth errors before returning them.
