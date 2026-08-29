__all__ = ["sync_komga_library"]

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from typing import Any, Protocol, cast

import requests
from h2hdb import (
    CatalogPublication,
    CatalogReader,
    CatalogRevision,
    CatalogRevisionNotFoundError,
)

from .config_loader import KomgaConfig
from .coordination import LibraryReadCoordinator
from .komga import PATCH_TIMEOUT_SECONDS, KomgaClient
from .metadata import KomgaMetadata, publication_to_komga_metadata

logger = logging.getLogger(__name__)

KOMGA_MAX_WORKERS = 10
# Bounds each PATCH request body regardless of library size -- one request
# bundling a whole library-wide pass risks a body-size limit (Komga's own,
# or a reverse proxy's).
BOOK_METADATA_PATCH_CHUNK_SIZE = 200
# Re-patches only the books still unverified after a full attempt (not the
# whole batch), up to this many times, with a pause between attempts.
PATCH_RETRY_ATTEMPTS = 3
PATCH_RETRY_DELAY_SECONDS = 30
# A percentage-based progress log can go silent for a long stretch on a huge,
# slow batch -- this caps the longest possible silence.
PROGRESS_LOG_MAX_INTERVAL_SECONDS = 300
SETTLING_POLL_INTERVAL_SECONDS = 5.0
SETTLING_STABLE_OBSERVATION_SECONDS = 30.0
SETTLING_TIMEOUT_SECONDS = 3600.0
CATALOG_LOOKUP_BATCH_SIZE = 128
CANONICAL_ARTIFACT_NAME_PATTERN = re.compile(r"^h2h-([1-9][0-9]*)$")
MAX_GID = (1 << 63) - 1


