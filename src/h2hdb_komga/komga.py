__all__ = ["scan_komga_library"]

# swagger-ui/index.html
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from typing import Any

import requests
from h2hdb import H2HDB
from h2hdb.config_loader import H2HDBConfig
from requests.auth import HTTPBasicAuth

from .config_loader import KomgaConfig

KOMGA_MAX_WORKERS = 10


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
                    sleep(5)
                    return retry_request(request, retries - 1)(*args, **kwargs)
                else:
                    print(e)
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
        response = requests.get(url, auth=HTTPBasicAuth(api_username, api_password))
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
        response = requests.get(url, auth=HTTPBasicAuth(api_username, api_password))
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
        response = requests.get(url, auth=HTTPBasicAuth(api_username, api_password))
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
    response = requests.get(url, auth=HTTPBasicAuth(api_username, api_password))
    response.raise_for_status()
    book: dict[str, Any] = response.json()
    return book


@retry_request
def patch_books_metadata(
    metadata_by_book_id: dict[str, dict[str, Any]],
    base_url: str,
    api_username: str,
    api_password: str,
) -> None:
    url = f"{base_url}/api/v1/books/metadata"
    response = requests.patch(
        url,
        json=metadata_by_book_id,
        auth=HTTPBasicAuth(api_username, api_password),
    )
    response.raise_for_status()


@retry_request
def download_book(
    book_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> bytes:
    url = f"{base_url}/api/v1/books/{book_id}/file"
    response = requests.get(url, auth=HTTPBasicAuth(api_username, api_password))
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
    response = requests.post(url, auth=HTTPBasicAuth(api_username, api_password))
    response.raise_for_status()


@retry_request
def analyze_library(
    library_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> None:
    url = f"{base_url}/api/v1/libraries/{library_id}/analyze"
    response = requests.post(url, auth=HTTPBasicAuth(api_username, api_password))
    response.raise_for_status()


def get_h2hdb_metadata_by_gallery_names(
    h2hconfig: H2HDBConfig, gallery_names: list[str]
) -> dict[str, dict[str, Any]]:
    # H2HDB.get_komga_metadata() raises a plain KeyError (not DatabaseKeyError)
    # for any gallery name it doesn't recognize, and fails the whole batch
    # rather than skipping just that one name. Retry with the offending name
    # removed until the batch succeeds, since a Komga book without a matching
    # H2HDB gallery is an expected, not exceptional, case.
    names = list(dict.fromkeys(gallery_names))
    with H2HDB(config=h2hconfig) as connector:
        while names:
            try:
                return connector.get_komga_metadata(names)
            except KeyError as e:
                names.remove(e.args[0])
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
        scan_library(library_id, base_url, api_username, api_password)
        analyze_library(library_id, base_url, api_username, api_password)

    def update_books_metadata(vset: set[str], exclude_vset: set[str]) -> None:
        vset = vset - exclude_vset
        if not vset:
            return

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
            # "metadata"; comparing against komga_metadata itself (the
            # top-level BookDto) would never match, since those keys don't
            # exist at that level.
            if current_metadata is not None and not (
                current_metadata.items() <= komga_metadata["metadata"].items()
            ):
                updates[book_id] = current_metadata
        if updates:
            patch_books_metadata(updates, base_url, api_username, api_password)

    books_ids = get_books_ids_in_library_id(
        library_id, base_url, api_username, api_password
    )
    update_books_metadata(books_ids, previously_book_ids)

    # Series titles are left as Komga's own defaults (the folder name, or
    # the wrapped book's name for oneshots) — nothing to sync there. The
    # series listing is only used to detect whether the library is still
    # settling after scan_library()/analyze_library(), via the recursion
    # check below.
    series_ids = get_series_ids(library_id, base_url, api_username, api_password)

    if (books_ids != previously_book_ids) or (series_ids != previously_series_ids):
        scan_komga_library(komgaconfig, h2hconfig, books_ids, series_ids)
