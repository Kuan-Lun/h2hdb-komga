__all__ = ["sync_komga_library"]

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic, sleep
from typing import Any, Protocol

import requests
from h2hdb import (
    CatalogPublication,
    CatalogReader,
    CatalogRevision,
    CatalogRevisionNotFoundError,
)

from .config_loader import KomgaConfig
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
FRIENDLY_GALLERY_GID_PATTERN = re.compile(r"\[(\d+)]$")
CONTENT_ADDRESSED_GID_PATTERN = re.compile(
    r"^(\d+)-[0-9a-f]{64}(?:-[0-9a-f]{32})?$",
    re.IGNORECASE,
)


class KomgaGateway(Protocol):
    library_id: str

    def get_book_ids(self, *, timeout_seconds: float | None = None) -> set[str]: ...

    def get_series_ids(self, *, timeout_seconds: float | None = None) -> set[str]: ...

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


def _artifact_name_candidates(book_name: str) -> tuple[str, ...]:
    if book_name.casefold().endswith(".cbz"):
        return (book_name,)
    return (book_name, f"{book_name}.cbz")


def _friendly_gallery_gid(book_name: str) -> int | None:
    normalized = book_name.strip()
    if normalized.casefold().endswith(".cbz"):
        normalized = normalized[:-4]
    if normalized.isdecimal():
        gid = int(normalized)
        return gid if gid > 0 else None
    content_addressed = CONTENT_ADDRESSED_GID_PATTERN.fullmatch(normalized)
    if content_addressed is not None:
        gid = int(content_addressed.group(1))
        return gid if gid > 0 else None
    match = FRIENDLY_GALLERY_GID_PATTERN.search(normalized)
    if match is None:
        return None
    gid = int(match.group(1))
    return gid if gid > 0 else None


def _publications_by_gids(
    catalog_reader: CatalogReader,
    gids: set[int],
    revision: CatalogRevision,
) -> dict[int, CatalogPublication]:
    if not gids:
        return {}
    result: dict[int, CatalogPublication] = {}
    offset = 0
    while offset < revision.publication_count and len(result) < len(gids):
        page = catalog_reader.list_publications(
            offset=offset,
            limit=CATALOG_LOOKUP_BATCH_SIZE,
            revision=revision,
        )
        for publication in page.publications:
            if publication.gid in gids:
                result[publication.gid] = publication
        if not page.publications:
            break
        offset += len(page.publications)
        if offset >= page.total:
            break
    return result


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

    candidates_by_name = {name: _artifact_name_candidates(name) for name in names}
    artifact_candidates = list(
        dict.fromkeys(
            candidate
            for candidates in candidates_by_name.values()
            for candidate in candidates
        )
    )
    publications_by_artifact: dict[str, CatalogPublication] = {}
    for offset in range(0, len(artifact_candidates), CATALOG_LOOKUP_BATCH_SIZE):
        batch = artifact_candidates[offset : offset + CATALOG_LOOKUP_BATCH_SIZE]
        publications_by_artifact.update(
            catalog_reader.get_publications_by_artifact_names(
                batch,
                revision=selected_revision,
            )
        )

    publications_by_name: dict[str, CatalogPublication] = {}
    for name, candidates in candidates_by_name.items():
        for candidate in candidates:
            publication = publications_by_artifact.get(candidate)
            if publication is not None:
                publications_by_name[name] = publication
                break

    friendly_gids_by_name = {
        name: gid
        for name in names
        if name not in publications_by_name
        if (gid := _friendly_gallery_gid(name)) is not None
    }
    publications_by_gid = _publications_by_gids(
        catalog_reader,
        set(friendly_gids_by_name.values()),
        selected_revision,
    )
    for name, gid in friendly_gids_by_name.items():
        if publication := publications_by_gid.get(gid):
            publications_by_name[name] = publication

    result = {
        name: publication_to_komga_metadata(publication)
        for name, publication in publications_by_name.items()
    }
    skipped = len(names) - len(result)
    if skipped:
        logger.info("%d Komga book name(s) had no published catalog entry", skipped)
    return result


def _book_metadata_is_up_to_date(
    expected_metadata: KomgaMetadata, book: dict[str, Any]
) -> bool:
    return bool(expected_metadata.items() <= book["metadata"].items())


