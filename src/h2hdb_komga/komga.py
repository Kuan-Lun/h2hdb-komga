__all__ = ["scan_komga_library"]

# swagger-ui/index.html
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import sleep
from typing import Any

import requests
from h2hdb import H2HDB
from h2hdb.config_loader import H2HDBConfig
from requests.auth import HTTPBasicAuth

from .config_loader import KomgaConfig

logger = logging.getLogger(__name__)

KOMGA_MAX_WORKERS = 10
# Bounds each PATCH request body regardless of library size -- one request
# bundling a whole library-wide pass risks a body-size limit (Komga's own,
# or a reverse proxy's).
BOOK_METADATA_PATCH_CHUNK_SIZE = 200
# Plain GETs/POSTs should come back quickly -- timeout aggressively rather
# than hang forever if Komga stops responding mid-run.
REQUEST_TIMEOUT_SECONDS = 30
# A 200-book chunk needs a generous budget: concurrent bulk-PATCH load can
# slow requests several-fold without Komga actually hanging.
PATCH_TIMEOUT_SECONDS = 300
# Re-patches only the books still unverified after a full attempt (not the
# whole batch), up to this many times, with a pause between attempts.
PATCH_RETRY_ATTEMPTS = 3
PATCH_RETRY_DELAY_SECONDS = 30


def retry_request(request: Callable[..., Any], retries: int = 3) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if retries < 0:
            return
        else:
            try:
                return request(*args, **kwargs)
            except requests.exceptions.SSLError:
                pass
            except requests.exceptions.RequestException as e:
                retry_codes: list[str] = []  # Add more codes to this list as needed
                if any(code in str(e) for code in retry_codes):
                    logger.warning("%s -- retrying (%d left)", e, retries)
                    sleep(5)
                    return retry_request(request, retries - 1)(*args, **kwargs)
                else:
                    logger.error("%s -- giving up (not in retry_codes)", e)
                    return  # Don't retry

    return wrapper


