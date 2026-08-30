from datetime import UTC, datetime

import pytest
from h2hdb import (
    CatalogContributor,
    CatalogPublication,
    CatalogReader,
    CatalogSubject,
)

from h2hdb_komga.metadata import publication_to_komga_metadata
from h2hdb_komga.sync import _get_catalog_metadata_by_book_names

from .helpers import FakeCatalogReader, canonical_catalog_artifact


def _publication(gid: int = 42) -> CatalogPublication:
    return CatalogPublication(
        publication_id=f"urn:h2h:gallery:{gid}",
        gid=gid,
        source_gallery_name=f"[Artist] Friendly Gallery [{gid}]",
        source_title="Raw Gallery Title",
        title="Display Fallback Must Not Be Used",
        sort_title="a published gallery",
        summary="Catalog summary",
        language="en",
        published_at=datetime(2024, 2, 3, 4, 5, tzinfo=UTC),
        modified_at=datetime(2026, 8, 1, tzinfo=UTC),
        downloaded_at=datetime(2025, 6, 7, 8, 9, tzinfo=UTC),
        page_count=0,
        cover=None,
        thumbnail=None,
        contributors=(CatalogContributor(name="An Uploader", role="uploader"),),
        subjects=(
            CatalogSubject(name="An Artist", scheme="h2h:tag:artist", code="artist"),
            CatalogSubject(name="glasses", scheme="h2h:tag:female", code="female"),
            CatalogSubject(name="english", scheme="h2h:tag:language", code="language"),
            CatalogSubject(name="", scheme="h2h:tag:misc", code="misc"),
            CatalogSubject(name="award winner", scheme="urn:subject"),
        ),
        artifacts=(canonical_catalog_artifact(gid),),
    )


def test_publication_maps_only_h2h_catalog_fields_to_komga_metadata() -> None:
    assert publication_to_komga_metadata(_publication()) == {
        "title": "Raw Gallery Title",
        "summary": "Catalog summary",
        "releaseDate": "2024-02-03",
        "authors": [
            {"name": "An Artist", "role": "artist"},
            {"name": "glasses", "role": "female"},
            {"name": "english", "role": "language"},
            {"name": "42", "role": "gid"},
        ],
    }


def test_blank_raw_title_does_not_overwrite_komga_title() -> None:
    publication = _publication()
    publication = CatalogPublication(
        publication_id=publication.publication_id,
        gid=publication.gid,
        source_gallery_name=publication.source_gallery_name,
        source_title="  ",
        title="Generated display fallback",
        sort_title=publication.sort_title,
        summary=publication.summary,
        language=publication.language,
        published_at=publication.published_at,
        modified_at=publication.modified_at,
        downloaded_at=publication.downloaded_at,
        page_count=publication.page_count,
        cover=publication.cover,
        thumbnail=publication.thumbnail,
        contributors=publication.contributors,
        subjects=publication.subjects,
        artifacts=publication.artifacts,
        redownload_required=publication.redownload_required,
    )

    metadata = publication_to_komga_metadata(publication)

    assert "title" not in metadata
    assert "tags" not in metadata


def test_canonical_artifact_lookup_accepts_komga_name_without_cbz_suffix() -> None:
    publication = _publication()
    artifact_name = "h2h-42.cbz"
    reader = FakeCatalogReader({artifact_name: publication})

    result = _get_catalog_metadata_by_book_names(
        reader,
        ["h2h-42", "missing", "missing"],
    )

    assert result == {"h2h-42": publication_to_komga_metadata(publication)}
    assert reader.artifact_name_calls == [(artifact_name,)]
    assert reader.discovery_calls == 0


def test_artifact_lookup_chunks_129_canonical_names_on_one_pinned_revision() -> None:
    artifact_names = [f"h2h-{gid}.cbz" for gid in range(1, 130)]
    reader = FakeCatalogReader(
        {f"h2h-{gid}.cbz": _publication(gid) for gid in range(1, 130)}
    )
    revision = reader.get_catalog_revision()

    result = _get_catalog_metadata_by_book_names(
        reader,
        artifact_names,
        revision=revision,
    )

    assert len(result) == 129
    assert tuple(len(batch) for batch in reader.artifact_name_calls) == (128, 1)
    assert all(observed is revision for observed in reader.artifact_revisions)


def test_fake_catalog_reader_implements_the_complete_public_protocol() -> None:
    reader = FakeCatalogReader({"h2h-42.cbz": _publication()})
    public_reader: CatalogReader = reader

    assert isinstance(public_reader, CatalogReader)


def test_fake_catalog_reader_requires_exact_acquisition_identity() -> None:
    publication = _publication()
    artifact = publication.artifacts[0]
    digest = artifact.storage_object.sha256

    assert artifact.artifact_id == (f"urn:h2h:artifact:acquisition:42:sha256:{digest}")
    assert artifact.name == "h2h-42.cbz"
    with pytest.raises(ValueError, match="sole acquisition"):
        FakeCatalogReader({"h2h-43.cbz": publication})


def test_legacy_and_malformed_names_are_not_catalog_lookup_keys() -> None:
    publication = _publication()
    reader = FakeCatalogReader({"h2h-42.cbz": publication})

    result = _get_catalog_metadata_by_book_names(
        reader,
        [
            "[Artist] Friendly Gallery [42]",
            "[Artist] Friendly Gallery [42].cbz",
            "42",
            f"42-{'ab' * 32}.cbz",
            "h2h-0.cbz",
            "h2h-042.cbz",
            "H2H-42.CBZ",
            " h2h-42.cbz ",
            "h2h-4٢.cbz",
            f"h2h-{1 << 63}.cbz",
        ],
    )

    assert result == {}
    assert reader.revision_calls == 1
    assert reader.artifact_name_calls == []
    assert reader.discovery_calls == 0
