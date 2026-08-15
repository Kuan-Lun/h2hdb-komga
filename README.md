# H2HDB-Komga

## Description

`H2HDB-Komga` synchronizes a Komga library from the published catalog
projection in [`H2HDB`](https://github.com/Kuan-Lun/h2hdb). It matches Komga
book names to catalog publications and maps publication title, summary,
release date, every non-empty H2H gallery tag pair, and GID into Komga
metadata. Gallery tag pairs retain their original role/value meaning as Komga
authors. OPDS contributors, including the upload account, are not copied to
Komga, and this adapter does not patch Komga's `tags` field. When the original
gallery title is blank, the title field is also omitted so Komga keeps its
existing display title.

Published friendly artifact names are matched directly, whether Komga includes
the `.cbz` suffix or not. A physical `gid-sha256.cbz` storage name, a pure GID,
and a friendly name ending in `[gid]` are resolved against the same pinned
catalog revision through the public `CatalogReader`. A revision change resets
the stability window, and completion performs a final revision check. This
adapter never reads `CatalogArtifact.location` or any core repository internals.
Books with no published match are left unchanged.

The H2HDB database is always opened in read-only mode. Startup performs a
schema compatibility check but never runs migrations; schema ownership stays
with H2HDB core.

---

## Installation and Usage

1. Install Python 3.14 or higher from
   [python.org](https://www.python.org/downloads/).
1. Install the required packages.

    ```bash
    pip install h2hdb-komga
    ```

1. Run the script.

    ```bash
    python -m h2hdb_komga \
      --komgaconfig [komga-config.json] \
      --h2hdbconfig [h2hdb-config.json]
    ```

### Config

#### komga-config.json

```json
{
    "base_url": "https://komga.example",
    "api_username": "${KOMGA_API_USERNAME}",
    "api_password": "${KOMGA_API_PASSWORD}",
    "library_id": "library-id",
    "trigger_scan": true
}
```

Set both credential variables in the process environment before starting the
command:

```bash
export KOMGA_API_USERNAME="admin@example.com"
export KOMGA_API_PASSWORD="secret"
```

`${ENV_NAME}` is resolved only when it is the complete JSON string value; it is
not substring interpolation. Resolution is recursive, missing variables fail
startup without exposing credential values, and resolved credentials must both
be non-empty strings. Literal `api_username` and `api_password` values remain
supported, but placeholders keep deployment secrets out of the JSON file.
Credential fields are also omitted from `KomgaConfig`'s representation so
ordinary diagnostic output does not reveal them.

`trigger_scan` defaults to `true`. Set it to `false` to skip requesting a
Komga scan/analyze and only reconcile metadata already visible in the library.
Afterward, the command polls every five seconds and rechecks every current
book. It exits only after book/series IDs and metadata remain stable for 30
seconds; the default timeout is one hour and can be changed with
`--timeout-seconds`. This observation window allows Komga's asynchronous
scan/analyze jobs and transient book fetch failures to become visible before
completion. Each HTTP request, PATCH verification, retry, and retry delay uses
the remaining cooperative budget. The CLI also runs the complete operation in
a disposable worker process and kills it at the wall-clock deadline, so a
slow-drip socket, blocked database gate, or executor shutdown cannot extend the
documented hard timeout indefinitely.

#### h2hdb-config.json

Use an H2HDB core configuration compatible with `h2hdb>=0.22.0.1,<0.23`. Any
configured database access mode is overridden to `read-only` by this CLI.
The core loader supports the same exact `${ENV_NAME}` placeholders, including
for a dedicated read-only database account and password.

## Local Development

This repository has its own uv virtual environment and intentionally ignores
`uv.lock`; it is not part of an uv workspace. For adjacent local clones:

```bash
uv venv
uv pip install -e ../h2hdb.clone
uv pip install -e ".[dev]"
uv run --no-sync pytest
```

---

## Q & A

- How to use Komga?
See [Rainie's article](https://home.gamer.com.tw/artwork.php?sn=5659465).

- Why is a CBZ file not updated?

  The CBZ must be present in Komga and in a successfully published H2HDB
  catalog revision. Enable `trigger_scan`, scan the library in Komga, or run
  the command again after ingest has published the artifact. Both the current
  content-addressed filename and a current friendly projection name ending in
  `[gid]` are supported.

---

## Credits

The project was created by [Kuan-Lun Wang](https://www.klwang.tw/home/).

---

## License

This project is distributed under the terms of the GNU General Public Licence
(GPL). For detailed licence terms, see the `LICENSE` file included in this
distribution.
