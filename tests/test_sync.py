from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
import requests
from h2hdb import CatalogPublication, CatalogRevision

from h2hdb_komga.config_loader import KomgaConfig
from h2hdb_komga.metadata import KomgaMetadata, publication_to_komga_metadata
from h2hdb_komga.sync import sync_komga_library

from .helpers import FakeCatalogReader


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleep_calls: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds


class FakeKomgaClient:
    library_id = "library-1"

    def __init__(self) -> None:
        self.books: dict[str, dict[str, Any]] = {
            "book-found": {"name": "h2h-7.cbz", "metadata": {}},
            "book-missing": {"name": "h2h-8.cbz", "metadata": {}},
        }
        self.patch_calls: list[dict[str, KomgaMetadata]] = []
        self.scan_calls = 0
        self.analyze_calls = 0
        self.book_id_calls = 0
        self.series_id_calls = 0
        self.get_book_calls: list[str] = []

    def get_book_ids(self, *, timeout_seconds: float | None = None) -> set[str]:
        assert timeout_seconds is None or timeout_seconds > 0
        self.book_id_calls += 1
        return set(self.books)

    def get_series_ids(self, *, timeout_seconds: float | None = None) -> set[str]:
        assert timeout_seconds is None or timeout_seconds > 0
        self.series_id_calls += 1
        return {"series-1"}

    def get_book(
        self, book_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        assert timeout_seconds is None or timeout_seconds > 0
        self.get_book_calls.append(book_id)
        return self.books[book_id]

    def patch_books_metadata(
        self,
        metadata_by_book_id: dict[str, KomgaMetadata],
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        assert timeout_seconds is None or timeout_seconds > 0
        self.patch_calls.append(deepcopy(metadata_by_book_id))
        for book_id, metadata in metadata_by_book_id.items():
            self.books[book_id]["metadata"].update(deepcopy(metadata))

    def scan_library(self, *, timeout_seconds: float | None = None) -> None:
        assert timeout_seconds is None or timeout_seconds > 0
        self.scan_calls += 1

    def analyze_library(self, *, timeout_seconds: float | None = None) -> None:
        assert timeout_seconds is None or timeout_seconds > 0
        self.analyze_calls += 1


def _publication() -> CatalogPublication:
    return CatalogPublication(
        publication_id="urn:h2h:gallery:7",
        gid=7,
        source_gallery_name="Seven [7]",
        source_title="Seven",
        title="Seven",
        sort_title="seven",
        summary="Published summary",
        language="en",
        published_at=datetime(2025, 1, 2, tzinfo=UTC),
        modified_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _config(client: FakeKomgaClient, *, trigger_scan: bool = True) -> KomgaConfig:
    return KomgaConfig(
        base_url="https://komga.invalid",
        api_username="user",
        api_password="password",
        library_id=client.library_id,
        trigger_scan=trigger_scan,
    )


def test_sync_uses_injected_reader_and_client_and_skips_missing_books() -> None:
    publication = _publication()
    reader = FakeCatalogReader({"h2h-7.cbz": publication})
    client = FakeKomgaClient()
    clock = FakeClock()

    sync_komga_library(
        _config(client),
        reader,
        client=client,
        clock=clock,
        sleep_for=clock.sleep,
        poll_interval_seconds=1,
        stable_observation_seconds=1,
        timeout_seconds=10,
    )

    assert client.scan_calls == 1
    assert client.analyze_calls == 1
    assert client.book_id_calls == 3
    assert client.series_id_calls == 3
    assert len(reader.artifact_name_calls) == 3
    assert all(
        set(call) == {"h2h-7.cbz", "h2h-8.cbz"} for call in reader.artifact_name_calls
    )
    assert client.patch_calls == [
        {"book-found": publication_to_komga_metadata(publication)}
    ]
    assert client.books["book-missing"]["metadata"] == {}
    assert clock.sleep_calls == [1, 1]


def test_unchanged_ids_are_reconciled_when_analyze_rewrites_metadata() -> None:
    publication = _publication()
    expected = publication_to_komga_metadata(publication)
    reader = FakeCatalogReader({"h2h-7.cbz": publication})
    clock = FakeClock()

    class AnalyzeRewriteClient(FakeKomgaClient):
        def get_book_ids(self, *, timeout_seconds: float | None = None) -> set[str]:
            book_ids = super().get_book_ids(timeout_seconds=timeout_seconds)
            if self.book_id_calls == 2:
                self.books["book-found"]["metadata"] = {}
            return book_ids

    client = AnalyzeRewriteClient()
    client.books["book-found"]["metadata"] = deepcopy(expected)

    sync_komga_library(
        _config(client, trigger_scan=False),
        reader,
        client=client,
        clock=clock,
        sleep_for=clock.sleep,
        poll_interval_seconds=1,
        stable_observation_seconds=1,
        timeout_seconds=10,
    )

    assert client.book_id_calls == 4
    assert client.series_id_calls == 4
    assert client.patch_calls == [{"book-found": expected}]
    assert len(reader.artifact_name_calls) == 4
    assert clock.sleep_calls == [1, 1, 1]


def test_delayed_scan_results_reset_the_stable_observation_window() -> None:
    publication = _publication()
    reader = FakeCatalogReader({"h2h-7.cbz": publication})
    clock = FakeClock()

    class DelayedScanClient(FakeKomgaClient):
        def __init__(self) -> None:
            super().__init__()
            self.delayed_book = self.books.pop("book-found")

        def get_book_ids(self, *, timeout_seconds: float | None = None) -> set[str]:
            assert timeout_seconds is None or timeout_seconds > 0
            self.book_id_calls += 1
            if self.book_id_calls == 3:
                self.books["book-found"] = self.delayed_book
            return set(self.books)

    client = DelayedScanClient()

    sync_komga_library(
        _config(client),
        reader,
        client=client,
        clock=clock,
        sleep_for=clock.sleep,
        poll_interval_seconds=1,
        stable_observation_seconds=2,
        timeout_seconds=10,
    )

    assert client.book_id_calls == 6
    assert client.series_id_calls == 6
    assert client.patch_calls == [
        {"book-found": publication_to_komga_metadata(publication)}
    ]
    assert clock.sleep_calls == [1, 1, 1, 1, 1]


def test_transient_book_fetch_failure_is_retried_on_later_poll() -> None:
    publication = _publication()
    reader = FakeCatalogReader({"h2h-7.cbz": publication})
    clock = FakeClock()

    class TransientFetchClient(FakeKomgaClient):
        failures_remaining = 1

        def get_book(
            self, book_id: str, *, timeout_seconds: float | None = None
        ) -> dict[str, Any]:
            assert timeout_seconds is None or timeout_seconds > 0
            self.get_book_calls.append(book_id)
            if book_id == "book-found" and self.failures_remaining:
                self.failures_remaining -= 1
                raise requests.exceptions.ConnectionError("temporary failure")
            return self.books[book_id]

    client = TransientFetchClient()

    sync_komga_library(
        _config(client, trigger_scan=False),
        reader,
        client=client,
        clock=clock,
        sleep_for=clock.sleep,
        poll_interval_seconds=1,
        stable_observation_seconds=1,
        timeout_seconds=10,
    )

    assert client.get_book_calls.count("book-found") >= 5
    assert client.patch_calls == [
        {"book-found": publication_to_komga_metadata(publication)}
    ]
    assert clock.sleep_calls == [1, 1, 1]


def test_settling_times_out_when_book_fetch_never_succeeds() -> None:
    publication = _publication()
    reader = FakeCatalogReader({"h2h-7.cbz": publication})
    clock = FakeClock()

    class FailingFetchClient(FakeKomgaClient):
        def __init__(self) -> None:
            super().__init__()
            self.books = {"book-found": self.books["book-found"]}

        def get_book(
            self, book_id: str, *, timeout_seconds: float | None = None
        ) -> dict[str, Any]:
            assert timeout_seconds is None or timeout_seconds > 0
            self.get_book_calls.append(book_id)
            raise requests.exceptions.ConnectionError("still unavailable")

    client = FailingFetchClient()

    with pytest.raises(TimeoutError, match="waiting for Komga library"):
        sync_komga_library(
            _config(client, trigger_scan=False),
            reader,
            client=client,
            clock=clock,
            sleep_for=clock.sleep,
            poll_interval_seconds=1,
            stable_observation_seconds=1,
            timeout_seconds=3,
        )

    assert client.get_book_calls == ["book-found"] * 3
    assert client.patch_calls == []
    assert clock.sleep_calls == [1, 1, 1]


def test_catalog_revision_change_resets_settling_and_pins_each_pass() -> None:
    publication = _publication()

    class ChangingCatalogReader(FakeCatalogReader):
        def get_catalog_revision(self, revision: int | None = None) -> CatalogRevision:
            if self.revision_calls == 1:
                self.current_revision = 2
            return super().get_catalog_revision(revision)

    reader = ChangingCatalogReader({"h2h-7.cbz": publication})
    client = FakeKomgaClient()
    client.books["book-found"]["metadata"] = publication_to_komga_metadata(publication)
    clock = FakeClock()

    sync_komga_library(
        _config(client, trigger_scan=False),
        reader,
        client=client,
        clock=clock,
        sleep_for=clock.sleep,
        poll_interval_seconds=1,
        stable_observation_seconds=1,
        timeout_seconds=10,
    )

    assert reader.artifact_revision_calls[0] == 1
    assert set(reader.artifact_revision_calls[1:]) == {2}
    assert clock.sleep_calls == [1, 1]


def test_head_advance_during_artifact_name_lookup_restarts_whole_pass() -> None:
    publication = _publication()
    book_names = tuple(f"h2h-{index}.cbz" for index in range(1, 130))

    class ArtifactLookupRaceReader(FakeCatalogReader):
        def __init__(self) -> None:
            super().__init__(dict.fromkeys(book_names, publication))
            self.attempted_revisions: list[int] = []

        def get_publications_by_artifact_names(
            self,
            names: Sequence[str],
            *,
            revision: CatalogRevision | int | None = None,
        ) -> Mapping[str, CatalogPublication]:
            assert isinstance(revision, CatalogRevision)
            self.attempted_revisions.append(revision.revision)
            if len(self.attempted_revisions) == 2:
                self.current_revision = 2
            return super().get_publications_by_artifact_names(
                names,
                revision=revision,
            )

    reader = ArtifactLookupRaceReader()
    client = FakeKomgaClient()
    client.books = {
        f"book-{index:03}": {"name": name, "metadata": {}}
        for index, name in enumerate(book_names)
    }
    clock = FakeClock()

    sync_komga_library(
        _config(client, trigger_scan=False),
        reader,
        client=client,
        clock=clock,
        sleep_for=clock.sleep,
        poll_interval_seconds=1,
        stable_observation_seconds=1,
        timeout_seconds=10,
    )

    assert reader.attempted_revisions[:2] == [1, 1]
    assert set(reader.attempted_revisions[2:]) == {2}
    assert reader.artifact_revision_calls[0] == 1
    assert set(reader.artifact_revision_calls[1:]) == {2}
    assert len(client.patch_calls) == 1
    assert set(client.patch_calls[0]) == set(client.books)


def test_hard_deadline_is_checked_inside_a_reconciliation_pass() -> None:
    publication = _publication()
    reader = FakeCatalogReader({"h2h-7.cbz": publication})
    clock = FakeClock()

    class SlowPaginationClient(FakeKomgaClient):
        def get_book_ids(self, *, timeout_seconds: float | None = None) -> set[str]:
            assert timeout_seconds is not None
            clock.now += timeout_seconds + 1
            return super().get_book_ids(timeout_seconds=timeout_seconds)

    client = SlowPaginationClient()

    with pytest.raises(TimeoutError, match="waiting for Komga library"):
        sync_komga_library(
            _config(client, trigger_scan=False),
            reader,
            client=client,
            clock=clock,
            sleep_for=clock.sleep,
            poll_interval_seconds=1,
            stable_observation_seconds=1,
            timeout_seconds=3,
        )
