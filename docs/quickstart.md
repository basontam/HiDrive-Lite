# Quickstart

The shortest safe local setup is:

```bash
git clone <repository-url>
cd hidrive-lite
cp .env.example .env
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/bootstrap.py --demo
python app.py
```

Open `http://127.0.0.1:12367`. The demo library contains fictional titles and
fake provider codes only. It is safe to delete `data/` and `secrets/` when
resetting a local installation.

## Docker

Create the key and data directories with the bootstrap command as the same
host user that will own the bind mounts. Then set the container UID/GID to
that owner before starting the stack:

```bash
export HIDRIVE_UID="$(id -u)"
export HIDRIVE_GID="$(id -g)"
docker compose up --build -d
```

The Compose file uses these numeric IDs so the non-root process can read the
0600 Fernet key and write the SQLite data. Do not run bootstrap as a different
user, and do not make `secrets/` world-readable. The image does not include an
OpenList server; set `OPENLIST_URL` to a reachable OpenList endpoint (for a
host service, Docker Desktop usually uses `http://host.docker.internal:5244`;
on Linux add a host-gateway mapping or use the host's LAN address).

The container binds to loopback. Put a reverse proxy in front of it only after
changing `HIDRIVE_PUBLIC_ORIGIN` and selecting an authentication mode that the
proxy actually provides.

## Production checklist

1. Create a dedicated OS user and private data/key directories.
2. Install pinned production requirements in a virtual environment.
3. Copy `deploy/systemd/hidrive-lite.service.example` and
   `deploy/systemd/hidrive-lite.env.example` to the paths named by the unit,
   replacing only the documented generic paths.
4. Configure OpenList and 115 using the integration guides.
5. Enter TMDB/115 values in Settings and run one mocked/local smoke test
   before enabling background enrichment. HDHive is an optional legacy backend
   adapter: its `HDHIVE_APP_SECRET`/`HDHIVE_CLIENT_ID` environment fallback is
   intended only for local mode; access mode requires encrypted provisioning,
   and this public UI intentionally has no HDHive credential form.
6. Back up the encrypted databases and master key independently.