def _fetch_books(
    client: KomgaGateway,
    book_ids: set[str],
    *,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    books: dict[str, dict[str, Any]] = {}
    failed_book_ids: set[str] = set()
    log_progress = _progress_logger("Fetched", len(book_ids))

    def fetch(book_id: str) -> dict[str, Any]:
        return client.get_book(
            book_id,
            timeout_seconds=_remaining_seconds(deadline, clock, operation="book fetch"),
        )

    with ThreadPoolExecutor(max_workers=KOMGA_MAX_WORKERS) as executor:
        futures = {executor.submit(fetch, book_id): book_id for book_id in book_ids}
        for completed, future in enumerate(as_completed(futures), start=1):
            book_id = futures[future]
            try:
                books[book_id] = future.result()
            except requests.exceptions.RequestException as e:
                failed_book_ids.add(book_id)
                logger.debug("Failed to fetch book %s: %s", book_id, e)
            log_progress(completed)
    if failed_book_ids:
        logger.warning(
            "Failed to fetch %d of %d book(s)", len(failed_book_ids), len(book_ids)
        )
    return books, failed_book_ids


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


def _update_books_metadata(
    client: KomgaGateway,
    catalog_reader: CatalogReader,
    book_ids: set[str],
    *,
    revision: CatalogRevision,
    deadline: float,
    clock: Callable[[], float],
    sleep_for: Callable[[float], None],
) -> bool:
    """Return whether a complete pass needed no metadata writes."""
    if not book_ids:
        logger.info("No books to check in library %s", client.library_id)
        return True

    logger.info("Fetching Komga metadata for %d book(s)", len(book_ids))
    books, failed_book_ids = _fetch_books(
        client,
        book_ids,
        deadline=deadline,
        clock=clock,
    )

    catalog_metadata_by_name = _get_catalog_metadata_by_book_names(
        catalog_reader,
        [book["name"] for book in books.values()],
        revision=revision,
    )

    # BookDto nests title/summary/releaseDate/authors under "metadata" --
    # comparing against the top-level BookDto would never match.
    updates: dict[str, KomgaMetadata] = {}
    for book_id, book in books.items():
        expected_metadata = catalog_metadata_by_name.get(book["name"])
        if expected_metadata is None:
            continue
        if not _book_metadata_is_up_to_date(expected_metadata, book):
            updates[book_id] = expected_metadata
    logger.info("%d of %d book(s) are out of date", len(updates), len(books))
    if not updates:
        return not failed_book_ids

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
    return False


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
        # Analyze in particular can run well past REQUEST_TIMEOUT_SECONDS on
        # Komga's side; the caller only needs the job queued, not finished,
        # since the settling loop below re-polls until it's done.
        try:
            active_client.scan_library(
                timeout_seconds=_remaining_seconds(
                    deadline, clock, operation="scan request"
                )
            )
        except requests.exceptions.Timeout:
            logger.info(
                "Scan request timed out waiting for a response; treating as queued"
            )
        try:
            active_client.analyze_library(
                timeout_seconds=_remaining_seconds(
                    deadline, clock, operation="analyze request"
                )
            )
        except requests.exceptions.Timeout:
            logger.info(
                "Analyze request timed out waiting for a response; treating as queued"
            )

    previous_book_ids: set[str] | None = None
    previous_series_ids: set[str] | None = None
    previous_catalog_revision: int | None = None
    stable_since: float | None = None
    while True:
        if clock() >= deadline:
            raise TimeoutError(
                f"Timed out after {timeout_seconds:g}s waiting for Komga library "
                f"{active_client.library_id} to settle"
            )

        revision = catalog_reader.get_catalog_revision()
        book_ids = active_client.get_book_ids(
            timeout_seconds=_remaining_seconds(
                deadline, clock, operation="book pagination"
            )
        )
        try:
            metadata_was_stable = _update_books_metadata(
                active_client,
                catalog_reader,
                book_ids,
                revision=revision,
                deadline=deadline,
                clock=clock,
                sleep_for=sleep_for,
            )
        except CatalogRevisionNotFoundError as error:
            # Core accepts only the current head.  A head advance between any
            # two bounded artifact-name or pagination reads invalidates the
            # whole metadata map before a PATCH can be constructed.  Discard
            # every observation from that pass and retry from a fresh head.
            previous_book_ids = None
            previous_series_ids = None
            previous_catalog_revision = None
            stable_since = None
            logger.info(
                "Catalog head advanced during pinned reconciliation; "
                "restarting from the current head"
            )
            remaining_seconds = deadline - clock()
            if remaining_seconds <= 0:
                raise TimeoutError(
                    f"Timed out after {timeout_seconds:g}s waiting for Komga "
                    f"library {active_client.library_id} to settle"
                ) from error
            sleep_for(min(poll_interval_seconds, remaining_seconds))
            continue
        series_ids = active_client.get_series_ids(
            timeout_seconds=_remaining_seconds(
                deadline, clock, operation="series pagination"
            )
        )
        observed_revision = catalog_reader.get_catalog_revision()
        observed_at = clock()
        if observed_at >= deadline:
            raise TimeoutError(
                f"Timed out after {timeout_seconds:g}s waiting for Komga library "
                f"{active_client.library_id} to settle"
            )

        ids_changed = (
            previous_book_ids is not None
            and previous_series_ids is not None
            and (book_ids != previous_book_ids or series_ids != previous_series_ids)
        )
        revision_changed = observed_revision.revision != revision.revision or (
            previous_catalog_revision is not None
            and previous_catalog_revision != revision.revision
        )
        if metadata_was_stable and not revision_changed:
            if stable_since is None or ids_changed:
                stable_since = observed_at
            stable_for = observed_at - stable_since
            if stable_for >= stable_observation_seconds:
                final_revision = catalog_reader.get_catalog_revision()
                if final_revision.revision == revision.revision:
                    logger.info(
                        "Library %s was stable for %.1fs at catalog revision %d; "
                        "sync complete",
                        active_client.library_id,
                        stable_for,
                        revision.revision,
                    )
                    return
                stable_since = None
                observed_revision = final_revision
                logger.info(
                    "Catalog revision changed during final stability check; "
                    "polling again"
                )
            logger.info(
                "Library %s stable for %.1f/%.1fs (%d books, %d series)",
                active_client.library_id,
                stable_for,
                stable_observation_seconds,
                len(book_ids),
                len(series_ids),
            )
        else:
            stable_since = None
            if revision_changed:
                logger.info(
                    "Catalog revision changed during reconciliation; polling again"
                )
            else:
                logger.info(
                    "Library %s changed during reconciliation; polling again",
                    active_client.library_id,
                )

        previous_book_ids, previous_series_ids = book_ids, series_ids
        previous_catalog_revision = observed_revision.revision
        remaining_seconds = deadline - clock()
        if remaining_seconds <= 0:
            raise TimeoutError(
                f"Timed out after {timeout_seconds:g}s waiting for Komga library "
                f"{active_client.library_id} to settle"
            )
        sleep_for(min(poll_interval_seconds, remaining_seconds))
