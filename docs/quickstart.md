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

After creating the key and data directories with the bootstrap command:

```bash
docker compose up --build -d
```

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
5. Enter HDHive/TMDB/115 values in Settings and run one mocked/local smoke test
   before enabling background enrichment.
6. Back up the encrypted databases and master key independently.
