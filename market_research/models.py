from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def domain_of(value: str) -> str:
    parsed = urlsplit(value if "://" in value else "https://" + value)
    domain = (parsed.hostname or "").lower().removeprefix("www.")
    if not domain or "." not in domain or parsed.username or parsed.password:
        raise ValueError("Enter a company domain such as example.com")
    return domain


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResearchRequest(StrictModel):
    company: str = Field(min_length=1, max_length=150)
    domain: str
    scope: str = Field(default="", max_length=1000)
    geography: str = Field(default="Global", max_length=150)
    segment: str = Field(default="All business sizes", max_length=150)
    industry: str = "saas"
    news_days: int = Field(default=90, ge=1, le=365)
    max_queries: int = Field(default=40, ge=5, le=120)
    max_model_calls: int = Field(default=35, ge=5, le=100)
    max_seconds: int = Field(default=600, ge=30, le=3600)
    demo: bool = True
    demo_scenario: Literal["normal", "conflict", "ambiguous"] = "normal"

    @field_validator("company")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Company name is required")
        return value.strip()

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        return domain_of(value)


class Evidence(StrictModel):
    id: str
    url: str
    title: str
    publisher: str
    text: str
    retrieved_at: str
    published_at: str | None
    kind: Literal["web", "news"]
    content_type: Literal["snippet", "highlights", "full_page"]
    category: str
    official: bool


class Candidate(StrictModel):
    name: str
    domain: str
    relationship: Literal["direct", "adjacent"]
    reason: str
    product_overlap: float = Field(ge=0, le=1)
    customer_overlap: float = Field(ge=0, le=1)
    geography_overlap: float = Field(ge=0, le=1)
    evidence_ids: list[str]

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        return domain_of(value)

    @property
    def rank_score(self) -> float:
        return round(
            0.5 * self.product_overlap
            + 0.35 * self.customer_overlap
            + 0.15 * self.geography_overlap,
            3,
        )


class Discovery(StrictModel):
    target_summary: str
    target_evidence_ids: list[str]
    candidates: list[Candidate]
    ambiguity: str | None


class ResearchPlan(StrictModel):
    objective: str
    discovery_queries: list[str]
    comparison_focus: list[str]


class SelectionAssessment(StrictModel):
    domain: str
    supported: bool
    directness: float = Field(ge=0, le=1)
    explanation: str


class DiscoveryReview(StrictModel):
    target_supported: bool
    target_explanation: str
    assessments: list[SelectionAssessment]


class SearchTask(StrictModel):
    query: str
    category: Literal["pricing", "features", "positioning", "news"]
    official_only: bool


class CollectionPlan(StrictModel):
    tasks: list[SearchTask]


class Claim(StrictModel):
    id: str
    category: Literal["pricing", "features", "positioning", "news"]
    label: str
    statement: str
    evidence_ids: list[str]
    # Optional details are explicit nulls in structured outputs.
    currency: str | None
    amount: float | None
    billing_period: str | None
    unit: str | None
    region: str | None
    event_date: str | None
    publication_date: str | None
    interpretation: bool


class Extraction(StrictModel):
    claims: list[Claim]
    gaps: list[str]


class ClaimReview(StrictModel):
    claim_id: str
    supported: bool
    directness: float = Field(ge=0, le=1)
    explanation: str
    conflict: str | None


class Review(StrictModel):
    assessments: list[ClaimReview]
    followup_queries: list[str]


class Insight(StrictModel):
    kind: Literal["summary", "opportunity", "threat", "recommendation"]
    text: str
    claim_refs: list[str]


class Briefing(StrictModel):
    insights: list[Insight]
