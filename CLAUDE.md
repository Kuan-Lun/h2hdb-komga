# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Communication

- Claude 必須以繁體中文回答所有對話內容，不論使用者以何種語言提問；程式碼、指令、檔名、專有名詞等仍維持原文。

## What this is

H2HDB-Komga is a small CLI tool that syncs metadata from an
[H2HDB](https://github.com/Kuan-Lun/h2hdb) database into a
[Komga](https://komga.org/) library: it triggers a library scan/analyze,
then walks the books and series in that library and patches their Komga
metadata (tags, titles) to match what H2HDB has recorded. Entry point:
`python -m h2hdb_komga --komgaconfig [json-path] --h2hdbconfig [json-path]`
(see `src/h2hdb_komga/__main__.py`).

Requires Python >= 3.14.

## Common commands

Environment is managed with `uv`.

```bash
uv pip install -e ".[dev]" --group dev
uv run ruff check src/h2hdb_komga     # lint
uv run black src/h2hdb_komga          # format
uv run mypy src/h2hdb_komga           # type-check (strict mode, see mypy.ini)
uv run pymarkdownlnt fix .            # markdown autofix
```

Always run Python through `uv run` (e.g. `uv run python -m h2hdb_komga ...`)
so it resolves to the project venv's interpreter and dependency versions.

A Claude Code Stop hook already runs this pipeline automatically after each
turn — see `scripts/hooks/finalize-python.sh` (black → ruff --fix → black →
mypy, scoped to `src/h2hdb_komga`) and `scripts/hooks/finalize-markdown.sh`
(pymarkdown fix → ruff format --preview on embedded code blocks), registered
in `.claude/settings.local.json`. It mirrors the VS Code on-save pipeline in
`.vscode/settings.json`. Tool versions for both paths come from the `dev`
extra in `pyproject.toml` — bump versions there, not via a system-wide
install.

If the venv breaks (e.g. after a Python version upgrade — mypyc extension
module errors), nuke and rebuild it with `./scripts/rebuild-env.sh`.

### Testing

There is no test suite yet (`[dependency-groups] dev` in `pyproject.toml`
pins `pytest` as groundwork, but no `tests/` directory exists). Since the
module talks to a live Komga server and H2HDB database, any test suite added
here will need to mock or stub the `requests` calls in `komga.py` and the
`H2HDB` connector rather than hitting real services.

## Architecture

This project is pre-1.0 and the sections below describe today's design, not a
contract to preserve. If a change intentionally replaces one of these
patterns, update or delete the stale part of this doc in the same change
rather than working around it.

### Module layout

- `config_loader.py` — `KomgaConfig`, a frozen dataclass with a
  `from_file()` classmethod that loads the user-supplied JSON file.
- `komga.py` — `KomgaClient`, a thin wrapper over Komga's REST API
  (`requests.Session` + HTTP basic auth, hard timeouts on every call).
  Methods raise `requests` exceptions on failure; deciding how to react
  (skip, verify, retry) is the sync layer's job, not the client's.
- `sync.py` — orchestration. `sync_komga_library` is the entry point: scan +
  analyze the library, then repeatedly diff Komga book metadata against
  H2HDB and patch what's out of date, looping until a pass finds the
  book/series listings unchanged (the library has settled). Patches go out
  in bounded chunks (`BOOK_METADATA_PATCH_CHUNK_SIZE`) dispatched
  concurrently; after each attempt every patched book is re-fetched and
  verified (a bulk-PATCH 204 doesn't confirm each individual book was
  applied), and only the books that failed verification are re-patched, up
  to `PATCH_RETRY_ATTEMPTS` — any survivors abort the run with a
  `RuntimeError` listing their IDs. Series metadata is left as Komga's own
  defaults; the series listing is only used for the settling check.
- `__main__.py` — CLI argument parsing (`--komgaconfig`, `--h2hdbconfig`),
  logging setup, config loading, one `sync_komga_library` call.

### Concurrency

HTTP calls fan out through `concurrent.futures.ThreadPoolExecutor` bounded
by `KOMGA_MAX_WORKERS` (10) in `sync.py` — per-book GETs when collecting
metadata and verifying patches, and per-chunk bulk PATCHes when writing.
Verification runs once per attempt after all of that attempt's chunks have
finished, never nested inside the chunk dispatch, so pools don't stack into
`KOMGA_MAX_WORKERS**2` concurrent requests against Komga. There used to be
a dependency on `h2hdb.threading_tools.ThreadsList` for this, but that class
was removed upstream in h2hdb 0.10.x (replaced there with a
`multiprocessing`-based helper meant for CPU-bound work, not this module's
I/O-bound HTTP calls) — don't reintroduce that dependency; the stdlib
`ThreadPoolExecutor` is the right primitive here.

### Dependency on H2HDB

This package only imports the public surface of `h2hdb`
(`H2HDB`, `H2HDBConfig`/`load_config`, `DatabaseKeyError` from
`h2hdb.sql_connector`). The `dependencies` constraint on `h2hdb` in
`pyproject.toml` is currently a wide range (`>=0.7.0.9,<2.0.0.0`); h2hdb is
pre-1.0 and has broken this package's imports across minor versions before
(see the `ThreadsList` removal above), so after bumping the installed h2hdb
version, re-run `uv run mypy src/h2hdb_komga` to catch API drift before
assuming the bump is safe.

## Keeping this file in sync

Routine use of an existing pattern needs no doc update. Update or delete the
affected paragraph only when a change replaces the *pattern itself* — e.g.
the concurrency primitive changes again, or the module layout is
restructured. Do that update in the same change, not a separate docs pass; a
stale Architecture section is worse than no Architecture section, since it
actively misleads the next session instead of just being silent.

## Design Principles

- Follow SOLID principles: single responsibility, open/closed, Liskov
  substitution, interface segregation, dependency inversion.

## Code Style

- **Sync obligation for tooling configuration:** the IDE save pipeline and the
  Stop hook pipeline are kept in lockstep across the locations below. Any
  change to one of them requires matching updates to the others in the same
  change.
  - Python formatting/lint/type-check:
    [.vscode/settings.json](.vscode/settings.json) (`[python]` block),
    [mypy.ini](mypy.ini) (strict mode), the `[tool.ruff.lint]` section of
    [pyproject.toml](pyproject.toml), all auto-discovered by both the IDE and
    `uv run`, and the shared implementation at
    [scripts/hooks/finalize-python.sh](scripts/hooks/finalize-python.sh),
    registered as a Claude Stop hook in
    [.claude/settings.local.json](.claude/settings.local.json).
  - Markdown formatting: [.vscode/settings.json](.vscode/settings.json)
    (`[markdown]` block), the shared implementation at
    [scripts/hooks/finalize-markdown.sh](scripts/hooks/finalize-markdown.sh),
    and the same Claude Stop-hook registration in
    [.claude/settings.local.json](.claude/settings.local.json).
  - Tool versions: `[project.optional-dependencies] dev` in
    [pyproject.toml](pyproject.toml) pins `black`, `ruff`, `mypy`, and
    `pymarkdownlnt`; `[dependency-groups] dev` pins `pytest`. Both the IDE
    pipeline (when invoked via `uv run`) and the Stop-hook scripts resolve to
    these venv-installed versions, so bumping any of them must be done here —
    not via Homebrew or any other system-wide install.
- Ruff's `E2xx` whitespace rules (e.g. `E271`/`E272`
  multiple-spaces-before/after-keyword) are preview-only in this Ruff version
  and stay off even with `select = ["E", ...]` unless `preview = true` is set.
  Don't be surprised if the CLI/hook misses a whitespace nit that an IDE
  extension flags separately.
- Python version range: refer to `requires-python` in
  [pyproject.toml](pyproject.toml)
- **Comments:** default to none. Only add one when the *why* isn't obvious
  from the code itself (a hidden constraint, a non-obvious invariant, a
  workaround for a specific bug). Never frame a comment around the current
  change, refactor, or task ("moved here for X", "changed from Y to Z",
  "added for the Z flow") — write it as a timeless statement of the
  constraint, since that context rots as the codebase evolves but the
  underlying constraint doesn't. Prefer one line over a multi-line block.
