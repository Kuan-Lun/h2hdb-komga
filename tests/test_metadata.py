from datetime import UTC, datetime

from h2hdb import CatalogContributor, CatalogPublication, CatalogSubject

from h2hdb_komga.metadata import publication_to_komga_metadata
from h2hdb_komga.sync import _get_catalog_metadata_by_book_names

from .helpers import FakeCatalogReader


def _publication() -> CatalogPublication:
    return CatalogPublication(
        publication_id="urn:h2h:gallery:42",
        gid=42,
        source_gallery_name="[Artist] Friendly Gallery [42]",
        source_title="Raw Gallery Title",
        title="Display Fallback Must Not Be Used",
        sort_title="a published gallery",
        summary="Catalog summary",
        language="en",
        published_at=datetime(2024, 2, 3, 4, 5, tzinfo=UTC),
        modified_at=datetime(2026, 8, 1, tzinfo=UTC),
        contributors=(CatalogContributor(name="An Uploader", role="uploader"),),
        subjects=(
            CatalogSubject(name="An Artist", scheme="h2h:tag:artist", code="artist"),
            CatalogSubject(name="glasses", scheme="h2h:tag:female", code="female"),
            CatalogSubject(name="english", scheme="h2h:tag:language", code="language"),
            CatalogSubject(name="", scheme="h2h:tag:misc", code="misc"),
            CatalogSubject(name="award winner", scheme="urn:subject"),
        ),
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
        contributors=publication.contributors,
        subjects=publication.subjects,
        artifacts=publication.artifacts,
        redownload_required=publication.redownload_required,
    )

    metadata = publication_to_komga_metadata(publication)

    assert "title" not in metadata
    assert "tags" not in metadata


def test_artifact_lookup_accepts_komga_name_without_cbz_suffix() -> None:
    publication = _publication()
    reader = FakeCatalogReader({"42-content-address.cbz": publication})

    result = _get_catalog_metadata_by_book_names(
        reader,
        ["42-content-address", "missing", "42-content-address"],
    )

    assert result == {"42-content-address": publication_to_komga_metadata(publication)}
    assert reader.artifact_name_calls == [
        (
            "42-content-address",
            "42-content-address.cbz",
            "missing",
            "missing.cbz",
        )
    ]
    assert reader.list_calls == []


def test_friendly_gallery_name_resolves_by_gid_through_public_reader() -> None:
    publication = _publication()
    reader = FakeCatalogReader({"42-content-address.cbz": publication})

    result = _get_catalog_metadata_by_book_names(
        reader,
        [
            "[Artist] Friendly Gallery [42]",
            "[Artist] Friendly Gallery [42].cbz",
            "42",
            f"42-{'ab' * 32}.cbz",
        ],
    )

    expected = publication_to_komga_metadata(publication)
    assert result == {
        "[Artist] Friendly Gallery [42]": expected,
        "[Artist] Friendly Gallery [42].cbz": expected,
        "42": expected,
        f"42-{'ab' * 32}.cbz": expected,
    }
    assert reader.revision_calls == 1
    assert reader.list_calls == [(0, 200, 1)]