class KomgaGateway(Protocol):
    library_id: str

    def get_book_page(
        self,
        page: int,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[dict[str, Any], ...]: ...

    def get_series_page(
        self,
        page: int,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[dict[str, Any], ...]: ...

    def get_book(
        self, book_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]: ...

    def patch_books_metadata(
        self,
        metadata_by_book_id: dict[str, KomgaMetadata],
        *,
        timeout_seconds: float | None = None,
    ) -> None: ...

    def scan_library(self, *, timeout_seconds: float | None = None) -> None: ...

    def analyze_library(self, *, timeout_seconds: float | None = None) -> None: ...


def _remaining_seconds(
    deadline: float,
    clock: Callable[[], float],
    *,
    operation: str,
) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError(
            f"Timed out waiting for Komga library to settle during {operation}"
        )
    return remaining


def _progress_logger(
    action: str, total: int, unit: str = "book(s)"
) -> Callable[[int], None]:
    log_every = max(total // 10, 1)
    last_logged_at = monotonic()

    def log(completed: int) -> None:
        nonlocal last_logged_at
        now = monotonic()
        if (
            completed % log_every == 0
            or completed == total
            or now - last_logged_at >= PROGRESS_LOG_MAX_INTERVAL_SECONDS
        ):
            logger.info("%s %d/%d %s", action, completed, total, unit)
            last_logged_at = now

    return log


def _canonical_artifact_name(book_name: str) -> str | None:
    normalized = book_name
    if normalized.endswith(".cbz"):
        normalized = normalized[:-4]
    match = CANONICAL_ARTIFACT_NAME_PATTERN.fullmatch(normalized)
    if match is None:
        return None
    gid = int(match.group(1))
    if gid > MAX_GID:
        return None
    return f"h2h-{gid}.cbz"


def _get_catalog_publications_by_artifact_names(
    catalog_reader: CatalogReader,
    artifact_names: list[str],
    *,
    revision: CatalogRevision,
) -> dict[str, CatalogPublication]:
    publications: dict[str, CatalogPublication] = {}
    for offset in range(0, len(artifact_names), CATALOG_LOOKUP_BATCH_SIZE):
        batch = artifact_names[offset : offset + CATALOG_LOOKUP_BATCH_SIZE]
        publications.update(
            catalog_reader.get_publications_by_artifact_names(
                batch,
                revision=revision,
            )
        )
    return publications


def _get_catalog_metadata_by_book_names(
    catalog_reader: CatalogReader,
    book_names: list[str],
    *,
    revision: CatalogRevision | None = None,
) -> dict[str, KomgaMetadata]:
    names = list(dict.fromkeys(book_names))
    if not names:
        return {}
    selected_revision = revision or catalog_reader.get_catalog_revision()

    artifact_by_name = {
        name: artifact_name
        for name in names
        if (artifact_name := _canonical_artifact_name(name)) is not None
    }
    artifact_candidates = list(dict.fromkeys(artifact_by_name.values()))
    publications_by_artifact = _get_catalog_publications_by_artifact_names(
        catalog_reader,
        artifact_candidates,
        revision=selected_revision,
    )

    publications_by_name: dict[str, CatalogPublication] = {}
    for name, artifact_name in artifact_by_name.items():
        if publication := publications_by_artifact.get(artifact_name):
            publications_by_name[name] = publication

    result = {
        name: publication_to_komga_metadata(publication)
        for name, publication in publications_by_name.items()
    }
    skipped = len(names) - len(result)
    if skipped:
        logger.info("%d Komga book name(s) had no published catalog entry", skipped)
    return result


class _IncompleteSnapshot(Exception):
    pass


class _CatalogHeadAdvanced(Exception):
    pass


@dataclass(frozen=True)
class _SnapshotObservation:
    fingerprint: str
    book_count: int
    series_count: int
    metadata_was_stable: bool


class _SnapshotStore:
    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path)
        self._book_count = 0
        self._series_count = 0
        self._update_count = 0
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            """
            CREATE TABLE observed_book (
                book_id TEXT PRIMARY KEY,
                artifact_name TEXT NOT NULL UNIQUE,
                series_id TEXT NOT NULL UNIQUE
            ) WITHOUT ROWID;
            CREATE TABLE pending_update (
                book_id TEXT PRIMARY KEY
                    REFERENCES observed_book(book_id) ON DELETE CASCADE,
                metadata_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE observed_series (
                series_id TEXT PRIMARY KEY
                    REFERENCES observed_book(series_id)
            ) WITHOUT ROWID;
            """
        )

    def close(self) -> None:
        self._connection.close()

    def reset(self) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM pending_update")
            self._connection.execute("DELETE FROM observed_series")
            self._connection.execute("DELETE FROM observed_book")
        self._book_count = 0
        self._series_count = 0
        self._update_count = 0

    def add_books(
        self,
        rows: list[tuple[str, str, str, KomgaMetadata | None]],
    ) -> None:
        try:
            with self._connection:
                self._connection.executemany(
                    """
                    INSERT INTO observed_book(book_id, artifact_name, series_id)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (book_id, artifact_name, series_id)
                        for book_id, artifact_name, series_id, _ in rows
                    ),
                )
                self._connection.executemany(
                    """
                    INSERT INTO pending_update(book_id, metadata_json)
                    VALUES (?, ?)
                    """,
                    (
                        (
                            book_id,
                            json.dumps(
                                metadata,
                                ensure_ascii=True,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )
                        for book_id, _, _, metadata in rows
                        if metadata is not None
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise _IncompleteSnapshot(
                "duplicate book id, canonical artifact name, or one-shot series"
            ) from error
        self._book_count += len(rows)
        self._update_count += sum(metadata is not None for *_, metadata in rows)

    def add_series(self, series_ids: list[str]) -> None:
        try:
            with self._connection:
                self._connection.executemany(
                    "INSERT INTO observed_series(series_id) VALUES (?)",
                    ((series_id,) for series_id in series_ids),
                )
        except sqlite3.IntegrityError as error:
            raise _IncompleteSnapshot(
                "duplicate or unreferenced one-shot series"
            ) from error
        self._series_count += len(series_ids)

    def count_books(self) -> int:
        return self._book_count

    def count_series(self) -> int:
        return self._series_count

    def count_updates(self) -> int:
        return self._update_count

    def update_batches(self) -> Iterator[dict[str, KomgaMetadata]]:
        after_book_id = ""
        while True:
            rows = self._connection.execute(
                """
                SELECT book_id, metadata_json
                FROM pending_update
                WHERE book_id > ?
                ORDER BY book_id
                LIMIT ?
                """,
                (after_book_id, BOOK_METADATA_PATCH_CHUNK_SIZE),
            ).fetchall()
            if not rows:
                return
            batch: dict[str, KomgaMetadata] = {}
            for book_id, metadata_json in rows:
                metadata = json.loads(metadata_json)
                if not isinstance(book_id, str) or not isinstance(metadata, dict):
                    raise RuntimeError("Invalid internal Komga snapshot data")
                batch[book_id] = cast(KomgaMetadata, metadata)
            yield batch
            after_book_id = cast(str, rows[-1][0])

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for row in self._connection.execute(
            """
            SELECT book_id, artifact_name, series_id
            FROM observed_book
            ORDER BY book_id
            """
        ):
            digest.update(
                json.dumps(
                    row,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode()
            )
        for row in self._connection.execute(
            "SELECT series_id FROM observed_series ORDER BY series_id"
        ):
            digest.update(
                json.dumps(
                    row,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode()
            )
        return digest.hexdigest()


def _book_metadata_is_up_to_date(
    expected_metadata: KomgaMetadata, book: dict[str, Any]
) -> bool:
    metadata = book.get("metadata")
    return isinstance(metadata, dict) and bool(
        expected_metadata.items() <= metadata.items()
    )


def _patch_chunk(
    client: KomgaGateway,
    chunk: dict[str, KomgaMetadata],
    *,
    deadline: float,
    clock: Callable[[], float],
) -> None:
    try:
        client.patch_books_metadata(
            chunk,
            timeout_seconds=_remaining_seconds(
                deadline, clock, operation="metadata PATCH"
            ),
        )
    except requests.exceptions.Timeout:
        logger.warning(
            "PATCH for %d book(s) timed out client-side after %ds; will verify "
            "and retry if it didn't actually land",
            len(chunk),
            PATCH_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as e:
        logger.error("PATCH for %d book(s) failed: %s", len(chunk), e)


def _find_unverified_books(
    client: KomgaGateway,
    expected_metadata_by_book_id: dict[str, KomgaMetadata],
    *,
    deadline: float,
    clock: Callable[[], float],
) -> list[str]:
    # A 204 only confirms the bulk request was accepted, not that every book
    # in it was actually applied -- re-fetching and diffing is the only way
    # to confirm a given book's write landed.
    def is_verified(book_id: str, expected_metadata: KomgaMetadata) -> bool:
        try:
            book = client.get_book(
                book_id,
                timeout_seconds=_remaining_seconds(
                    deadline, clock, operation="metadata verification"
                ),
            )
        except requests.exceptions.RequestException:
            return False
        return _book_metadata_is_up_to_date(expected_metadata, book)

    log_progress = _progress_logger("Verified", len(expected_metadata_by_book_id))
    # Runs once per attempt after all chunks finish, not nested inside chunk
    # dispatch, so this pool doesn't multiply concurrency against Komga.
    with ThreadPoolExecutor(max_workers=KOMGA_MAX_WORKERS) as executor:
        futures = {
            executor.submit(is_verified, book_id, expected_metadata): book_id
            for book_id, expected_metadata in expected_metadata_by_book_id.items()
        }
        unverified = list[str]()
        for completed, future in enumerate(as_completed(futures), start=1):
            if not future.result():
                unverified.append(futures[future])
            log_progress(completed)
        return sorted(unverified)


def _chunk_size_for_attempt(attempt: int) -> int:
    # A bulk PATCH appears to reject its whole request if any single book in
    # it is invalid, rather than applying the rest -- so retrying with the
    # same grouping leaves a bad book's chunk-mates stuck behind it forever.
    # Shrinking chunk size each attempt, down to one book per request on the
    # last attempt, guarantees only genuinely-bad books are still isolated
    # (and identifiable) once retries run out.
    if attempt == PATCH_RETRY_ATTEMPTS:
        return 1
    shrink_factor = int(10 ** (attempt - 1))
    return max(1, BOOK_METADATA_PATCH_CHUNK_SIZE // shrink_factor)


def _patch_with_retries(
    client: KomgaGateway,
    updates: dict[str, KomgaMetadata],
    *,
    deadline: float,
    clock: Callable[[], float],
    sleep_for: Callable[[float], None],
) -> dict[str, KomgaMetadata]:
    remaining = updates
    for attempt in range(1, PATCH_RETRY_ATTEMPTS + 1):
        _remaining_seconds(deadline, clock, operation="metadata retry")
        chunk_size = _chunk_size_for_attempt(attempt)
        remaining_ids = list(remaining)
        chunks = [
            {
                book_id: remaining[book_id]
                for book_id in remaining_ids[i : i + chunk_size]
            }
            for i in range(0, len(remaining_ids), chunk_size)
        ]
        logger.info(
            "Attempt %d/%d: patching %d book(s) in %d chunk(s) of up to %d",
            attempt,
            PATCH_RETRY_ATTEMPTS,
            len(remaining),
            len(chunks),
            chunk_size,
        )
        log_progress = _progress_logger("Patched", len(chunks), unit="chunk(s)")
        with ThreadPoolExecutor(max_workers=KOMGA_MAX_WORKERS) as executor:
            chunk_futures = [
                executor.submit(
                    _patch_chunk,
                    client,
                    chunk,
                    deadline=deadline,
                    clock=clock,
                )
                for chunk in chunks
            ]
            for completed, future in enumerate(as_completed(chunk_futures), start=1):
                future.result()
                log_progress(completed)

        unverified_ids = _find_unverified_books(
            client,
            remaining,
            deadline=deadline,
            clock=clock,
        )
        if not unverified_ids:
            logger.info("All %d book(s) patched and verified", len(remaining))
            return {}

        remaining = {book_id: remaining[book_id] for book_id in unverified_ids}
        logger.warning(
            "%d book(s) still not verified after attempt %d/%d",
            len(remaining),
            attempt,
            PATCH_RETRY_ATTEMPTS,
        )
        if attempt < PATCH_RETRY_ATTEMPTS:
            sleep_for(
                min(
                    PATCH_RETRY_DELAY_SECONDS,
                    _remaining_seconds(
                        deadline, clock, operation="metadata retry delay"
                    ),
                )
            )
    return remaining


def _required_nonempty_string(
    item: dict[str, Any],
    field: str,
    *,
    item_kind: str,
) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise _IncompleteSnapshot(f"{item_kind} has invalid {field}")
    return value


def _collect_book_page(
    client: KomgaGateway,
    catalog_reader: CatalogReader,
    store: _SnapshotStore,
    books: tuple[dict[str, Any], ...],
    *,
    revision: CatalogRevision,
    expected_count: int,
) -> None:
    parsed: list[tuple[str, str, str, dict[str, Any]]] = []
    canonical_names: list[str] = []
    for book in books:
        book_id = _required_nonempty_string(book, "id", item_kind="Komga book")
        book_name = _required_nonempty_string(book, "name", item_kind="Komga book")
        series_id = _required_nonempty_string(
            book,
            "seriesId",
            item_kind="Komga book",
        )
        library_id = _required_nonempty_string(
            book,
            "libraryId",
            item_kind="Komga book",
        )
        metadata = book.get("metadata")
        if library_id != client.library_id:
            raise _IncompleteSnapshot("Komga returned a book from another library")
        if book.get("oneshot") is not True:
            raise _IncompleteSnapshot("Komga book is not a One-Shot")
        if not isinstance(metadata, dict):
            raise _IncompleteSnapshot("Komga book has invalid metadata")
        canonical_name = _canonical_artifact_name(book_name)
        if canonical_name is None:
            raise _IncompleteSnapshot("Komga book name is not canonical")
        parsed.append((book_id, canonical_name, series_id, metadata))
        canonical_names.append(canonical_name)

    if len(set(canonical_names)) != len(canonical_names):
        raise _IncompleteSnapshot("Komga page has duplicate canonical artifact names")
    publications = _get_catalog_publications_by_artifact_names(
        catalog_reader,
        canonical_names,
        revision=revision,
    )
    if set(publications) != set(canonical_names):
        raise _IncompleteSnapshot("canonical catalog lookup did not match every book")

    rows: list[tuple[str, str, str, KomgaMetadata | None]] = []
    for book_id, canonical_name, series_id, current_metadata in parsed:
        publication = publications[canonical_name]
        if canonical_name != f"h2h-{publication.gid}.cbz":
            raise _IncompleteSnapshot(
                "catalog publication GID does not match book name"
            )
        expected_metadata = publication_to_komga_metadata(publication)
        rows.append(
            (
                book_id,
                canonical_name,
                series_id,
                None
                if expected_metadata.items() <= current_metadata.items()
                else expected_metadata,
            )
        )
    store.add_books(rows)
    if store.count_books() > expected_count:
        raise _IncompleteSnapshot("Komga has extra books")


def _collect_series_page(
    client: KomgaGateway,
    store: _SnapshotStore,
    series: tuple[dict[str, Any], ...],
    *,
    expected_count: int,
) -> None:
    series_ids: list[str] = []
    for item in series:
        series_id = _required_nonempty_string(
            item,
            "id",
            item_kind="Komga series",
        )
        library_id = _required_nonempty_string(
            item,
            "libraryId",
            item_kind="Komga series",
        )
        if library_id != client.library_id:
            raise _IncompleteSnapshot("Komga returned a series from another library")
        if item.get("oneshot") is not True:
            raise _IncompleteSnapshot("Komga series is not a One-Shot")
        series_ids.append(series_id)
    if len(set(series_ids)) != len(series_ids):
        raise _IncompleteSnapshot("Komga page has duplicate One-Shot series")
    store.add_series(series_ids)
    if store.count_series() > expected_count:
        raise _IncompleteSnapshot("Komga has extra One-Shot series")


def _reconcile_exact_snapshot(
    client: KomgaGateway,
    catalog_reader: CatalogReader,
    store: _SnapshotStore,
    *,
    revision: CatalogRevision,
    deadline: float,
    clock: Callable[[], float],
    sleep_for: Callable[[float], None],
) -> _SnapshotObservation:
    expected_count = revision.artifact_count
    store.reset()

    book_page_number = 0
    while True:
        try:
            books = client.get_book_page(
                book_page_number,
                timeout_seconds=_remaining_seconds(
                    deadline,
                    clock,
                    operation="book pagination",
                ),
            )
        except requests.exceptions.RequestException as error:
            raise _IncompleteSnapshot("Komga book page request failed") from error
        if not books:
            break
        _collect_book_page(
            client,
            catalog_reader,
            store,
            books,
            revision=revision,
            expected_count=expected_count,
        )
        book_page_number += 1

    book_count = store.count_books()
    if book_count != expected_count:
        raise _IncompleteSnapshot(
            f"Komga book count {book_count} does not match catalog artifact "
            f"count {expected_count}"
        )

    series_page_number = 0
    while True:
        try:
            series = client.get_series_page(
                series_page_number,
                timeout_seconds=_remaining_seconds(
                    deadline,
                    clock,
                    operation="series pagination",
                ),
            )
        except requests.exceptions.RequestException as error:
            raise _IncompleteSnapshot("Komga series page request failed") from error
        if not series:
            break
        _collect_series_page(
            client,
            store,
            series,
            expected_count=expected_count,
        )
        series_page_number += 1

    series_count = store.count_series()
    if series_count != expected_count:
        raise _IncompleteSnapshot(
            f"Komga One-Shot series count {series_count} does not match catalog "
            f"artifact count {expected_count}"
        )

    observed_head = catalog_reader.get_catalog_revision()
    if observed_head.revision != revision.revision:
        raise _CatalogHeadAdvanced

    update_count = store.count_updates()
    logger.info(
        "%d of %d exactly matched Komga book(s) are out of date",
        update_count,
        book_count,
    )
    for updates in store.update_batches():
        remaining = _patch_with_retries(
            client,
            updates,
            deadline=deadline,
            clock=clock,
            sleep_for=sleep_for,
        )
        if remaining:
            raise RuntimeError(
                f"Komga metadata update did not verify for {len(remaining)} "
                f"book(s): {', '.join(sorted(remaining))}"
            )

    return _SnapshotObservation(
        fingerprint=store.fingerprint(),
        book_count=book_count,
        series_count=series_count,
        metadata_was_stable=update_count == 0,
    )


def _validate_settling_timing(
    *,
    poll_interval_seconds: float,
    stable_observation_seconds: float,
    timeout_seconds: float,
) -> None:
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    if stable_observation_seconds <= 0:
        raise ValueError("stable_observation_seconds must be positive")
    if timeout_seconds <= stable_observation_seconds:
        raise ValueError(
            "timeout_seconds must be greater than stable_observation_seconds"
        )


def sync_komga_library(
    komgaconfig: KomgaConfig,
    catalog_reader: CatalogReader,
    *,
    client: KomgaGateway | None = None,
    clock: Callable[[], float] = monotonic,
    sleep_for: Callable[[float], None] = sleep,
    poll_interval_seconds: float = SETTLING_POLL_INTERVAL_SECONDS,
    stable_observation_seconds: float = SETTLING_STABLE_OBSERVATION_SECONDS,
    timeout_seconds: float = SETTLING_TIMEOUT_SECONDS,
) -> None:
    with LibraryReadCoordinator(komgaconfig.coordination_root).read():
        _sync_komga_library(
            komgaconfig,
            catalog_reader,
            client=client,
            clock=clock,
            sleep_for=sleep_for,
            poll_interval_seconds=poll_interval_seconds,
            stable_observation_seconds=stable_observation_seconds,
            timeout_seconds=timeout_seconds,
        )


def _sync_komga_library(
    komgaconfig: KomgaConfig,
    catalog_reader: CatalogReader,
    *,
    client: KomgaGateway | None,
    clock: Callable[[], float],
    sleep_for: Callable[[float], None],
    poll_interval_seconds: float,
    stable_observation_seconds: float,
    timeout_seconds: float,
) -> None:
    _validate_settling_timing(
        poll_interval_seconds=poll_interval_seconds,
        stable_observation_seconds=stable_observation_seconds,
        timeout_seconds=timeout_seconds,
    )
    active_client: KomgaGateway = (
        client if client is not None else KomgaClient(komgaconfig)
    )

    started_at = clock()
    deadline = started_at + timeout_seconds

    if komgaconfig.trigger_scan:
        logger.info(
            "Triggering scan and analyze for library %s", active_client.library_id
        )
        active_client.scan_library(
            timeout_seconds=_remaining_seconds(
                deadline,
                clock,
                operation="scan request",
            )
        )
        active_client.analyze_library(
            timeout_seconds=_remaining_seconds(
                deadline,
                clock,
                operation="analyze request",
            )
        )

    previous_fingerprint: str | None = None
    previous_catalog_revision: int | None = None
    stable_since: float | None = None
    with TemporaryDirectory(prefix="h2hdb-komga-snapshot-") as directory:
        store = _SnapshotStore(Path(directory) / "snapshot.sqlite3")
        try:
            while True:
                if clock() >= deadline:
                    raise TimeoutError(
                        f"Timed out after {timeout_seconds:g}s waiting for Komga "
                        f"library {active_client.library_id} to settle"
                    )

                revision = catalog_reader.get_catalog_revision()
                try:
                    observation = _reconcile_exact_snapshot(
                        active_client,
                        catalog_reader,
                        store,
                        revision=revision,
                        deadline=deadline,
                        clock=clock,
                        sleep_for=sleep_for,
                    )
                except CatalogRevisionNotFoundError, _CatalogHeadAdvanced:
                    previous_fingerprint = None
                    previous_catalog_revision = None
                    stable_since = None
                    logger.info(
                        "Catalog head advanced during pinned reconciliation; "
                        "restarting from the current head"
                    )
                except _IncompleteSnapshot as error:
                    previous_fingerprint = None
                    previous_catalog_revision = None
                    stable_since = None
                    logger.info(
                        "Komga exact-set observation is incomplete: %s; polling again",
                        error,
                    )
                else:
                    observed_at = clock()
                    if observed_at >= deadline:
                        raise TimeoutError(
                            f"Timed out after {timeout_seconds:g}s waiting for Komga "
                            f"library {active_client.library_id} to settle"
                        )

                    snapshot_changed = (
                        previous_fingerprint is not None
                        and previous_fingerprint != observation.fingerprint
                    )
                    revision_changed = (
                        previous_catalog_revision is not None
                        and previous_catalog_revision != revision.revision
                    )
                    if observation.metadata_was_stable and not revision_changed:
                        if stable_since is None or snapshot_changed:
                            stable_since = observed_at
                        stable_for = observed_at - stable_since
                        if stable_for >= stable_observation_seconds:
                            final_revision = catalog_reader.get_catalog_revision()
                            if final_revision.revision == revision.revision:
                                logger.info(
                                    "Library %s exactly matched %d catalog "
                                    "artifact(s) and was stable for %.1fs at catalog "
                                    "revision %d; sync complete",
                                    active_client.library_id,
                                    observation.book_count,
                                    stable_for,
                                    revision.revision,
                                )
                                return
                            stable_since = None
                            logger.info(
                                "Catalog revision changed during final stability "
                                "check; polling again"
                            )
                        logger.info(
                            "Library %s exact-set stable for %.1f/%.1fs "
                            "(%d books, %d One-Shot series)",
                            active_client.library_id,
                            stable_for,
                            stable_observation_seconds,
                            observation.book_count,
                            observation.series_count,
                        )
                    else:
                        stable_since = None
                        logger.info(
                            "Library %s changed during exact-set reconciliation; "
                            "polling again",
                            active_client.library_id,
                        )

                    previous_fingerprint = observation.fingerprint
                    previous_catalog_revision = revision.revision

                remaining_seconds = deadline - clock()
                if remaining_seconds <= 0:
                    raise TimeoutError(
                        f"Timed out after {timeout_seconds:g}s waiting for Komga "
                        f"library {active_client.library_id} to settle"
                    )
                sleep_for(min(poll_interval_seconds, remaining_seconds))
        finally:
            store.close()
