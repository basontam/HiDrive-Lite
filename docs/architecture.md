# Architecture

HiDrive-Lite is a self-hosted Flask application for a personal media-resource
library. It keeps metadata/search separate from provider actions:

```text
user-owned CSV/XLSX
        -> normalize and deduplicate
        -> encrypted SQLite library
        -> search / filters / detail page
        -> 115: validate session -> resolve target -> snap -> receive
           other providers: open or copy on demand
```

## Components

* `app.py` owns HTTP routes, authentication, CSRF, encrypted settings, the
  HDHive OAuth/check-in flow, and the 115 transfer/re-authorisation flow.
* `library_normalize.py` parses titles, years, providers, editions and media
  specifications without network access.
* `library_store.py` owns the SQLite schema, encryption-at-install, backups and
  live-link semantics.
* `library_search.py` builds and queries the offline CJK/Latin search index.
* `library_tmdb.py` provides cached, rate-limited TMDB matching/enrichment and
  optional IMDb/TVmaze hints.
* `sync_openlist_115.py` is an optional compatibility helper for installations
  where OpenList owns the 115 Open Platform refresh cycle.

The application can run in `local` mode for a private LAN or development
instance. `access` mode validates a reverse-proxy identity assertion and is
intended for an operator who has configured that proxy separately.

## Provider boundary

The browser never receives a 115 URL or access code for a transfer. It sends an
opaque `resource_link_id`; the server decrypts and parses the link just in time,
validates the target directory, and records a redacted audit result. Non-115
providers use the existing open/copy path and are not sent to the 115 receive
endpoint.
