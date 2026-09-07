#!/usr/bin/env python3
"""Initialize a local HiDrive-Lite data directory.

The optional ``--demo`` build contains fictional titles and fake provider
codes. It never contacts HDHive, TMDB, OpenList or 115.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _write_key(path: Path) -> None:
    if path.exists():
        try:
            Fernet(path.read_bytes().strip())
        except Exception as exc:
            raise SystemExit(f"invalid Fernet key: {path} ({type(exc).__name__})") from exc
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(Fernet.generate_key())
    os.chmod(path, 0o600)


def _build_demo_bundle(bundle: Path) -> None:
    import library_normalize
    import library_search
    import library_store
    import library_tmdb

    store = library_store.LibraryStore(bundle)
    store.create_schema()
    with store.connect() as conn:
        library_tmdb.ensure_tables(conn)

    now = int(time.time())
    media = [
        library_store.MediaRecord(
            media_identity="demo:movie:1", media_type="movie", title_zh="示例电影",
            title_original="Demo Movie", year=2024, search_key="示例电影",
            overview="这是一个虚构条目，仅用于验证搜索、详情和转存界面。",
            match_status="exact", tmdb_id=900001,
        ),
        library_store.MediaRecord(
            media_identity="demo:tv:1", media_type="tv", title_zh="示例剧集",
            title_original="Demo Series", year=2023, search_key="示例剧集",
            overview="这是一个虚构剧集，不包含真实资源。", match_status="unmatched",
        ),
    ]
    media_ids = {record.media_identity: store.upsert_media(record) for record in media}
    groups = [
        library_store.GroupRecord(
            media_id=media_ids["demo:movie:1"], edition_fingerprint="demo-movie-4k",
            display_title="2160p WEB-DL · Dolby Vision", quality="2160p",
            source_type="webdl", hdr="Dolby Vision",
        ),
        library_store.GroupRecord(
            media_id=media_ids["demo:tv:1"], edition_fingerprint="demo-tv-s01",
            display_title="S01 · 1080p WEB-DL", season_from=1, season_to=1,
            quality="1080p", source_type="webdl",
        ),
    ]
    group_ids = [store.upsert_group(group) for group in groups]
    links = [
        ("demo-link-115", group_ids[0], "115", "https://115.com/s/demo-share-001", "demo"),
        ("demo-link-quark", group_ids[0], "quark", "https://pan.quark.cn/s/demo-share-002", None),
        ("demo-link-tianyicloud", group_ids[1], "tianyicloud", "https://cloud.189.cn/t/demo-share-003", None),
    ]
    for public_id, group_id, provider, url, code in links:
        store.upsert_link(
            library_store.LinkRecord(
                public_id=public_id, group_id=group_id, provider=provider,
                canonical_url_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
                url_label=f"{provider} 分享 · demo", url_plain=url,
                access_code_plain=code, has_access_code=int(bool(code)),
                remark="仅用于本地演示", created_at_source=now,
            )
        )
    store.recount()
    library_search.build_index(store)
    store.meta_set("normalize_version", library_normalize.NORMALIZE_VERSION)
    store.meta_set("built_at", str(now))
    store.meta_set("source_hashes", json.dumps({"demo": "fictional"}))
    store.meta_set("encrypted", "0")

    # LibraryStore writes in WAL mode.  The install routine intentionally
    # copies only the database file (not transient ``-wal``/``-shm``
    # sidecars), and read-only SQLite connections on some platforms cannot
    # open a WAL database without those sidecars.  Use SQLite's backup API to
    # materialize a portable single-file DELETE-journal copy before handing
    # the fictional bundle to ``install_bundle``.
    portable = bundle.with_name(bundle.name + ".portable")
    for path in (portable, portable.with_name(portable.name + "-wal"), portable.with_name(portable.name + "-shm")):
        path.unlink(missing_ok=True)
    source = sqlite3.connect(bundle)
    destination = sqlite3.connect(portable)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    # ``Connection.backup`` preserves the source journal-mode metadata, so
    # explicitly switch the standalone copy to DELETE after all source
    # handles are closed.  This makes the following read-only validation
    # portable on macOS as well as Linux.
    connection = sqlite3.connect(portable, timeout=10)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.commit()
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        bundle.with_name(bundle.name + suffix).unlink(missing_ok=True)
        portable.with_name(portable.name + suffix).unlink(missing_ok=True)
    os.replace(portable, bundle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--key-file", type=Path, default=Path("secrets/master.key"))
    parser.add_argument("--demo", action="store_true", help="install the fictional demo media library")
    args = parser.parse_args()

    data_dir = args.data_dir.expanduser().resolve()
    key_file = args.key_file.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "strm").mkdir(parents=True, exist_ok=True)
    _write_key(key_file)

    os.environ.setdefault("HIDRIVE_AUTH_MODE", "local")
    os.environ.setdefault("HIDRIVE_DATA_DIR", str(data_dir))
    os.environ.setdefault("HIDRIVE_MASTER_KEY_FILE", str(key_file))
    os.environ.setdefault("HIDRIVE_PUBLIC_ORIGIN", "http://127.0.0.1:12367")
    os.environ.setdefault("STRM_ROOT", str(data_dir / "strm"))
    os.environ.setdefault("OPENLIST_DB", str(data_dir / "openlist.db"))
    os.environ.setdefault("LIBRARY_ENRICH_AUTOSTART", "0")

    import app

    app.init_db()
    installed_demo = False
    if args.demo:
        import library_store

        bundle = data_dir / ".demo-library.sqlite"
        try:
            _build_demo_bundle(bundle)
            target = data_dir / "media-library.db"
            result = library_store.install_bundle(bundle, target, app.load_fernet())
            for suffix in (".tmp-wal", ".tmp-shm"):
                target.with_name(target.name + suffix).unlink(missing_ok=True)
            installed_demo = True
        finally:
            bundle.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                bundle.with_name(bundle.name + suffix).unlink(missing_ok=True)
    else:
        result = None

    payload = {
        "ok": True,
        "data_dir": str(data_dir),
        "key_file": str(key_file),
        "demo_installed": installed_demo,
    }
    if result is not None:
        payload["library"] = {key: result[key] for key in ("media_total", "groups_total", "links_total")}
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
