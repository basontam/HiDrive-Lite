# Data model

The application uses two encrypted SQLite databases under `HIDRIVE_DATA_DIR`:

* `hidrive.db` stores settings, encrypted secrets, OAuth state/tokens,
  re-authorisation challenges, check-in history and redacted audit events;
* `media-library.db` stores media identities, resource groups, provider links,
  provenance, search indexes, match hints, metadata/ratings and link-check
  status.

The library hierarchy is:

```text
media (one title)
  -> resource_group (one edition / specification)
       -> resource_link (one provider URL)
```

URLs and access codes may exist as plaintext only in a transient import bundle.
They are encrypted during installation and the plaintext bundle should be
deleted immediately afterwards. Link-check rows contain hashes, status codes
and timestamps, never response bodies or URLs.

Database files, backups, WAL/SHM sidecars and master keys are runtime data and
are always ignored by Git.
