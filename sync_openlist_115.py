"""Optionally synchronize OpenList credentials into HiDrive-Lite's encrypted store.

OpenList owns the 115 Open Platform refresh cycle.  HiDrive-Lite receives a
copy of the current access/refresh pair for read-only CID resolution and never
refreshes that pair itself, avoiding a one-use refresh-token race with
OpenList.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet


OPENLIST_DB = Path(os.getenv("OPENLIST_DB", "./data/openlist.db"))
HIDRIVE_DB = Path(os.getenv("HIDRIVE_DB", "./data/hidrive.db"))
MASTER_KEY = Path(os.getenv("HIDRIVE_MASTER_KEY_FILE", "./secrets/master.key"))
OPENLIST_115PAN_PATH = os.getenv("OPENLIST_115PAN_PATH", "/115pan").strip() or "/115pan"


def now() -> int:
    return int(time.time())


def main() -> int:
    if not OPENLIST_DB.exists() or not MASTER_KEY.exists():
        raise RuntimeError("OpenList database or HiDrive master key is missing")
    with sqlite3.connect(f"file:{OPENLIST_DB}?mode=ro", uri=True, timeout=10) as source:
        token_row = source.execute("SELECT value FROM x_setting_items WHERE key='token'").fetchone()
        storage_row = source.execute(
            "SELECT addition FROM x_storages WHERE mount_path=? AND driver='115 Open'",
            (OPENLIST_115PAN_PATH,),
        ).fetchone()
    if not token_row or not str(token_row[0]).strip():
        raise RuntimeError("OpenList API token is missing")
    if not storage_row:
        raise RuntimeError("OpenList /115pan storage is missing")
    addition = json.loads(storage_row[0])
    access = str(addition.get("access_token") or "").strip()
    refresh = str(addition.get("refresh_token") or "").strip()
    if not access or not refresh:
        raise RuntimeError("OpenList 115 Open Platform tokens are missing")
    key = MASTER_KEY.read_bytes().strip()
    fernet = Fernet(key)
    HIDRIVE_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(HIDRIVE_DB, timeout=10) as target:
        target.executescript(
            """
            CREATE TABLE IF NOT EXISTS secrets (
                name TEXT PRIMARY KEY,
                value BLOB NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        timestamp = now()
        for name, value in (
            ("openlist_token", str(token_row[0]).strip()),
            ("115_open_access_token", access),
            ("115_open_refresh_token", refresh),
        ):
            target.execute(
                "INSERT INTO secrets(name,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (name, fernet.encrypt(value.encode("utf-8")), timestamp),
            )
        root_cid = str(addition.get("root_folder_id") or "0").strip() or "0"
        target.execute(
            "INSERT INTO settings(name,value,updated_at) VALUES('115_open_root_cid',?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (root_cid, timestamp),
        )
    print("HiDrive-Lite credentials synchronized: OpenList token, 115 Open Platform pair, root CID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
