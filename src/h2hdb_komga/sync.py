__all__ = ["sync_komga_library"]

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import sleep
from typing import Any

import requests
from h2hdb import H2HDB
from h2hdb.config_loader import H2HDBConfig

from .config_loader import KomgaConfig
from .komga import PATCH_TIMEOUT_SECONDS, KomgaClient

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


def _get_h2hdb_metadata_by_gallery_names(
    h2hconfig: H2HDBConfig, gallery_names: list[str]
) -> dict[str, dict[str, Any]]:
    # get_komga_metadata() raises a plain KeyError for any unrecognized
    # gallery name and fails the whole batch -- retry with the offending name
    # removed, since a Komga book with no matching H2HDB gallery is expected.
    names = list(dict.fromkeys(gallery_names))
    skipped = 0
    with H2HDB(config=h2hconfig) as connector:
        while names:
            try:
                result = connector.get_komga_metadata(names)
                if skipped:
                    logger.info(
                        "%d gallery name(s) had no matching H2HDB entry", skipped
                    )
                return result
            except KeyError as e:
                names.remove(e.args[0])
                skipped += 1
    return {}


def _book_metadata_is_up_to_date(
    expected_metadata: dict[str, Any], book: dict[str, Any]
) -> bool:
    return bool(expected_metadata.items() <= book["metadata"].items())


def _fetch_books(client: KomgaClient, book_ids: set[str]) -> dict[str, dict[str, Any]]:
    books: dict[str, dict[str, Any]] = {}
    failed = 0
    with ThreadPoolExecutor(max_workers=KOMGA_MAX_WORKERS) as executor:
        futures = {
            executor.submit(client.get_book, book_id): book_id for book_id in book_ids
        }
        for future in as_completed(futures):
            book_id = futures[future]
            try:
                books[book_id] = future.result()
            except requests.exceptions.RequestException as e:
                failed += 1
                logger.debug("Failed to fetch book %s: %s", book_id, e)
    if failed:
        logger.warning("Failed to fetch %d of %d book(s)", failed, len(book_ids))
    return books


def _patch_chunk(client: KomgaClient, chunk: dict[str, dict[str, Any]]) -> None:
    try:
        client.patch_books_metadata(chunk)
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
    client: KomgaClient, expected_metadata_by_book_id: dict[str, dict[str, Any]]
) -> list[str]:
    # A 204 only confirms the bulk request was accepted, not that every book
    # in it was actually applied -- re-fetching and diffing is the only way
    # to confirm a given book's write landed.
    def is_verified(book_id: str, expected_metadata: dict[str, Any]) -> bool:
        try:
            book = client.get_book(book_id)
        except requests.exceptions.RequestException:
            return False
        return _book_metadata_is_up_to_date(expected_metadata, book)

    # Runs once per attempt after all chunks finish, not nested inside chunk
    # dispatch, so this pool doesn't multiply concurrency against Komga.
    with ThreadPoolExecutor(max_workers=KOMGA_MAX_WORKERS) as executor:
        futures = {
            executor.submit(is_verified, book_id, expected_metadata): book_id
            for book_id, expected_metadata in expected_metadata_by_book_id.items()
        }
        return sorted(
            book_id for future, book_id in futures.items() if not future.result()
        )


def _patch_with_retries(
    client: KomgaClient, updates: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    remaining = updates
    for attempt in range(1, PATCH_RETRY_ATTEMPTS + 1):
        remaining_ids = list(remaining)
        chunks = [
            {
                book_id: remaining[book_id]
                for book_id in remaining_ids[i : i + BOOK_METADATA_PATCH_CHUNK_SIZE]
            }
            for i in range(0, len(remaining_ids), BOOK_METADATA_PATCH_CHUNK_SIZE)
        ]
        logger.info(
            "Attempt %d/%d: patching %d book(s) in %d chunk(s)",
            attempt,
            PATCH_RETRY_ATTEMPTS,
            len(remaining),
            len(chunks),
        )
        with ThreadPoolExecutor(max_workers=KOMGA_MAX_WORKERS) as executor:
            chunk_futures = [
                executor.submit(_patch_chunk, client, chunk) for chunk in chunks
            ]
            for future in as_completed(chunk_futures):
                future.result()

        unverified_ids = _find_unverified_books(client, remaining)
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
            sleep(PATCH_RETRY_DELAY_SECONDS)
    return remaining


def _update_books_metadata(
    client: KomgaClient, h2hconfig: H2HDBConfig, book_ids: set[str]
) -> None:
    if not book_ids:
        logger.info("No new books to check in library %s", client.library_id)
        return

    logger.info("Fetching Komga metadata for %d book(s)", len(book_ids))
    books = _fetch_books(client, book_ids)

    h2hdb_metadata_by_name = _get_h2hdb_metadata_by_gallery_names(
        h2hconfig, [book["name"] for book in books.values()]
    )

    # BookDto nests title/summary/releaseDate/authors under "metadata" --
    # comparing against the top-level BookDto would never match.
    updates = {
        book_id: expected_metadata
        for book_id, book in books.items()
        if (expected_metadata := h2hdb_metadata_by_name.get(book["name"])) is not None
        and not _book_metadata_is_up_to_date(expected_metadata, book)
    }
    logger.info("%d of %d book(s) are out of date", len(updates), len(books))
    if not updates:
        return

    remaining = _patch_with_retries(client, updates)
    if remaining:
        raise RuntimeError(
            f"Komga metadata update did not verify for {len(remaining)} "
            f"book(s): {', '.join(sorted(remaining))}"
        )


def sync_komga_library(komgaconfig: KomgaConfig, h2hconfig: H2HDBConfig) -> None:
    client = KomgaClient(komgaconfig)

    if komgaconfig.trigger_scan:
        logger.info("Triggering scan and analyze for library %s", client.library_id)
        client.scan_library()
        client.analyze_library()

    # scan_library/analyze_library are asynchronous jobs on Komga's side, so
    # keep re-diffing until a pass finds the book/series listings unchanged
    # -- only then has the library settled.
    previous_book_ids: set[str] = set()
    previous_series_ids: set[str] = set()
    while True:
        book_ids = client.get_book_ids()
        _update_books_metadata(client, h2hconfig, book_ids - previous_book_ids)

        series_ids = client.get_series_ids()
        if book_ids == previous_book_ids and series_ids == previous_series_ids:
            logger.info("Library %s settled; sync complete", client.library_id)
            return

        logger.info(
            "Library %s changed since last pass (%d books, %d series); re-scanning",
            client.library_id,
            len(book_ids),
            len(series_ids),
        )
        previous_book_ids, previous_series_ids = book_ids, series_ids
