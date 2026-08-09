# AGENTS.md

Guidance for coding agents working in this repository.

## Project Overview

H2HDB-Komga is a small CLI tool that syncs metadata from an
[H2HDB](https://github.com/Kuan-Lun/h2hdb) database into a
[Komga](https://komga.org/) library: it triggers a Komga library
scan/analyze, then walks the books and series in that library and patches
their Komga metadata to match the currently published H2HDB catalog
revision. H2HDB-Komga is a read-only catalog consumer and never owns or
migrates the database schema.

The main entry point is:

```bash
uv run --no-sync python -m h2hdb_komga --komgaconfig [komga-config.json] --h2hdbconfig [h2hdb-config.json]
```

Python must be run through `uv run --no-sync` so commands use the project virtual
environment and dependency versions. The Python version requirement is
defined by `requires-python` in `pyproject.toml`.

## Common Commands

```bash
uv pip install -e ".[dev]"
uv run --no-sync ruff check src/h2hdb_komga tests
uv run --no-sync black src/h2hdb_komga tests
uv run --no-sync mypy src/h2hdb_komga tests
uv run --no-sync pytest
uv run --no-sync python -m build
uv run --no-sync pymarkdownlnt fix .
```

If the virtual environment breaks after a Python upgrade or similar toolchain
change, rebuild it with:

```bash
./scripts/rebuild-env.sh
```

## Testing

The `tests/` suite uses fake `CatalogReader` and Komga gateway implementations;
it must not require a live database or Komga server. Cover neutral catalog
mapping, missing artifacts, settling-loop behavior, PATCH verification, and
the CLI's read-only/compatibility-check bootstrap whenever those boundaries
change.

## Module Layout

- `src/h2hdb_komga/config_loader.py` — frozen `KomgaConfig` dataclass and JSON
  loader.
- `src/h2hdb_komga/metadata.py` — adapter from neutral core
  `CatalogPublication` values to Komga metadata. It preserves the raw title
  when non-blank, summary, release date, every non-empty `h2h:tag:*` subject
  as its original role/value author pair, and GID. It does not map OPDS
  contributors or patch Komga tags.
- `src/h2hdb_komga/komga.py` — thin Komga REST client (`requests` + HTTP basic
  auth). It raises request failures for the orchestration layer to handle.
- `src/h2hdb_komga/sync.py` — settling-loop orchestration. Production injects
  the core `CatalogReader`; tests inject fake reader/client ports. Artifact
  names first resolve against public artifact-name lookup, including Komga's
  extensionless CBZ names. Projection names that are a GID or end in `[gid]`
  fall back to pagination over one pinned revision through the public reader;
  this also covers collision-disambiguated current projection names. Missing
  publications are expected and skipped. Every poll reconciles
  every current book, so transient fetch failures and metadata rewritten by
  Komga analysis are retried even when IDs do not change. Completion requires
  an unchanged, write-free observation window; polling has a hard timeout.
- `src/h2hdb_komga/__main__.py` — CLI argument parsing
  (`--komgaconfig`, `--h2hdbconfig`, `--timeout-seconds`), read-only core
  bootstrap inside a disposable worker process, and the outer wall-clock
  deadline supervisor. The process fence is what makes the CLI timeout hard
  even when a socket, database gate, or executor thread does not cooperate.

## Concurrency

`sync.py` dispatches per-book fetches and verification plus bulk metadata
PATCH chunks through a `concurrent.futures.ThreadPoolExecutor` bounded by
`KOMGA_MAX_WORKERS` (10), since each call is an HTTP round-trip to Komga. Do
not reintroduce a dependency on `h2hdb.threading_tools` for this:
that module's `ThreadsList` class (a threading+semaphore primitive) was
removed upstream in h2hdb 0.10.x and replaced with a `multiprocessing`-based
helper intended for CPU-bound work, which is the wrong concurrency model for
this module's I/O-bound HTTP calls. The stdlib `ThreadPoolExecutor` is the
correct primitive here.

## Dependency on H2HDB

This package only imports the top-level public surface of `h2hdb`. Sync code
depends on `CatalogReader` and neutral catalog models; it must not import core
connectors or repository internals. The CLI may construct `H2HDB` from a
`CoreConfig`, but must replace `database.access_mode` with `read-only`, call
`check_compatibility()`, and never call `migrate()`. The dependency is pinned
to the compatible core minor line (`>=0.21.0.0,<0.22`). Run mypy and the complete
test suite when changing that range.

This repo intentionally does not commit or depend on `uv.lock`. Rebuild the
independent virtual environment with editable installs; it is not a uv
workspace member.

## Tooling and Style

Follow SOLID principles and the existing local patterns. Keep changes scoped
to the feature or bug being addressed.

The IDE save pipeline and Claude Stop-hook pipeline are intentionally kept in
sync. If changing Python formatting, linting, type-checking, Markdown
formatting, or tool versions, update all relevant locations together:

- `.vscode/settings.json`
- `mypy.ini`
- `[tool.ruff.lint]` in `pyproject.toml`
- `scripts/hooks/finalize-python.sh`
- `scripts/hooks/finalize-markdown.sh`
- `.claude/settings.local.json`
- `[project.optional-dependencies] dev` in `pyproject.toml`

Tool versions should be changed in `pyproject.toml`, not through system-wide
installs.

Ruff `E2xx` whitespace rules are preview-only for the configured Ruff version.
Do not assume the CLI or hook will report every whitespace issue an IDE
extension might flag separately.

## Documentation Sync

`CLAUDE.md` is the source document this file was derived from. Keep both
files consistent when changing project workflow, architecture patterns,
testing expectations, or tooling behavior. Routine use of an already
documented pattern does not require a docs update; replacing the pattern
itself does.
