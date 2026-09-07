# Vendored icon assets

The `fa-*` and `si-*` symbols in `static/icons.svg` are a small,
version-pinned subset of Font Awesome Free and Simple Icons. They are vendored
as SVG path data so a fresh installation works without a CDN, build step, or
third-party request at render time.

## Font Awesome Free 6.7.2

Source package: `@fortawesome/fontawesome-free@6.7.2` (the symbols were
obtained from its published SVG files during implementation).

Licence: **CC BY 4.0** for the icon artwork. Attribution: “Icons by Font
Awesome (https://fontawesome.com), CC BY 4.0”. The project uses only the
embedded SVG path data.

| Symbol | Meaning in the UI |
| --- | --- |
| `fa-photo-film` | Resolution chip (`4K`, `1080p`, `720p`, `SD`) |
| `fa-circle-half-stroke` | Dynamic-range chip, except Dolby Vision |
| `fa-film` | Streaming/broadcast source (`WEB-DL`, `WEBRip`, `HDTV`) |
| `fa-compact-disc` | Physical source (`BluRay`, `BDRemux`, `BDRip`) |

## Simple Icons 16.29.0

Source package: `simple-icons@16.29.0` (the symbols were obtained from its
published SVG files during implementation).

Licence: **CC0 1.0** for the pictogram/markup. Brand names and marks remain
the property of their respective owners; Dolby, TMDB and IMDb are trademarks
of their respective holders. The icons identify data providers or media
specifications and do not imply sponsorship or endorsement.

| Symbol | Meaning in the UI |
| --- | --- |
| `si-dolby` | Dolby Vision dynamic-range chip |
| `si-tmdb` | TMDB rating source |
| `si-imdb` | IMDb rating source |

TVmaze has no vendored brand icon in this release; its rating uses a plain
text `TVmaze` label. To update an icon version, fetch the new package release,
review the path/viewBox diff, update this table and the version note in
`static/icons.svg`, and retain the corresponding licence notice.
