__all__ = ["scan_komga_library"]

# swagger-ui/index.html
from time import sleep
from typing import Callable

import requests
from requests.auth import HTTPBasicAuth

from h2hdb import H2HDB
from h2hdb.config_loader import H2HDBConfig
from h2hdb.sql_connector import DatabaseKeyError

from .config_loader import KomgaConfig
from .threading_tools import KomgaThreadsList


def retry_request(request, retries: int = 3):
    def wrapper(*args, **kwargs):
        if retries < 0:
            return
        else:
            try:
                return request(*args, **kwargs)
            except requests.exceptions.SSLError:
                pass
            except requests.exceptions.RequestException as e:
                retry_codes = []  # Add more codes to this list as needed
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
def get_books_ids_in_series_id(
    series_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> set[str]:
    books_informations = list[tuple[str, str]]()
    page_num = 0
    while True:
        url = f"{base_url}/api/v1/series/{series_id}/books?page={page_num}&size=1000"
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
) -> dict:
    url = f"{base_url}/api/v1/books/{book_id}"
    response = requests.get(url, auth=HTTPBasicAuth(api_username, api_password))
    response.raise_for_status()
    return response.json()


@retry_request
def patch_book_metadata(
    metadata: dict,
    book_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> None:
    url = f"{base_url}/api/v1/books/{book_id}/metadata"
    response = requests.patch(
        url,
        json=metadata,
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


@retry_request
def get_series(
    series_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> dict:
    url = f"{base_url}/api/v1/series/{series_id}"
    response = requests.get(url, auth=HTTPBasicAuth(api_username, api_password))
    response.raise_for_status()
    return response.json()


@retry_request
def patch_series_metadata(
    metadata: dict,
    series_id: str,
    base_url: str,
    api_username: str,
    api_password: str,
) -> None:
    url = f"{base_url}/api/v1/series/{series_id}/metadata"
    response = requests.patch(
        url,
        json=metadata,
        auth=HTTPBasicAuth(api_username, api_password),
    )
    response.raise_for_status()


def update_komga_book_metadata(
    komgaconfig: KomgaConfig, h2hconfig: H2HDBConfig, book_id: str
) -> None:
    base_url = komgaconfig.base_url
    api_username = komgaconfig.api_username
    api_password = komgaconfig.api_password

    komga_metadata = get_book(book_id, base_url, api_username, api_password)
    if komga_metadata is not None:
        try:
            with H2HDB(config=h2hconfig) as connector:
                current_metadata = connector.get_komga_metadata(komga_metadata["name"])
            if not (current_metadata.items() <= komga_metadata.items()):
                patch_book_metadata(
                    current_metadata, book_id, base_url, api_username, api_password
                )
        except DatabaseKeyError:
            pass


def update_komga_series_metadata(
    komgaconfig: KomgaConfig, h2hconfig: H2HDBConfig, series_id: str
) -> None:
    base_url = komgaconfig.base_url
    api_username = komgaconfig.api_username
    api_password = komgaconfig.api_password

    books_ids = get_books_ids_in_series_id(
        series_id, base_url, api_username, api_password
    )

    ischecktitle = False
    for book_id in books_ids:
        komga_metadata = get_book(book_id, base_url, api_username, api_password)
        if komga_metadata is not None:
            try:
                with H2HDB(config=h2hconfig) as connector:
                    current_metadata = connector.get_komga_metadata(
                        komga_metadata["name"]
                    )
                ischecktitle = True
                break
            except DatabaseKeyError:
                continue

    if ischecktitle:
        series_title = get_series(series_id, base_url, api_username, api_password)[
            "metadata"
        ]["title"]
        if series_title == current_metadata["releaseDate"]:
            pass
        else:
            patch_series_metadata(
                {"title": current_metadata["releaseDate"]},
                series_id,
                base_url,
                api_username,
                api_password,
            )


def scan_komga_library(
    komgaconfig: KomgaConfig,
    h2hconfig: H2HDBConfig,
    previously_book_ids=set[str](),
    previously_series_ids=set[str](),
) -> None:
    library_id = komgaconfig.library_id
    base_url = komgaconfig.base_url
    api_username = komgaconfig.api_username
    api_password = komgaconfig.api_password

    scan_library(library_id, base_url, api_username, api_password)
    analyze_library(library_id, base_url, api_username, api_password)

    def update_metadata(
        vset: set[str],
        exclude_vset: set[str],
        update_fun: Callable[[KomgaConfig, H2HDBConfig, str], None],
    ) -> None:
        vset = vset - exclude_vset
        with KomgaThreadsList() as threads:
            for v in vset:
                threads.append(update_fun, (komgaconfig, h2hconfig, v))

    books_ids = get_books_ids_in_library_id(
        library_id, base_url, api_username, api_password
    )
    update_metadata(books_ids, previously_book_ids, update_komga_book_metadata)

    series_ids = get_series_ids(library_id, base_url, api_username, api_password)
    update_metadata(series_ids, previously_series_ids, update_komga_series_metadata)

    if (books_ids != previously_book_ids) or (series_ids != previously_series_ids):
        scan_komga_library(komgaconfig, h2hconfig, books_ids, series_ids)
