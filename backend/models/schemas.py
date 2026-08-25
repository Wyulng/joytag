"""API contracts and compliance constants.

This module intentionally has no imports from ``services`` so it can be used by
the database and transparency layers without creating an import cycle.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# Versioned public contracts. Bump these when the disclosed behavior changes.
DISCLOSURE_VERSION = "2026-08-25"
TRANSPARENCY_VERSION = "2026-08-25"

DSAR_REQUEST_TYPES = ("access", "erasure", "objection")
DSAR_CONTACT_MIN_LENGTH = 3
DSAR_CONTACT_MAX_LENGTH = 500
DSAR_NOTE_MAX_LENGTH = 2000
DSAR_RATE_LIMIT = "5/hour"
DSAR_RESPONSE_DEADLINE_DAYS = 30

DEFAULT_RETENTION_DAYS = {
    "llm_trace": 90,
    "lineage": 396,
    "audit": 396,
}

EU_COUNTRY_CODES = ("DE", "FR", "NL", "UK", "IT", "ES")


class PendingReviewItem(BaseModel):
    id: str
    word: str
    country: str
    assessment_reason: str
    source: str
    category: str | None = None
    cn_original: str | None = None
    created_at: str


class PendingListResponse(BaseModel):
    count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    items: list[PendingReviewItem]
    next_offset: int | str | None = None


class ApproveRequest(BaseModel):
    category: str | None = Field(default=None, max_length=100)
    anchor_cn_id: str | None = Field(default=None, max_length=100)


class ApproveResponse(BaseModel):
    success: bool
    message: str
    tag_id: str
    updated_rules: dict[str, Any]


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("拒绝理由不能为空")
        return value


class RejectResponse(BaseModel):
    success: bool
    message: str
    reason: str
    updated_rules: dict[str, Any]


class RecommendRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    target_country: str
    category: str | None = Field(default=None, max_length=100)
    top_k: int = Field(default=5, ge=1, le=10)

    @field_validator("title", "target_country", "category", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("target_country")
    @classmethod
    def validate_country(cls, value: str) -> str:
        value = value.upper()
        if value not in EU_COUNTRY_CODES:
            raise ValueError(f"不支持的国家代码: {value}")
        return value


class RecommendItem(BaseModel):
    word: str
    reason: str
    similarity: float | None = None
    source: str | None = None
    compliance_reason: str | None = None
    anchor_cn_word: str | None = None
    trend_score: float = 0.0
    ai_generated: bool = True


class RecommendResponse(BaseModel):
    recommendations: list[RecommendItem]
    total_candidates: int = Field(ge=0)
    filtered_candidates: int = Field(ge=0)
    ai_assisted: bool = False
    parameters_version: str | None = None
    disclosure_url: str | None = None


class LocalTagItem(BaseModel):
    id: str
    word: str
    country: str
    category: str | None = None
    compliance_status: str
    reason: str
    source: str
    trend_score: float = 0.0
    anchor_cn_id: str | None = None
    anchor_cn_word: str | None = None
    created_at: str


class LocalTagListResponse(BaseModel):
    count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    items: list[LocalTagItem]
    next_offset: int | str | None = None


class AnchorItem(BaseModel):
    id: str
    cn_word: str
    category: str | None = None
    created_at: str
    linked_tags_count: int = Field(default=0, ge=0)


class AnchorListResponse(BaseModel):
    count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    items: list[AnchorItem]
    next_offset: int | str | None = None


class UpdateScheduleRequest(BaseModel):
    enabled: bool | None = None
    cron: str | None = Field(default=None, min_length=9, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("cron", "name")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else value


class DisclosureParameter(BaseModel):
    key: str
    description: str
    relative_importance: str
    values: dict[str, Any] | None = None


class DisclosureParameters(BaseModel):
    version: str
    last_updated: str
    system_name: str
    description: str
    input_signals: list[DisclosureParameter]
    ranking_parameters: list[DisclosureParameter]
    compliance_filters: list[DisclosureParameter]
    data_sources: list[DisclosureParameter]
    ai_involvement: list[DisclosureParameter]
    user_controls: list[DisclosureParameter]


class DsarCreateRequest(BaseModel):
    request_type: Literal["access", "erasure", "objection"]
    contact: str = Field(
        min_length=DSAR_CONTACT_MIN_LENGTH,
        max_length=DSAR_CONTACT_MAX_LENGTH,
    )
    subject_note: str = Field(default="", max_length=DSAR_NOTE_MAX_LENGTH)

    @field_validator("contact")
    @classmethod
    def validate_contact(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("联系方式不能为空")
        return value

    @field_validator("subject_note")
    @classmethod
    def strip_subject_note(cls, value: str) -> str:
        return value.strip()


class DsarCreateResponse(BaseModel):
    ticket_id: str
    status: str
    message: str


class DsarSearchRequest(BaseModel):
    term: str = Field(min_length=1, max_length=500)
    country: str | None = None

    @field_validator("term")
    @classmethod
    def validate_term(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("检索词不能为空")
        return value

    @field_validator("country")
    @classmethod
    def strip_country(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else value


class TransparencyResponse(BaseModel):
    version: str
    last_updated: str
    system_name: str
    disclosure_url: str
    dsar_submission: dict[str, Any]
    sections: list[dict[str, Any]]
