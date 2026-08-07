from typing import NotRequired, TypedDict

from h2hdb import CatalogPublication


class KomgaAuthor(TypedDict):
    name: str
    role: str


class KomgaMetadata(TypedDict):
    title: NotRequired[str]
    summary: str
    releaseDate: str
    authors: list[KomgaAuthor]


H2H_TAG_SCHEME_PREFIX = "h2h:tag:"


def _gallery_tag_author(
    subject_name: str,
    scheme: str | None,
    code: str | None,
) -> KomgaAuthor | None:
    if (
        not subject_name
        or scheme is None
        or not scheme.startswith(H2H_TAG_SCHEME_PREFIX)
    ):
        return None
    role = code or scheme.removeprefix(H2H_TAG_SCHEME_PREFIX)
    if not role:
        return None
    return KomgaAuthor(name=subject_name, role=role)


def publication_to_komga_metadata(
    publication: CatalogPublication,
) -> KomgaMetadata:
    """Translate a core catalog publication to Komga's metadata shape."""
    authors = [
        author
        for subject in publication.subjects
        if (
            author := _gallery_tag_author(
                subject.name,
                subject.scheme,
                subject.code,
            )
        )
        is not None
    ]
    authors.append(KomgaAuthor(name=str(publication.gid), role="gid"))

    metadata = KomgaMetadata(
        summary=publication.summary,
        releaseDate=publication.published_at.date().isoformat(),
        authors=authors,
    )
    if publication.source_title.strip():
        metadata["title"] = publication.source_title
    return metadata
