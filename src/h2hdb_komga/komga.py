__all__ = ["KomgaClient", "PATCH_TIMEOUT_SECONDS", "REQUEST_TIMEOUT_SECONDS"]

import logging
from time import monotonic, sleep
from typing import Any, cast

import requests
from requests.auth import HTTPBasicAuth

from .config_loader import KomgaConfig
from .metadata import KomgaMetadata

logger = logging.getLogger(__name__)

PAGE_SIZE = 500
# Plain GETs/POSTs should come back quickly -- timeout aggressively rather
# than hang forever if Komga stops responding mid-run.
REQUEST_TIMEOUT_SECONDS = 30
# A 200-book bulk PATCH needs a generous budget: concurrent bulk-PATCH load
# can slow requests several-fold without Komga actually hanging.
PATCH_TIMEOUT_SECONDS = 300
# Pagination has no known total up front, so progress can only be time-based.
PAGINATION_LOG_INTERVAL_SECONDS = 30
# A single page fetch can fail from a transient Komga-side hiccup (e.g.
# contention while a scan is still running) -- retrying just that page is far
# cheaper than re-running the whole paginated listing.
PAGE_FETCH_RETRY_ATTEMPTS = 3
PAGE_FETCH_RETRY_DELAY_SECONDS = 5


def _bounded_timeout(limit: float, remaining_seconds: float | None) -> float:
    if remaining_seconds is None:
        return limit
    if remaining_seconds <= 0:
        raise TimeoutError("Komga synchronization deadline expired")
    return min(limit, remaining_seconds)


class KomgaClient:
    # Every method raises requests exceptions on failure; deciding how to
    # react (skip, verify, retry) is the caller's job.

    def __init__(self, config: KomgaConfig) -> None:
        self.library_id = config.library_id
        self._base_url = config.base_url
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(config.api_username, config.api_password)

    def _get_page(
        self,
        path: str,
        params: dict[str, str | int],
        *,
        deadline: float | None,
    ) -> list[dict[str, Any]]:
        attempt = 1
        while True:
            try:
                response = self._session.get(
                    f"{self._base_url}{path}",
                    params=params,
                    timeout=_bounded_timeout(
                        REQUEST_TIMEOUT_SECONDS,
                        None if deadline is None else deadline - monotonic(),
                    ),
                )
                response.raise_for_status()
                content: list[dict[str, Any]] = response.json()["content"]
                return content
            except requests.exceptions.RequestException as e:
                is_client_error = (
                    isinstance(e, requests.exceptions.HTTPError)
                    and e.response is not None
                    and e.response.status_code < 500
                )
                if is_client_error or attempt >= PAGE_FETCH_RETRY_ATTEMPTS:
                    raise
                logger.warning(
                    "Page fetch %s (page %s) failed (attempt %d/%d): %s; retrying",
                    path,
                    params["page"],
                    attempt,
                    PAGE_FETCH_RETRY_ATTEMPTS,
                    e,
                )
                remaining = None if deadline is None else deadline - monotonic()
                sleep(_bounded_timeout(PAGE_FETCH_RETRY_DELAY_SECONDS, remaining))
                attempt += 1

    def _paginate_ids(
        self, path: str, *, timeout_seconds: float | None = None
    ) -> set[str]:
        ids = set[str]()
        page_num = 0
        last_logged_at = monotonic()
        deadline = None if timeout_seconds is None else monotonic() + timeout_seconds
        while True:
            params: dict[str, str | int] = {
                "library_id": self.library_id,
                "page": page_num,
                "size": PAGE_SIZE,
            }
            content = self._get_page(path, params, deadline=deadline)
            if not content:
                return ids
            ids.update(item["id"] for item in content)
            page_num += 1
            now = monotonic()
            if now - last_logged_at >= PAGINATION_LOG_INTERVAL_SECONDS:
                logger.info("Listed %d id(s) so far (page %d)", len(ids), page_num)
                last_logged_at = now

    def get_book_ids(self, *, timeout_seconds: float | None = None) -> set[str]:
        return self._paginate_ids("/api/v1/books", timeout_seconds=timeout_seconds)

    def get_series_ids(self, *, timeout_seconds: float | None = None) -> set[str]:
        return self._paginate_ids("/api/v1/series", timeout_seconds=timeout_seconds)

    def get_book(
        self, book_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        response = self._session.get(
            f"{self._base_url}/api/v1/books/{book_id}",
            timeout=_bounded_timeout(REQUEST_TIMEOUT_SECONDS, timeout_seconds),
        )
        response.raise_for_status()
        book: dict[str, Any] = response.json()
        return book

    def patch_books_metadata(
        self,
        metadata_by_book_id: dict[str, KomgaMetadata],
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        response = self._session.patch(
            f"{self._base_url}/api/v1/books/metadata",
            json=cast(Any, metadata_by_book_id),
            timeout=_bounded_timeout(PATCH_TIMEOUT_SECONDS, timeout_seconds),
        )
        response.raise_for_status()

    def scan_library(self, *, timeout_seconds: float | None = None) -> None:
        response = self._session.post(
            f"{self._base_url}/api/v1/libraries/{self.library_id}/scan",
            timeout=_bounded_timeout(REQUEST_TIMEOUT_SECONDS, timeout_seconds),
        )
        response.raise_for_status()

    def analyze_library(self, *, timeout_seconds: float | None = None) -> None:
        response = self._session.post(
            f"{self._base_url}/api/v1/libraries/{self.library_id}/analyze",
            timeout=_bounded_timeout(REQUEST_TIMEOUT_SECONDS, timeout_seconds),
        )
        response.raise_for_status()
