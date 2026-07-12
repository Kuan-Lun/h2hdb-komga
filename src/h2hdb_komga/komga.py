__all__ = ["KomgaClient", "PATCH_TIMEOUT_SECONDS", "REQUEST_TIMEOUT_SECONDS"]

import logging
from time import monotonic
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from .config_loader import KomgaConfig

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


class KomgaClient:
    # Every method raises requests exceptions on failure; deciding how to
    # react (skip, verify, retry) is the caller's job.

    def __init__(self, config: KomgaConfig) -> None:
        self.library_id = config.library_id
        self._base_url = config.base_url
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(config.api_username, config.api_password)

    def _paginate_ids(self, path: str) -> set[str]:
        ids = set[str]()
        page_num = 0
        last_logged_at = monotonic()
        while True:
            params: dict[str, str | int] = {
                "library_id": self.library_id,
                "page": page_num,
                "size": PAGE_SIZE,
            }
            response = self._session.get(
                f"{self._base_url}{path}",
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            content = response.json()["content"]
            if not content:
                return ids
            ids.update(item["id"] for item in content)
            page_num += 1
            now = monotonic()
            if now - last_logged_at >= PAGINATION_LOG_INTERVAL_SECONDS:
                logger.info("Listed %d id(s) so far (page %d)", len(ids), page_num)
                last_logged_at = now

    def get_book_ids(self) -> set[str]:
        return self._paginate_ids("/api/v1/books")

    def get_series_ids(self) -> set[str]:
        return self._paginate_ids("/api/v1/series")

    def get_book(self, book_id: str) -> dict[str, Any]:
        response = self._session.get(
            f"{self._base_url}/api/v1/books/{book_id}",
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        book: dict[str, Any] = response.json()
        return book

    def patch_books_metadata(
        self, metadata_by_book_id: dict[str, dict[str, Any]]
    ) -> None:
        response = self._session.patch(
            f"{self._base_url}/api/v1/books/metadata",
            json=metadata_by_book_id,
            timeout=PATCH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

    def scan_library(self) -> None:
        response = self._session.post(
            f"{self._base_url}/api/v1/libraries/{self.library_id}/scan",
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

    def analyze_library(self) -> None:
        response = self._session.post(
            f"{self._base_url}/api/v1/libraries/{self.library_id}/analyze",
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
