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
metadata from one published H2HDB catalog revision. It is a read-only catalog
consumer and never owns or migrates the core schema. Entry point:
`python -m h2hdb_komga --komgaconfig [json-path] --h2hdbconfig [json-path]`
(see `src/h2hdb_komga/__main__.py`).

Requires Python >= 3.14.

## Common commands

Environment is managed with `uv`.

```bash
uv pip install -e ".[dev]"
uv run --no-sync ruff check src/h2hdb_komga tests  # lint
uv run --no-sync black src/h2hdb_komga tests       # format
uv run --no-sync mypy src/h2hdb_komga tests        # type-check (strict mode)
uv run --no-sync pytest                             # tests use fakes
uv run --no-sync python -m build                    # distribution smoke test
uv run --no-sync pymarkdownlnt fix .                # markdown autofix
```

Always run Python through `uv run --no-sync` (for example,
`uv run --no-sync python -m h2hdb_komga ...`)
so it resolves to the project venv's interpreter and dependency versions.

A Claude Code Stop hook already runs this pipeline automatically after each
turn — see `scripts/hooks/finalize-python.sh` (black → ruff --fix → black →
mypy, scoped to `src/h2hdb_komga` and `tests`) and
`scripts/hooks/finalize-markdown.sh`
(pymarkdown fix → ruff format --preview on embedded code blocks), registered
in `.claude/settings.local.json`. It mirrors the VS Code on-save pipeline in
`.vscode/settings.json`. Tool versions for both paths come from the `dev`
extra in `pyproject.toml` — bump versions there, not via a system-wide
install.

If the venv breaks (e.g. after a Python version upgrade — mypyc extension
module errors), nuke and rebuild it with `./scripts/rebuild-env.sh`.

### Testing

The `tests/` suite uses fake `CatalogReader` and Komga gateway implementations,
so it does not require a live Komga server or database. Preserve coverage for
neutral-model mapping, missing artifacts, the settling loop, PATCH
verification, and the CLI's read-only compatibility bootstrap.

## Architecture

This project is pre-1.0 and the sections below describe today's design, not a
contract to preserve. If a change intentionally replaces one of these
patterns, update or delete the stale part of this doc in the same change
rather than working around it.

### Module layout

- `config_loader.py` — `KomgaConfig`, a frozen dataclass with a
  `from_file()` classmethod that loads the user-supplied JSON file.
- `metadata.py` — the only translation layer from neutral core
  `CatalogPublication` values to Komga metadata. It maps the raw title when
  non-blank, summary, release date, every non-empty `h2h:tag:*` subject back
  to its original role/value author pair, and GID. It does not map OPDS
  contributors or patch Komga tags.
- `komga.py` — `KomgaClient`, a thin wrapper over Komga's REST API
  (`requests.Session` + HTTP basic auth, hard timeouts on every call).
  Methods raise `requests` exceptions on failure; deciding how to react
  (skip, verify, retry) is the sync layer's job, not the client's.
- `sync.py` — orchestration over injected `CatalogReader` and Komga gateway
  ports. `sync_komga_library` is the entry point: scan + analyze the library,
  then repeatedly diff Komga book metadata against the published core catalog
  and patch what's out of date. It resolves current artifact names through the
  public artifact lookup, accepting names with or without `.cbz`. A projection
  name that is a numeric GID or ends in `[gid]` is resolved through public,
  revision-pinned catalog pagination; this includes collision-disambiguated
  current projection names. Every polling pass re-fetches
  every current book, including books whose IDs were present on earlier
  passes, so transient GET failures and metadata rewritten by asynchronous
  analysis are retried.
  The library is complete only after book/series IDs remain unchanged and a
  complete metadata pass needs no write for
  `SETTLING_STABLE_OBSERVATION_SECONDS`; polling sleeps between passes and
  aborts at `SETTLING_TIMEOUT_SECONDS`. Patches go out in bounded chunks
  (`BOOK_METADATA_PATCH_CHUNK_SIZE`) dispatched
  concurrently; after each attempt every patched book is re-fetched and
  verified (a bulk-PATCH 204 doesn't confirm each individual book was
  applied), and only the books that failed verification are re-patched, up
  to `PATCH_RETRY_ATTEMPTS` — any survivors abort the run with a
  `RuntimeError` listing their IDs. Series metadata is left as Komga's own
  defaults; the series listing is only used for the settling check.
- `__main__.py` — CLI argument parsing (`--komgaconfig`, `--h2hdbconfig`,
  `--timeout-seconds`), logging setup, and read-only core bootstrap inside a
  disposable worker process. An outer supervisor kills that worker at the
  wall-clock deadline, even if a socket, database gate, or executor thread is
  uncooperative. It must never migrate the schema.

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

This package imports only the top-level public surface of `h2hdb`. Runtime
sync depends on `CatalogReader` and neutral catalog models, not connectors or
repository internals. The CLI constructs `H2HDB` from `CoreConfig`, forces
`DatabaseAccessMode.read_only`, and calls only `check_compatibility()` before
sync. The dependency constraint is the compatible minor line
`h2hdb>=0.22.0.1,<0.23`; re-run mypy and pytest after changing it.

This repository stays independent: there is no uv workspace and `uv.lock` is
ignored. Use the editable-install rebuild workflow for local multi-repo work.

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
    `uv run --no-sync`, and the shared implementation at
    [scripts/hooks/finalize-python.sh](scripts/hooks/finalize-python.sh),
    registered as a Claude Stop hook in
    [.claude/settings.local.json](.claude/settings.local.json).
  - Markdown formatting: [.vscode/settings.json](.vscode/settings.json)
    (`[markdown]` block), the shared implementation at
    [scripts/hooks/finalize-markdown.sh](scripts/hooks/finalize-markdown.sh),
    and the same Claude Stop-hook registration in
    [.claude/settings.local.json](.claude/settings.local.json).
  - Tool versions: `[project.optional-dependencies] dev` in
    [pyproject.toml](pyproject.toml) pins `black`, `ruff`, `mypy`,
    `pymarkdownlnt`, and `pytest`. Both the IDE pipeline (when invoked via
    `uv run --no-sync`) and the Stop-hook scripts resolve to these
    venv-installed versions, so bumping any of them must be done here — not via
    Homebrew or any other system-wide install.
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
