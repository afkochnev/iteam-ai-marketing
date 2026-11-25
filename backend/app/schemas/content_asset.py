from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from app.models.content_asset import ContentAssetKind

AssetTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
ArticleBody = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100_000)
]
CreativeBrief = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5_000)
]
BrandProfile = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)
]
AltText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
CreativeNotes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1_000)]
UsageRights = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)
]


class StudioTextFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def reject_null_character(cls, value: object) -> object:
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("Text contains a null character.")
        return value


class ArticleVersionData(StudioTextFields):
    title: AssetTitle
    body: ArticleBody


class ArticleDraft(ArticleVersionData):
    source_version_id: UUID | None = None
    illustration_version_id: UUID | None = None


class ArticleRevision(ArticleDraft):
    expected_current_version_id: UUID


class IllustrationDetails(StudioTextFields):
    title: AssetTitle
    brief: CreativeBrief
    brand_profile: BrandProfile
    alt_text: AltText
    usage_rights: UsageRights
    caption: CreativeNotes | None = None


class IllustrationDraft(IllustrationDetails):
    source_version_id: UUID | None = None


class IllustrationGenerationRequest(IllustrationDraft):
    asset_id: UUID | None = None
    expected_current_version_id: UUID | None = None


class IllustrationProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["UPLOAD", "GENERATED"]
    model: str | None = None
    prompt: str | None = None
    provider_request_id: str | None = None
    generation_size: str | None = None


class CreativeMediaObject(BaseModel):
    sha256: str
    width: int
    height: int
    mime_type: str
    size_bytes: int


class CreativeMediaVariant(CreativeMediaObject):
    name: str


class IllustrationVersionData(IllustrationDetails):
    original: CreativeMediaObject
    variants: list[CreativeMediaVariant]
    provenance: IllustrationProvenance


class ContentAssetApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    approved_at: datetime
    approved_by: UUID


class ContentAssetVersionResponse(BaseModel):
    id: UUID
    asset_id: UUID
    kind: ContentAssetKind
    version_number: int
    supersedes_version_id: UUID | None
    source_version_id: UUID | None
    illustration_version_id: UUID | None
    snapshot_hash: str
    created_by: UUID
    created_at: datetime
    approval: ContentAssetApprovalResponse | None
    payload: ArticleVersionData | IllustrationVersionData


class ContentAssetResponse(BaseModel):
    id: UUID
    kind: ContentAssetKind
    latest_version: ContentAssetVersionResponse


class ContentAssetPage(BaseModel):
    items: list[ContentAssetResponse]
    total: int
    limit: int
    offset: int


class ContentAssetVersionPage(BaseModel):
    items: list[ContentAssetVersionResponse]
    total: int
    limit: int
    offset: int
