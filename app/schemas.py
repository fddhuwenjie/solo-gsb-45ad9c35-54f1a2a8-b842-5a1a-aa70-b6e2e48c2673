"""请求 / 响应的 Pydantic 模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 段级状态：
#   missing   —— 尚无任何译文
#   submitted —— 存在依据当前原文版本、待确认的译文
#   confirmed —— 存在依据当前原文版本的已确认译文
#   stale     —— 最新译文依据的是旧原文版本（已确认译文失效，待复核）
SEGMENT_MISSING = "missing"
SEGMENT_SUBMITTED = "submitted"
SEGMENT_CONFIRMED = "confirmed"
SEGMENT_STALE = "stale"


class CreateManuscript(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    segments: list[str] = Field(min_length=1)


class RewriteSource(BaseModel):
    text: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)


class SubmitTranslation(BaseModel):
    text: str = Field(min_length=1)
    source_version: int = Field(ge=1)


class ConfirmTranslation(BaseModel):
    source_version: int = Field(ge=1)


class PublishRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class TranslationView(BaseModel):
    source_version: int
    target_text: str
    status: str
    updated_at: str


class SegmentView(BaseModel):
    position: int
    source_text: str
    source_version: int
    status: str
    translation: TranslationView | None = None
    # 失效待复核的旧译文（若有），随工作稿一并展示供复核参考
    stale_translation: TranslationView | None = None


class DraftView(BaseModel):
    manuscript_id: int
    title: str
    revision: int
    segments: list[SegmentView]


class PublicationSegment(BaseModel):
    position: int
    source_version: int
    source_text: str
    target_text: str


class PublicationView(BaseModel):
    edition: int
    revision: int
    title: str
    published_at: str
    segments: list[PublicationSegment]
