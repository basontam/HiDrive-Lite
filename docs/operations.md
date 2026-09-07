# Operations and recovery

## Health

`/healthz` is a process-level probe. In `local` mode it is directly usable from
the local listener. In `access` mode it is intentionally protected by the
reverse-proxy authentication layer; use a probe that supplies the proxy's
identity assertion or probe the local listener directly.

## Backups

Back up the encrypted databases and the Fernet master key as separate protected
artifacts. A database backup without its key is unusable; a key backup without
the database does not expose the database contents by itself.

For a library update, build a new plaintext bundle, inspect the redacted import
report, install it atomically, and retain the previous encrypted database until
the new instance passes health, search, detail and one mocked transfer checks.

## Troubleshooting

Provider failures should be diagnosed using status/reason codes and request
timestamps. Do not ask users to paste Cookies, access codes or full share URLs
into issue reports. Rotate credentials before attaching any log that may have
contained sensitive material.
