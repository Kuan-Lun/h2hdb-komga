from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256

from h2hdb import (
    DEFAULT_CATALOG_DISCOVERY_QUERY,
    CatalogArtifact,
    CatalogDiscoveryBundle,
    CatalogDiscoveryCursor,
    CatalogDiscoveryPage,
    CatalogDiscoveryQuery,
    CatalogFacetCursor,
    CatalogFacetKind,
    CatalogFacetPage,
    CatalogImageResource,
    CatalogPublication,
    CatalogPublicationPresentation,
    CatalogRecentOrder,
    CatalogRecentWindow,
    CatalogRevision,
    CatalogRevisionNotFoundError,
    StorageObjectDescriptor,
    StorageObjectKey,
)


def canonical_catalog_artifact(gid: int) -> CatalogArtifact:
    content = f"h2hdb-komga-test-artifact:{gid}".encode()
    digest = sha256(content).hexdigest()
    return CatalogArtifact(
        artifact_id=(f"urn:h2h:artifact:acquisition:{gid}:sha256:{digest}"),
        name=f"h2h-{gid}.cbz",
        storage_object=StorageObjectDescriptor(
            key=StorageObjectKey(
                codec="test-memory-v1",
                segments=("acquisitions", f"h2h-{gid}.cbz"),
            ),
            size_bytes=len(content),
            sha256=digest,
            modified_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        media_type="application/vnd.comicbook+zip",
    )


class FakeCatalogReader:
    def __init__(self, publications_by_name: Mapping[str, CatalogPublication]) -> None:
        self.publications_by_name = dict(publications_by_name)
        for name, publication in self.publications_by_name.items():
            if len(publication.artifacts) != 1 or publication.artifacts[0].name != name:
                raise ValueError(
                    "fake artifact lookup key must name the publication's sole "
                    "acquisition"
                )
        self.artifact_name_calls: list[tuple[str, ...]] = []
        self.artifact_revision_calls: list[int] = []
        self.artifact_revisions: list[CatalogRevision] = []
        self.revision_calls = 0
        self.current_revision = 1
        self.discovery_calls = 0

    def get_catalog_revision(self, revision: int | None = None) -> CatalogRevision:
        self.revision_calls += 1
        return self._revision_at(revision)

    def _revision_at(self, revision: CatalogRevision | int | None) -> CatalogRevision:
        selected = (
            revision.revision if isinstance(revision, CatalogRevision) else revision
        )
        if selected is not None and selected != self.current_revision:
            raise CatalogRevisionNotFoundError(selected)
        if isinstance(revision, CatalogRevision):
            return revision
        return CatalogRevision(
            revision=self.current_revision,
            published_at=datetime(2026, 8, 1, tzinfo=UTC),
            publication_count=len(self._publications()),
            artifact_count=len(self._publications()),
        )

    def _publications(self) -> tuple[CatalogPublication, ...]:
        return tuple(
            {
                publication.publication_id: publication
                for publication in self.publications_by_name.values()
            }.values()
        )

    def discover_publications(
        self,
        *,
        query: CatalogDiscoveryQuery = DEFAULT_CATALOG_DISCOVERY_QUERY,
        after: CatalogDiscoveryCursor | None = None,
        limit: int = 50,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogDiscoveryPage:
        self.discovery_calls += 1
        del query, after, limit, revision
        raise AssertionError("discovery feed must not be used by Komga sync")

    def discover_publications_with_facets(
        self,
        *,
        query: CatalogDiscoveryQuery = DEFAULT_CATALOG_DISCOVERY_QUERY,
        after: CatalogDiscoveryCursor | None = None,
        limit: int = 50,
        facet_limit: int = 128,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogDiscoveryBundle:
        del query, after, limit, facet_limit, revision
        raise AssertionError("discovery bundle must not be used by Komga sync")

    def list_publication_facets(
        self,
        *,
        facet: CatalogFacetKind,
        query: CatalogDiscoveryQuery = DEFAULT_CATALOG_DISCOVERY_QUERY,
        after: CatalogFacetCursor | None = None,
        limit: int = 50,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogFacetPage:
        del facet, query, after, limit, revision
        raise AssertionError("facet feed must not be used by Komga sync")

    def list_recent_publications(
        self,
        *,
        order: CatalogRecentOrder,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogRecentWindow:
        del order, revision
        raise AssertionError("recent feed must not be used by Komga sync")

    def get_publication(
        self,
        publication_id: str,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogPublication | None:
        del publication_id, revision
        raise AssertionError("get_publication must not be used by Komga sync")

    def get_publication_presentation(
        self,
        publication_id: str,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogPublicationPresentation | None:
        del publication_id, revision
        raise AssertionError("presentation must not be used by Komga sync")

    def get_publication_page(
        self,
        publication_id: str,
        page_index: int,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogImageResource | None:
        del publication_id, page_index, revision
        raise AssertionError("publication pages must not be used by Komga sync")

    def get_publications_by_artifact_names(
        self,
        names: Sequence[str],
        *,
        revision: CatalogRevision | int | None = None,
    ) -> Mapping[str, CatalogPublication]:
        assert revision is not None
        selected_revision = self._revision_at(revision)
        self.artifact_revision_calls.append(selected_revision.revision)
        self.artifact_revisions.append(selected_revision)
        self.artifact_name_calls.append(tuple(names))
        return {
            name: self.publications_by_name[name]
            for name in names
            if name in self.publications_by_name
        }

    def get_artifact(
        self,
        artifact_id: str,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogArtifact | None:
        del artifact_id, revision
        raise AssertionError("get_artifact must not be used by Komga sync")
