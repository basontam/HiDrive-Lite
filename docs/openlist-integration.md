# OpenList integration

OpenList is optional. The media library, search and metadata pages work without
it; only directory browsing and automatic target-directory resolution require
an OpenList-compatible setup.

Preferred setup:

1. Configure an OpenList storage with a 115 Open Platform driver.
2. Expose the OpenList API at `OPENLIST_URL`.
3. Enter the OpenList token in HiDrive-Lite Settings.
4. Set `OPENLIST_115PAN_PATH` to the allowed target root.
5. Verify that listing the root and resolving one child directory work before
   enabling transfer.

`sync_openlist_115.py` is a compatibility helper for an installation where
OpenList owns the 115 token refresh cycle and exposes a local SQLite database.
It reads that database read-only and copies only encrypted token values into
HiDrive-Lite. It is optional and must be run with explicit `OPENLIST_DB`,
`HIDRIVE_DB`, and `HIDRIVE_MASTER_KEY_FILE` paths.

The public defaults point to `./data/openlist.db` and do not assume a host
filesystem layout. Do not mount an unrelated OpenList database into a clone.
