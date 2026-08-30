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

Canonical `h2h-<gid>.cbz` artifact basenames are matched directly through the
public lookup. Komga may include or omit the `.cbz` suffix; the adapter
normalizes either form to the one canonical name and queries names in batches
of at most 128 against one pinned revision. Legacy friendly, pure-GID, and
content-addressed filenames are intentionally unsupported. A revision change
resets the stability window, and completion performs a final revision check.
This adapter does not know the library's hash-shard paths and never reads core
repository internals. An unmatched, duplicate, noncanonical, non-One-Shot,
partial, or extra Komga item makes the observation incomplete, so no metadata
from that pass is patched.

The H2HDB database is always opened in read-only mode. Startup performs the
exact epoch-3 `READY` audit through H2HDB's public database opener but never
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
    "coordination_root": "/srv/h2hdb/coordination",
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

`coordination_root` is required and must be an absolute path to a separate
read-only bind of the ingest library's coordination directory. Its host source
is `<library-root>/.h2hdb-coordination`, alongside the private
`<library-root>/.h2hdb-state` directory. Mount only `.h2hdb-coordination` into
this consumer; the private state tree must not be mounted. The coordination
directory contains the permanent regular file `publication.lock` and, only
during an unfinished cutover, `ACTIVATING`. The command opens every directory
component without following symlinks, opens the lock nonblocking, acquires a
nonblocking shared flock, and checks the marker before contacting Komga. A busy
or unsafe lock, a symlinked path, or any `ACTIVATING` entry fails closed. The
shared lock is held through scan, analyze, settling, metadata reconciliation,
and the final stability check. Normal exit, failure, container stop, and the
CLI's hard worker termination all release the kernel lock when the descriptor
closes.

Edit the target Komga library and disable both autonomous scan settings:

- **Scan on startup:** disabled
- **Scan interval:** disabled

Only this coordinated command may trigger a library scan/analyze. Keep
`trigger_scan` set to `true` for the scheduled synchronization job, and do not
trigger scans from the Komga UI, another API client, or another scheduler.

`trigger_scan` defaults to `true`. Set it to `false` to skip requesting a
Komga scan/analyze and only reconcile metadata already visible in the library.
Afterward, the command polls every five seconds and page-reads every current
book and series. It checks that every book belongs to the configured library,
is a One-Shot, has a unique canonical artifact name and unique series, and
matches a publication from the pinned catalog revision. The referenced series
must exactly equal the library's unique One-Shot series, and both counts must
equal the revision's artifact count. Exact empty catalog and Komga sets are
valid. A temporary file-backed index enforces whole-library uniqueness while
keeping memory bounded; catalog lookups use at most 128 names, Komga pages use
at most 500 items, and pending updates are read in keyset batches of at most
200. No metadata is patched until the complete exact-set proof succeeds.

The command exits only after the exact set and metadata remain unchanged and
write-free for 30 seconds; the default timeout is one hour and can be changed
with `--timeout-seconds`. This observation window allows Komga's asynchronous
scan/analyze jobs and transient page failures to become visible before
completion. A scan or analyze request must receive a successful response;
client-side timeout cannot prove that Komga accepted it, so the run fails
closed and the next invocation retries the complete operation. Every catalog
lookup in one pass is pinned to the same current H2HDB head. If publication
advances during a batched lookup, the partial pass is discarded before
metadata is patched and the next poll starts again from the new head. Each
HTTP request, PATCH verification, retry, and retry delay uses the remaining
cooperative budget. The CLI also runs the complete operation in a disposable
worker process and kills it at the wall-clock deadline, so a slow-drip socket,
blocked database gate, or executor shutdown cannot extend the documented hard
timeout indefinitely.

#### h2hdb-config.json

Use an H2HDB core configuration compatible with `h2hdb>=0.27.0,<0.28`. Any
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

  The canonical `h2h-<gid>.cbz` file must be present in Komga and in a
  successfully published H2HDB catalog revision. After ingest publishes the
  artifact, run this coordinated command with `trigger_scan` enabled. Do not
  trigger an uncoordinated scan from Komga. Legacy content-addressed, pure-GID,
  and friendly projection names are not supported.

---

## Credits

The project was created by [Kuan-Lun Wang](https://www.klwang.tw/home/).

---

## License

This project is distributed under the terms of the GNU General Public License
version 3 (GPLv3). See the included `LICENSE` file for the complete terms.
