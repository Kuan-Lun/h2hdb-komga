from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from h2hdb import (
    CatalogArtifact,
    CatalogPage,
    CatalogPublication,
    CatalogRevision,
    CatalogRevisionNotFoundError,
)


class FakeCatalogReader:
    def __init__(self, publications_by_name: Mapping[str, CatalogPublication]) -> None:
        self.publications_by_name = dict(publications_by_name)
        self.artifact_name_calls: list[tuple[str, ...]] = []
        self.artifact_revision_calls: list[int] = []
        self.artifact_revisions: list[CatalogRevision] = []
        self.revision_calls = 0
        self.current_revision = 1
        self.list_calls: list[tuple[int, int, int]] = []

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
        )

    def _publications(self) -> tuple[CatalogPublication, ...]:
        return tuple(
            {
                publication.publication_id: publication
                for publication in self.publications_by_name.values()
            }.values()
        )

    def list_publications(
        self,
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int = 50,
        revision: CatalogRevision | int | None = None,
        require_artifact: bool = False,
    ) -> CatalogPage:
        assert query is None and not require_artifact
        assert revision is not None
        assert 1 <= limit <= 128
        selected_revision = self._revision_at(revision)
        publications = self._publications()
        self.list_calls.append((offset, limit, selected_revision.revision))
        return CatalogPage(
            revision=selected_revision,
            publications=publications[offset : offset + limit],
            offset=offset,
            limit=limit,
            total=len(publications),
        )

    def get_publication(
        self,
        publication_id: str,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogPublication | None:
        del publication_id, revision
        raise AssertionError("get_publication must not be used by Komga sync")

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
