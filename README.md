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

Published friendly artifact names are matched directly through the public
lookup, whether Komga includes the `.cbz` suffix or not; lookup candidates are
queried in batches of at most 128 against one pinned revision. When direct
lookup does not match, a physical `gid-sha256.cbz` storage name, a pure GID, or
a friendly name ending in `[gid]` is resolved against that same pinned revision
through public catalog pagination. A revision change resets the stability
window, and completion performs a final revision check. This adapter never
reads `CatalogArtifact.location` or any core repository internals. Books with
no published match are left unchanged.

The H2HDB database is always opened in read-only mode. Startup performs the
exact epoch-2 `READY` audit through H2HDB's public database opener but never
initializes or migrates schema; schema ownership stays with H2HDB core.

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
completion. Every catalog lookup in one pass is pinned to the same current
H2HDB head. If publication advances during a batched lookup or pagination, the
partial pass is discarded before metadata is patched and the next poll starts
again from the new head. Each HTTP request, PATCH verification, retry, and retry
delay uses the remaining cooperative budget. The CLI also runs the complete
operation in a disposable worker process and kills it at the wall-clock
deadline, so a slow-drip socket, blocked database gate, or executor shutdown
cannot extend the documented hard timeout indefinitely.

#### h2hdb-config.json

Use an H2HDB core configuration compatible with `h2hdb>=0.24.0,<0.25`. Any
configured database access mode is overridden to `read-only` by this CLI.
The core loader supports the same exact `${ENV_NAME}` placeholders, including
for a dedicated read-only database account and password.

## Local Development

Rebuild the repository-local environment and run its canonical gates with:

```bash
./scripts/rebuild-env.sh
./scripts/check-fast.sh
./scripts/check-full.sh
```

The rebuild script installs this project in editable mode and resolves the
published compatible H2HDB core. It uses `uv` only for `.venv` and pip-style
installation, never reads `uv.lock`, and never assumes an adjacent checkout.

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

This project is distributed under the terms of the GNU General Public License
version 3 (GPLv3). See the included `LICENSE` file for the complete terms.
