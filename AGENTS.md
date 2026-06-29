# AGENTS.md

Guidance for coding agents working in this repository.

## Project Overview

H2HDB-Komga is a small CLI tool that syncs metadata from an
[H2HDB](https://github.com/Kuan-Lun/h2hdb) database into a
[Komga](https://komga.org/) library: it triggers a Komga library
scan/analyze, then walks the books and series in that library and patches
their Komga metadata (tags, titles) to match what H2HDB has recorded.

The main entry point is:

```bash
uv run python -m h2hdb_komga --komgaconfig [komga-config.json] --h2hdbconfig [h2hdb-config.json]
```

Python must be run through `uv run` so commands use the project virtual
environment and dependency versions. The Python version requirement is
defined by `requires-python` in `pyproject.toml`.

## Common Commands

```bash
uv pip install -e ".[dev]" --group dev
uv run ruff check src/h2hdb_komga
uv run black src/h2hdb_komga
uv run mypy src/h2hdb_komga
uv run pymarkdownlnt fix .
```

If the virtual environment breaks after a Python upgrade or similar toolchain
change, rebuild it with:

```bash
./scripts/rebuild-env.sh
```

## Testing

There is no test suite yet. `[dependency-groups] dev` in `pyproject.toml`
pins `pytest` as groundwork, but no `tests/` directory exists. Because
`komga.py` talks to a live Komga server and `H2HDB` talks to a live database,
a future test suite will need to mock/stub those calls rather than hit real
services.

## Module Layout

- `src/h2hdb_komga/config_loader.py` — `KomgaConfig`, a plain `__slots__`
  value object (`base_url`, `api_username`, `api_password`, `library_id`).
- `src/h2hdb_komga/komga.py` — all Komga REST API calls (`requests` + HTTP
  basic auth) and the sync orchestration. Every request function is wrapped
  in `@retry_request`. `scan_komga_library` is the entry point: scan +
  analyze the Komga library, then patch book/series metadata for everything
  new since the previous call, recursing until a pass finds nothing new.
- `src/h2hdb_komga/__main__.py` — CLI argument parsing
  (`--komgaconfig`, `--h2hdbconfig`), config loading, and the `UpdateKomga`
  context manager that runs one `scan_komga_library` pass.

## Concurrency

`komga.py` dispatches per-book/per-series metadata updates through a
`concurrent.futures.ThreadPoolExecutor` bounded by `KOMGA_MAX_WORKERS` (10),
since each call is a handful of sequential HTTP round-trips to Komga and
H2HDB. Do not reintroduce a dependency on `h2hdb.threading_tools` for this:
that module's `ThreadsList` class (a threading+semaphore primitive) was
removed upstream in h2hdb 0.10.x and replaced with a `multiprocessing`-based
helper intended for CPU-bound work, which is the wrong concurrency model for
this module's I/O-bound HTTP calls. The stdlib `ThreadPoolExecutor` is the
correct primitive here.

## Dependency on H2HDB

This package only imports the public surface of `h2hdb` (`H2HDB`,
`H2HDBConfig`/`load_config`, `DatabaseKeyError` from `h2hdb.sql_connector`).
The `h2hdb` version constraint in `pyproject.toml` is currently wide
(`>=0.7.0.9,<2.0.0.0`); h2hdb is pre-1.0 and has broken this package's
imports across minor versions before. After bumping the installed h2hdb
version, run `uv run mypy src/h2hdb_komga` to catch API drift before
assuming the bump is safe.

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
- `[project.optional-dependencies] dev` and `[dependency-groups] dev` in
  `pyproject.toml`

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
