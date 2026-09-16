from dataclasses import dataclass


@dataclass(frozen=True)
class IndustryProfile:
    name: str
    description: str
    feature_taxonomy: tuple[str, ...]
    pricing_rules: str
    discovery_terms: str


# Add a profile here; orchestration, evidence, tracing and UI remain reusable.
PROFILES = {
    "saas": IndustryProfile(
        name="Software / SaaS",
        description="Business software competing for the same customer jobs and purchasing budget.",
        feature_taxonomy=(
            "Workflow",
            "Collaboration",
            "Automation",
            "Integrations",
            "Analytics",
            "Security",
            "Administration",
            "AI capabilities",
        ),
        pricing_rules="Preserve plan, currency, per-seat/usage unit, billing cadence, annual "
        "commitment, minimum seats, region and taxes when stated. Contact sales "
        "is valid. Do not compare annual per-month prices with monthly prices "
        "without explicitly identifying the billing commitment.",
        discovery_terms="direct competitors alternatives business software",
    ),
}


def get_profile(key: str) -> IndustryProfile:
    if key not in PROFILES:
        raise ValueError(f"Unknown industry: {key}. Register it in industries.PROFILES.")
    return PROFILES[key]
