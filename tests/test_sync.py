import fcntl
import os
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests
from h2hdb import CatalogPublication, CatalogRevision

from h2hdb_komga.config_loader import KomgaConfig
from h2hdb_komga.coordination import LibraryUnavailable
from h2hdb_komga.metadata import KomgaMetadata, publication_to_komga_metadata
from h2hdb_komga.sync import sync_komga_library

from .helpers import FakeCatalogReader, canonical_catalog_artifact


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
    page_size = 500

    def __init__(self) -> None:
        self.books: dict[str, dict[str, Any]] = {
            "book-7": self.make_book(7),
        }
        self.series: dict[str, dict[str, Any]] = {
            "series-7": self.make_series(7),
        }
        self.patch_calls: list[dict[str, KomgaMetadata]] = []
        self.scan_calls = 0
        self.analyze_calls = 0
        self.book_page_calls: list[int] = []
        self.series_page_calls: list[int] = []
        self.book_snapshot_calls = 0
        self.series_snapshot_calls = 0
        self.get_book_calls: list[str] = []

    @classmethod
    def make_book(
        cls,
        gid: int,
        *,
        book_id: str | None = None,
        name: str | None = None,
        series_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": book_id or f"book-{gid}",
            "name": name or f"h2h-{gid}.cbz",
            "seriesId": series_id or f"series-{gid}",
            "libraryId": cls.library_id,
            "oneshot": True,
            "metadata": {},
        }

    @classmethod
    def make_series(
        cls,
        gid: int,
        *,
        series_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": series_id or f"series-{gid}",
            "libraryId": cls.library_id,
            "oneshot": True,
        }

    def get_book_page(
        self,
        page: int,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[dict[str, Any], ...]:
        assert timeout_seconds is None or timeout_seconds > 0
        self.book_page_calls.append(page)
        if page == 0:
            self.book_snapshot_calls += 1
        books = sorted(self.books.values(), key=lambda item: str(item["id"]))
        offset = page * self.page_size
        return tuple(deepcopy(books[offset : offset + self.page_size]))

    def get_series_page(
        self,
        page: int,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[dict[str, Any], ...]:
        assert timeout_seconds is None or timeout_seconds > 0
        self.series_page_calls.append(page)
        if page == 0:
            self.series_snapshot_calls += 1
        series = sorted(self.series.values(), key=lambda item: str(item["id"]))
        offset = page * self.page_size
        return tuple(deepcopy(series[offset : offset + self.page_size]))

    def get_book(
        self, book_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        assert timeout_seconds is None or timeout_seconds > 0
        self.get_book_calls.append(book_id)
        return deepcopy(self.books[book_id])

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


def _publication(gid: int = 7) -> CatalogPublication:
    return CatalogPublication(
        publication_id=f"urn:h2h:gallery:{gid}",
        gid=gid,
        source_gallery_name=f"Gallery {gid} [{gid}]",
        source_title=f"Gallery {gid}",
        title=f"Gallery {gid}",
        sort_title=f"gallery {gid}",
        summary=f"Published summary {gid}",
        language="en",
        published_at=datetime(2025, 1, 2, tzinfo=UTC),
        modified_at=datetime(2026, 8, 1, tzinfo=UTC),
        downloaded_at=datetime(2025, 1, 3, tzinfo=UTC),
        page_count=0,
        cover=None,
        thumbnail=None,
        artifacts=(canonical_catalog_artifact(gid),),
    )


def _reader(*gids: int) -> FakeCatalogReader:
    return FakeCatalogReader({f"h2h-{gid}.cbz": _publication(gid) for gid in gids})


def _config(
    tmp_path: Path,
    client: FakeKomgaClient,
    *,
    trigger_scan: bool = True,
) -> KomgaConfig:
    coordination_root = tmp_path / "coordination"
    coordination_root.mkdir(exist_ok=True)
    (coordination_root / "publication.lock").touch(exist_ok=True)
    return KomgaConfig(
        base_url="https://komga.invalid",
        api_username="user",
        api_password="password",
        library_id=client.library_id,
        coordination_root=coordination_root,
        trigger_scan=trigger_scan,
    )


def _sync(
    tmp_path: Path,
    reader: FakeCatalogReader,
    client: FakeKomgaClient,
    clock: FakeClock,
    *,
    trigger_scan: bool = True,
    stable_observation_seconds: float = 1,
    timeout_seconds: float = 10,
) -> None:
    sync_komga_library(
        _config(tmp_path, client, trigger_scan=trigger_scan),
        reader,
        client=client,
        clock=clock,
        sleep_for=clock.sleep,
        poll_interval_seconds=1,
        stable_observation_seconds=stable_observation_seconds,
        timeout_seconds=timeout_seconds,
    )


def test_sync_proves_exact_one_shot_set_before_patching(tmp_path: Path) -> None:
    publication = _publication()
    reader = _reader(7)
    client = FakeKomgaClient()
    clock = FakeClock()

    _sync(tmp_path, reader, client, clock)

    assert client.scan_calls == 1
    assert client.analyze_calls == 1
    assert client.book_snapshot_calls == 3
    assert client.series_snapshot_calls == 3
    assert reader.artifact_name_calls == [("h2h-7.cbz",)] * 3
    assert client.patch_calls == [
        {"book-7": publication_to_komga_metadata(publication)}
    ]
    assert clock.sleep_calls == [1, 1]


def test_exact_empty_catalog_and_komga_library_can_settle(tmp_path: Path) -> None:
    reader = _reader()
    client = FakeKomgaClient()
    client.books.clear()
    client.series.clear()
    clock = FakeClock()

    _sync(tmp_path, reader, client, clock, trigger_scan=False)

    assert client.book_snapshot_calls == 2
    assert client.series_snapshot_calls == 2
    assert reader.artifact_name_calls == []
    assert client.patch_calls == []
    assert clock.sleep_calls == [1]


def test_unchanged_ids_are_reconciled_when_analysis_rewrites_metadata(
    tmp_path: Path,
) -> None:
    publication = _publication()
    expected = publication_to_komga_metadata(publication)
    reader = _reader(7)
    clock = FakeClock()

    class AnalysisRewriteClient(FakeKomgaClient):
        def get_book_page(
            self,
            page: int,
            *,
            timeout_seconds: float | None = None,
        ) -> tuple[dict[str, Any], ...]:
            if page == 0 and self.book_snapshot_calls == 1:
                self.books["book-7"]["metadata"] = {}
            return super().get_book_page(page, timeout_seconds=timeout_seconds)

    client = AnalysisRewriteClient()
    client.books["book-7"]["metadata"] = deepcopy(expected)

    _sync(tmp_path, reader, client, clock, trigger_scan=False)

    assert client.book_snapshot_calls == 4
    assert client.patch_calls == [{"book-7": expected}]
    assert clock.sleep_calls == [1, 1, 1]


def test_partial_snapshot_polls_to_timeout_without_patching(tmp_path: Path) -> None:
    reader = _reader(7, 8)
    client = FakeKomgaClient()
    clock = FakeClock()

    with pytest.raises(TimeoutError, match="waiting for Komga library"):
        _sync(
            tmp_path,
            reader,
            client,
            clock,
            trigger_scan=False,
            timeout_seconds=3,
        )

    assert client.book_snapshot_calls == 3
    assert client.series_snapshot_calls == 0
    assert client.patch_calls == []


def test_extra_catalog_miss_polls_to_timeout_without_patching(tmp_path: Path) -> None:
    reader = _reader(7)
    client = FakeKomgaClient()
    client.books["book-8"] = client.make_book(8)
    client.series["series-8"] = client.make_series(8)
    clock = FakeClock()

    with pytest.raises(TimeoutError, match="waiting for Komga library"):
        _sync(
            tmp_path,
            reader,
            client,
            clock,
            trigger_scan=False,
            timeout_seconds=3,
        )

    assert all(len(call) <= 128 for call in reader.artifact_name_calls)
    assert client.series_snapshot_calls == 0
    assert client.patch_calls == []


def test_duplicate_canonical_name_across_pages_fails_closed(tmp_path: Path) -> None:
    reader = _reader(7, 8)
    client = FakeKomgaClient()
    client.page_size = 1
    client.books["book-duplicate"] = client.make_book(
        7,
        book_id="book-duplicate",
        series_id="series-duplicate",
    )
    client.series["series-duplicate"] = client.make_series(
        7,
        series_id="series-duplicate",
    )
    clock = FakeClock()

    with pytest.raises(TimeoutError, match="waiting for Komga library"):
        _sync(
            tmp_path,
            reader,
            client,
            clock,
            trigger_scan=False,
            timeout_seconds=3,
        )

    assert 1 in client.book_page_calls
    assert client.patch_calls == []


def test_noncanonical_book_name_fails_closed_without_catalog_lookup(
    tmp_path: Path,
) -> None:
    reader = _reader(7)
    client = FakeKomgaClient()
    client.books["book-7"]["name"] = "7.cbz"
    clock = FakeClock()

    with pytest.raises(TimeoutError, match="waiting for Komga library"):
        _sync(
            tmp_path,
            reader,
            client,
            clock,
            trigger_scan=False,
            timeout_seconds=3,
        )

    assert reader.artifact_name_calls == []
    assert client.patch_calls == []


def test_unreferenced_one_shot_series_fails_closed(tmp_path: Path) -> None:
    reader = _reader(7)
    client = FakeKomgaClient()
    client.series = {
        "series-other": client.make_series(7, series_id="series-other"),
    }
    clock = FakeClock()

    with pytest.raises(TimeoutError, match="waiting for Komga library"):
        _sync(
            tmp_path,
            reader,
            client,
            clock,
            trigger_scan=False,
            timeout_seconds=3,
        )

    assert client.patch_calls == []


def test_scan_request_timeout_fails_closed_and_next_run_retries(
    tmp_path: Path,
) -> None:
    reader = _reader(7)

    class ScanTimeoutClient(FakeKomgaClient):
        failures_remaining = 1

        def scan_library(self, *, timeout_seconds: float | None = None) -> None:
            assert timeout_seconds is not None and timeout_seconds > 0
            self.scan_calls += 1
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise requests.exceptions.ReadTimeout("response not confirmed")

    client = ScanTimeoutClient()
    with pytest.raises(requests.exceptions.Timeout, match="not confirmed"):
        _sync(tmp_path, reader, client, FakeClock())

    assert client.analyze_calls == 0
    assert client.book_page_calls == []
    assert client.patch_calls == []

    _sync(tmp_path, reader, client, FakeClock())

    assert client.scan_calls == 2
    assert client.analyze_calls == 1


def test_delayed_scan_result_resets_exact_set_observation_window(
    tmp_path: Path,
) -> None:
    reader = _reader(7)
    clock = FakeClock()

    class DelayedScanClient(FakeKomgaClient):
        def __init__(self) -> None:
            super().__init__()
            self.delayed_book = self.books.pop("book-7")
            self.delayed_series = self.series.pop("series-7")

        def get_book_page(
            self,
            page: int,
            *,
            timeout_seconds: float | None = None,
        ) -> tuple[dict[str, Any], ...]:
            if page == 0 and self.book_snapshot_calls == 2:
                self.books["book-7"] = self.delayed_book
                self.series["series-7"] = self.delayed_series
            return super().get_book_page(page, timeout_seconds=timeout_seconds)

    client = DelayedScanClient()

    _sync(
        tmp_path,
        reader,
        client,
        clock,
        stable_observation_seconds=2,
    )

    assert client.book_snapshot_calls == 6
    assert client.patch_calls == [
        {"book-7": publication_to_komga_metadata(_publication())}
    ]
    assert clock.sleep_calls == [1, 1, 1, 1, 1]


def test_transient_book_page_failure_retries_on_later_poll(tmp_path: Path) -> None:
    reader = _reader(7)
    clock = FakeClock()

    class TransientPageClient(FakeKomgaClient):
        failures_remaining = 1

        def get_book_page(
            self,
            page: int,
            *,
            timeout_seconds: float | None = None,
        ) -> tuple[dict[str, Any], ...]:
            if page == 0 and self.failures_remaining:
                self.failures_remaining -= 1
                self.book_page_calls.append(page)
                self.book_snapshot_calls += 1
                raise requests.exceptions.ConnectionError("temporary failure")
            return super().get_book_page(page, timeout_seconds=timeout_seconds)

    client = TransientPageClient()

    _sync(tmp_path, reader, client, clock, trigger_scan=False)

    assert client.book_snapshot_calls == 4
    assert client.patch_calls == [
        {"book-7": publication_to_komga_metadata(_publication())}
    ]
    assert clock.sleep_calls == [1, 1, 1]


def test_settling_times_out_when_book_page_never_succeeds(tmp_path: Path) -> None:
    reader = _reader(7)
    clock = FakeClock()

    class FailingPageClient(FakeKomgaClient):
        def get_book_page(
            self,
            page: int,
            *,
            timeout_seconds: float | None = None,
        ) -> tuple[dict[str, Any], ...]:
            assert timeout_seconds is not None and timeout_seconds > 0
            self.book_page_calls.append(page)
            self.book_snapshot_calls += 1
            raise requests.exceptions.ConnectionError("still unavailable")

    client = FailingPageClient()

    with pytest.raises(TimeoutError, match="waiting for Komga library"):
        _sync(
            tmp_path,
            reader,
            client,
            clock,
            trigger_scan=False,
            timeout_seconds=3,
        )

    assert client.book_snapshot_calls == 3
    assert client.patch_calls == []
    assert clock.sleep_calls == [1, 1, 1]


def test_patch_timeout_is_verified_instead_of_assumed_failed(tmp_path: Path) -> None:
    reader = _reader(7)
    clock = FakeClock()

    class ResponseLostAfterPatchClient(FakeKomgaClient):
        failures_remaining = 1

        def patch_books_metadata(
            self,
            metadata_by_book_id: dict[str, KomgaMetadata],
            *,
            timeout_seconds: float | None = None,
        ) -> None:
            super().patch_books_metadata(
                metadata_by_book_id,
                timeout_seconds=timeout_seconds,
            )
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise requests.exceptions.ReadTimeout("response lost")

    client = ResponseLostAfterPatchClient()

    _sync(tmp_path, reader, client, clock, trigger_scan=False)

    assert len(client.patch_calls) == 1
    assert client.get_book_calls == ["book-7"]


def test_catalog_revision_change_restarts_pinned_exact_set_pass(
    tmp_path: Path,
) -> None:
    class ChangingCatalogReader(FakeCatalogReader):
        def get_catalog_revision(self, revision: int | None = None) -> CatalogRevision:
            if self.revision_calls == 1:
                self.current_revision = 2
            return super().get_catalog_revision(revision)

    reader = ChangingCatalogReader({"h2h-7.cbz": _publication()})
    client = FakeKomgaClient()
    client.books["book-7"]["metadata"] = publication_to_komga_metadata(_publication())
    clock = FakeClock()

    _sync(tmp_path, reader, client, clock, trigger_scan=False)

    assert reader.artifact_revision_calls[0] == 1
    assert set(reader.artifact_revision_calls[1:]) == {2}
    assert clock.sleep_calls == [1, 1]


def test_head_advance_during_bounded_catalog_lookup_discards_whole_pass(
    tmp_path: Path,
) -> None:
    gids = tuple(range(1, 130))

    class ArtifactLookupRaceReader(FakeCatalogReader):
        def __init__(self) -> None:
            super().__init__({f"h2h-{gid}.cbz": _publication(gid) for gid in gids})
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
    client.books = {f"book-{gid}": client.make_book(gid) for gid in gids}
    client.series = {f"series-{gid}": client.make_series(gid) for gid in gids}
    clock = FakeClock()

    _sync(tmp_path, reader, client, clock, trigger_scan=False)

    assert reader.attempted_revisions[:2] == [1, 1]
    assert set(reader.attempted_revisions[2:]) == {2}
    assert all(len(call) <= 128 for call in reader.artifact_name_calls)
    assert len(client.patch_calls) == 1
    assert set(client.patch_calls[0]) == set(client.books)


def test_large_exact_set_uses_bounded_pages_lookups_and_patch_batches(
    tmp_path: Path,
) -> None:
    gids = tuple(range(1, 502))
    reader = _reader(*gids)
    client = FakeKomgaClient()
    client.books = {f"book-{gid}": client.make_book(gid) for gid in gids}
    client.series = {f"series-{gid}": client.make_series(gid) for gid in gids}

    _sync(tmp_path, reader, client, FakeClock(), trigger_scan=False)

    assert set(client.book_page_calls) == {0, 1, 2}
    assert set(client.series_page_calls) == {0, 1, 2}
    assert all(1 <= len(call) <= 128 for call in reader.artifact_name_calls)
    assert len(client.patch_calls) == 3
    assert all(1 <= len(call) <= 200 for call in client.patch_calls)


def test_hard_deadline_is_checked_inside_paginated_snapshot(tmp_path: Path) -> None:
    reader = _reader(7)
    clock = FakeClock()

    class SlowPaginationClient(FakeKomgaClient):
        def get_book_page(
            self,
            page: int,
            *,
            timeout_seconds: float | None = None,
        ) -> tuple[dict[str, Any], ...]:
            assert timeout_seconds is not None
            clock.now += timeout_seconds + 1
            return super().get_book_page(page, timeout_seconds=timeout_seconds)

    client = SlowPaginationClient()

    with pytest.raises(TimeoutError, match="book pagination"):
        _sync(
            tmp_path,
            reader,
            client,
            clock,
            trigger_scan=False,
            timeout_seconds=3,
        )

    assert client.patch_calls == []


def test_sync_holds_shared_lock_for_complete_komga_lifecycle(
    tmp_path: Path,
) -> None:
    reader = _reader(7)
    clock = FakeClock()
    lock_path = tmp_path / "coordination" / "publication.lock"

    class LockCheckingClient(FakeKomgaClient):
        def __init__(self) -> None:
            super().__init__()
            self.locked_operations: set[str] = set()

        def _observe_shared_lock(self, operation: str) -> None:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            self.locked_operations.add(operation)

        def scan_library(self, *, timeout_seconds: float | None = None) -> None:
            self._observe_shared_lock("scan")
            super().scan_library(timeout_seconds=timeout_seconds)

        def analyze_library(self, *, timeout_seconds: float | None = None) -> None:
            self._observe_shared_lock("analyze")
            super().analyze_library(timeout_seconds=timeout_seconds)

        def get_book_page(
            self,
            page: int,
            *,
            timeout_seconds: float | None = None,
        ) -> tuple[dict[str, Any], ...]:
            self._observe_shared_lock("books")
            return super().get_book_page(page, timeout_seconds=timeout_seconds)

        def get_series_page(
            self,
            page: int,
            *,
            timeout_seconds: float | None = None,
        ) -> tuple[dict[str, Any], ...]:
            self._observe_shared_lock("series")
            return super().get_series_page(page, timeout_seconds=timeout_seconds)

        def patch_books_metadata(
            self,
            metadata_by_book_id: dict[str, KomgaMetadata],
            *,
            timeout_seconds: float | None = None,
        ) -> None:
            self._observe_shared_lock("metadata")
            super().patch_books_metadata(
                metadata_by_book_id,
                timeout_seconds=timeout_seconds,
            )

        def get_book(
            self, book_id: str, *, timeout_seconds: float | None = None
        ) -> dict[str, Any]:
            self._observe_shared_lock("verification")
            return super().get_book(book_id, timeout_seconds=timeout_seconds)

    client = LockCheckingClient()

    _sync(tmp_path, reader, client, clock)

    assert client.locked_operations == {
        "scan",
        "analyze",
        "books",
        "series",
        "metadata",
        "verification",
    }


def test_exclusive_publication_lock_prevents_sync_before_komga_calls(
    tmp_path: Path,
) -> None:
    reader = _reader(7)
    client = FakeKomgaClient()
    config = _config(tmp_path, client)
    descriptor = os.open(
        config.coordination_root / "publication.lock",
        os.O_RDWR | os.O_CLOEXEC,
    )
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(LibraryUnavailable, match="temporarily unavailable"):
            sync_komga_library(config, reader, client=client)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert client.scan_calls == 0
    assert client.book_page_calls == []


def test_activation_marker_prevents_sync_before_komga_calls(tmp_path: Path) -> None:
    reader = _reader(7)
    client = FakeKomgaClient()
    config = _config(tmp_path, client)
    (config.coordination_root / "ACTIVATING").touch()

    with pytest.raises(LibraryUnavailable, match="activating"):
        sync_komga_library(config, reader, client=client)

    assert client.scan_calls == 0
    assert client.book_page_calls == []