@retry_request
def get_series_ids(
    library_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> set[str]:
    series_informations = list[tuple[str, str]]()
    page_num = 0
    while True:
        url = (
            f"{base_url}/api/v1/series?library_id={library_id}&page={page_num}&size=500"
        )
        response = requests.get(
            url,
            auth=HTTPBasicAuth(api_username, api_password),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        response_json = response.json()
        if len(response_json["content"]) == 0:
            break
        for series in response_json["content"]:
            series_informations.append((series["id"], series["fileLastModified"]))
        page_num += 1
    series_ids = {s[0] for s in sorted(series_informations, key=lambda x: x[1])}
    return series_ids


@retry_request
def get_books_ids_in_library_id(
    library_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> set[str]:
    books_informations = list[tuple[str, str]]()
    page_num = 0
    while True:
        url = (
            f"{base_url}/api/v1/books?library_id={library_id}&page={page_num}&size=500"
        )
        response = requests.get(
            url,
            auth=HTTPBasicAuth(api_username, api_password),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        response_json = response.json()
        if len(response_json["content"]) == 0:
            break
        for book in response_json["content"]:
            books_informations.append((book["id"], book["fileLastModified"]))
        page_num += 1
    books_ids = {b[0] for b in sorted(books_informations, key=lambda x: x[1])}
    return books_ids


@retry_request
def get_books_ids_in_all_libraries(
    base_url: str, api_username: str, api_password: str
) -> set[str]:
    books_informations = list[tuple[str, str]]()
    page_num = 0
    while True:
        url = f"{base_url}/api/v1/books?page={page_num}&size=100"
        response = requests.get(
            url,
            auth=HTTPBasicAuth(api_username, api_password),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        response_json = response.json()
        if len(response_json["content"]) == 0:
            break
        for book in response_json["content"]:
            books_informations.append((book["id"], book["fileLastModified"]))
        page_num += 1
    # Sort by fileLastModified in descending order
    books_ids = {
        b[0] for b in sorted(books_informations, key=lambda x: x[1], reverse=True)
    }
    return books_ids


@retry_request
def get_book(
    book_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> dict[str, Any]:
    url = f"{base_url}/api/v1/books/{book_id}"
    response = requests.get(
        url,
        auth=HTTPBasicAuth(api_username, api_password),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    book: dict[str, Any] = response.json()
    return book


def patch_books_metadata(
    metadata_by_book_id: dict[str, dict[str, Any]],
    base_url: str,
    api_username: str,
    api_password: str,
) -> None:
    # Deliberately not @retry_request -- the caller (_patch_chunk) needs the
    # real exception to tell a timeout from a hard failure.
    url = f"{base_url}/api/v1/books/metadata"
    response = requests.patch(
        url,
        json=metadata_by_book_id,
        auth=HTTPBasicAuth(api_username, api_password),
        timeout=PATCH_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _book_metadata_is_up_to_date(
    expected_metadata: dict[str, Any], book: dict[str, Any]
) -> bool:
    return bool(expected_metadata.items() <= book["metadata"].items())


def _find_unverified_books(
    expected_metadata_by_book_id: dict[str, dict[str, Any]],
    base_url: str,
    api_username: str,
    api_password: str,
) -> list[str]:
    # A 204 only confirms the bulk request was accepted, not that every book
    # in it was actually applied -- re-fetching and diffing is the only way
    # to confirm a given book's write landed.
    # Runs once per attempt after all chunks finish, not nested inside chunk
    # dispatch, so this pool doesn't multiply concurrency against Komga.
    with ThreadPoolExecutor(max_workers=KOMGA_MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                get_book, book_id, base_url, api_username, api_password
            ): book_id
            for book_id in expected_metadata_by_book_id
        }
        return sorted(
            book_id
            for future, book_id in futures.items()
            if (book := future.result()) is None
            or not _book_metadata_is_up_to_date(
                expected_metadata_by_book_id[book_id], book
            )
        )


def _patch_chunk(
    chunk: dict[str, dict[str, Any]],
    base_url: str,
    api_username: str,
    api_password: str,
) -> None:
    try:
        patch_books_metadata(chunk, base_url, api_username, api_password)
    except requests.exceptions.Timeout:
        logger.warning(
            "PATCH for %d book(s) timed out client-side after %ds; will verify "
            "and retry if it didn't actually land",
            len(chunk),
            PATCH_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as e:
        logger.error("PATCH for %d book(s) failed: %s", len(chunk), e)


@retry_request
def download_book(
    book_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> bytes:
    url = f"{base_url}/api/v1/books/{book_id}/file"
    response = requests.get(
        url,
        auth=HTTPBasicAuth(api_username, api_password),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.content


@retry_request
def scan_library(
    library_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> None:
    url = f"{base_url}/api/v1/libraries/{library_id}/scan"
    response = requests.post(
        url,
        auth=HTTPBasicAuth(api_username, api_password),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


@retry_request
def analyze_library(
    library_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> None:
    url = f"{base_url}/api/v1/libraries/{library_id}/analyze"
    response = requests.post(
        url,
        auth=HTTPBasicAuth(api_username, api_password),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def get_h2hdb_metadata_by_gallery_names(
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


def scan_komga_library(
    komgaconfig: KomgaConfig,
    h2hconfig: H2HDBConfig,
    previously_book_ids: set[str] = set(),
    previously_series_ids: set[str] = set(),
) -> None:
    library_id = komgaconfig.library_id
    base_url = komgaconfig.base_url
    api_username = komgaconfig.api_username
    api_password = komgaconfig.api_password

    if komgaconfig.trigger_scan:
        logger.info("Triggering scan and analyze for library %s", library_id)
        scan_library(library_id, base_url, api_username, api_password)
        analyze_library(library_id, base_url, api_username, api_password)

    def update_books_metadata(vset: set[str], exclude_vset: set[str]) -> None:
        vset = vset - exclude_vset
        if not vset:
            logger.info("No new books to check in library %s", library_id)
            return

        logger.info("Fetching Komga metadata for %d book(s)", len(vset))
        komga_metadata_by_book_id: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=KOMGA_MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    get_book, book_id, base_url, api_username, api_password
                ): book_id
                for book_id in vset
            }
            for future, book_id in futures.items():
                komga_metadata = future.result()
                if komga_metadata is not None:
                    komga_metadata_by_book_id[book_id] = komga_metadata

        h2hdb_metadata_by_name = get_h2hdb_metadata_by_gallery_names(
            h2hconfig,
            [m["name"] for m in komga_metadata_by_book_id.values()],
        )

        updates: dict[str, dict[str, Any]] = {}
        for book_id, komga_metadata in komga_metadata_by_book_id.items():
            current_metadata = h2hdb_metadata_by_name.get(komga_metadata["name"])
            # BookDto nests title/summary/releaseDate/authors under
            # "metadata" -- comparing against komga_metadata itself would
            # never match.
            if current_metadata is not None and not _book_metadata_is_up_to_date(
                current_metadata, komga_metadata
            ):
                updates[book_id] = current_metadata

        logger.info(
            "%d of %d book(s) are out of date",
            len(updates),
            len(komga_metadata_by_book_id),
        )
        if not updates:
            return

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
                    executor.submit(
                        _patch_chunk, chunk, base_url, api_username, api_password
                    )
                    for chunk in chunks
                ]
                for future in as_completed(chunk_futures):
                    future.result()

            unverified_ids = _find_unverified_books(
                remaining, base_url, api_username, api_password
            )
            if not unverified_ids:
                logger.info("All %d book(s) patched and verified", len(remaining))
                remaining = {}
                break

            remaining = {book_id: remaining[book_id] for book_id in unverified_ids}
            logger.warning(
                "%d book(s) still not verified after attempt %d/%d",
                len(remaining),
                attempt,
                PATCH_RETRY_ATTEMPTS,
            )
            if attempt < PATCH_RETRY_ATTEMPTS:
                sleep(PATCH_RETRY_DELAY_SECONDS)

        if remaining:
            raise RuntimeError(
                f"Komga metadata update did not verify for {len(remaining)} "
                f"book(s): {', '.join(sorted(remaining))}"
            )

    books_ids = get_books_ids_in_library_id(
        library_id, base_url, api_username, api_password
    )
    update_books_metadata(books_ids, previously_book_ids)

    # Series titles are left as Komga's own defaults (folder name, or the
    # wrapped book's name for oneshots) -- this listing is only used to
    # detect whether the library is still settling after scan/analyze.
    series_ids = get_series_ids(library_id, base_url, api_username, api_password)

    if (books_ids != previously_book_ids) or (series_ids != previously_series_ids):
        logger.info(
            "Library %s changed since last pass (%d books, %d series); " "re-scanning",
            library_id,
            len(books_ids),
            len(series_ids),
        )
        scan_komga_library(komgaconfig, h2hconfig, books_ids, series_ids)
    else:
        logger.info("Library %s settled; sync complete", library_id)
