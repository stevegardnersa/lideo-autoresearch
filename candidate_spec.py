from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, get_type_hints, Literal, Optional, Tuple, Union

CANDIDATES_JSON_PATH = Path(os.environ.get("CANDIDATES_JSON", "data/candidates.json"))


def _load_json(path: Path) -> Dict[str, Any]:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "profiles": {}}


# ──────────────────────────────────────────────────────────────────────────────
# Literal types (statically defined for type-checker compatibility)
# ──────────────────────────────────────────────────────────────────────────────

Profile = Literal[
    "30m",
    "60m",
    "30m_minimax_notthinking",
    "60m_deepseek_notthinking",
    "30m_dv4flash_thinking",
    "30m_dv4flash_notthinking",
    "30m_dv4pro_thinking",
    "30m_dv4pro_notthinking",
    "60m_dv4flash_thinking",
    "60m_dv4flash_notthinking",
    "60m_dv4pro_thinking",
    "60m_dv4pro_notthinking",
    "30m_mimo25pro_thinking",
    "30m_mimo25pro_notthinking",
    "30m_mimoflash_thinking",
    "30m_mimoflash_notthinking",
    "60m_mimo25pro_thinking",
    "60m_mimo25pro_notthinking",
    "60m_mimoflash_thinking",
    "60m_mimoflash_notthinking",
    "30m_gpt5mini_mimo25pro_thinking",
    "30m_gpt5mini_mimo25pro_notthinking",
    "30m_gpt5mini_mimoflash_thinking",
    "30m_gpt5mini_mimoflash_notthinking",
    "30m_gpt5mini_dv4flash_thinking",
    "30m_gpt5mini_dv4flash_notthinking",
    "30m_gpt5mini_dv4pro_thinking",
    "30m_gpt5mini_dv4pro_notthinking",
    "30m_deepseek-v4-flash_thinking",
    "30m_deepseek-v4-flash_notthinking",
]
FormatMode = Literal["markdown_sections", "markdown_bullets", "prose"]
ContextMode = Literal[
    "chapter_only",
    "chapter_plus_toc",
    "chapter_plus_book_meta",
    "chapter_plus_toc_and_meta",
]
RepairStrategy = Literal["edit_existing", "regenerate_from_source"]
ComposerMode = Literal["summaries_only", "hybrid_retrieve", "source_aware"]


# ──────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class StageConfig:
    model: str
    temperature: float = 0.2
    seed: Optional[int] = 42
    max_tokens: int = 8192
    format_mode: FormatMode = "markdown_sections"
    context_mode: ContextMode = "chapter_plus_toc_and_meta"
    prompt_components: Dict[str, str] = field(default_factory=dict)
    provider_order: Tuple[str, ...] = ()
    allow_fallbacks: bool = False
    use_json_schema: Optional[bool] = None
    extra_body: Optional[Dict[str, Any]] = None


@dataclass
class LengthControlConfig:
    max_passes: int = 5
    tolerance_pct: float = 0.05
    hard_tolerance_pct: float = 0.10
    repair_strategy: RepairStrategy = "edit_existing"
    repair_more_prompt_id: str = "expand_missing_detail"
    repair_less_prompt_id: str = "shrink_dedup_first"


@dataclass
class BudgetAllocatorConfig:
    words_per_minute: int = 200
    allocation_alpha: float = 0.90
    min_chapter_share: float = 0.03
    max_chapter_share: float = 0.18
    chapter_stage_multiplier_30m: float = 1.20
    chapter_stage_multiplier_60m: float = 1.00
    max_summary_to_source_ratio: float = 0.90


@dataclass
class ScoringGatesOverride:
    min_faithfulness: Optional[float] = None
    min_concept_coverage: Optional[float] = None
    max_final_length_error_pct: Optional[float] = None
    max_passes: Optional[int] = None


@dataclass
class CandidateSpec:
    name: str
    profile: Profile
    chapter_stage: StageConfig
    composer_stage: StageConfig
    composer_mode: ComposerMode = "summaries_only"
    length_control: LengthControlConfig = field(default_factory=LengthControlConfig)
    budget_allocator: BudgetAllocatorConfig = field(default_factory=BudgetAllocatorConfig)
    use_json_schema: bool = True
    json_schema_name: str = "summary_response"
    notes: str = ""
    scoring_gates_override: Optional[ScoringGatesOverride] = None
    disable_composer: bool = False

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


SUMMARY_JSON_SCHEMA: Dict[str, object] = {
    "name": "summary_response",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "summary_md": {
                "type": "string",
                "description": "The requested summary in markdown.",
            },
            "estimated_visible_words": {
                "type": "integer",
                "description": "Estimated word count of the summary (visible words).",
            },
            "json_schema": {
                "type": "object",
                "description": "The JSON schema used for the summary.",
            },
        },
        "required": ["summary_md", "estimated_visible_words"],
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _dict_to_dataclass(data: Dict[str, Any], cls: type) -> Any:
    if data is None:
        return None
    fields = {}
    # Resolve field types (handles PEP 563 postponed evaluation via get_type_hints)
    try:
        field_types = get_type_hints(cls)
    except Exception:
        field_types = {k: v.type for k, v in cls.__dataclass_fields__.items()}
    for k, v in data.items():
        if k not in field_types:
            continue
        field_type = field_types[k]
        origin = getattr(field_type, "__origin__", None)
        if origin is Union:
            args = [a for a in getattr(field_type, "__args__", []) if a is not type(None)]
            if args:
                underlying = args[0]
                if hasattr(underlying, "__dataclass_fields__"):
                    fields[k] = _dict_to_dataclass(v, underlying)
                else:
                    fields[k] = v
                continue
        if hasattr(field_type, "__dataclass_fields__"):
            fields[k] = _dict_to_dataclass(v, field_type)
        elif isinstance(v, dict):
            fields[k] = v
        else:
            fields[k] = v
    return cls(**fields)


# ──────────────────────────────────────────────────────────────────────────────
# Profile loader
# ──────────────────────────────────────────────────────────────────────────────

def _build_profile_map() -> Dict[Profile, CandidateSpec]:
    data = _load_json(CANDIDATES_JSON_PATH)
    profiles_data = data.get("profiles", {})
    result: Dict[Profile, CandidateSpec] = {}
    for name, spec_dict in profiles_data.items():
        try:
            cs = _dict_to_dataclass(spec_dict, CandidateSpec)
            result[name] = cs
        except Exception as e:
            raise ValueError(f"Failed to deserialize profile {name!r}") from e
    return result


_PROFILES: Optional[Dict[Profile, CandidateSpec]] = None


def _get_profiles() -> Dict[Profile, CandidateSpec]:
    global _PROFILES
    if _PROFILES is None:
        _PROFILES = _build_profile_map()
    return _PROFILES


def get_candidate(profile: Profile) -> CandidateSpec:
    candidates = _get_profiles()
    if profile not in candidates:
        raise KeyError(f"Unknown profile: {profile}")
    return candidates[profile]


# Expose PROFILE_CANDIDATES as a module-level constant (lazy-loaded once)
def _build_and_assign() -> Dict[Profile, CandidateSpec]:
    return _build_profile_map()

PROFILE_CANDIDATES: Dict[Profile, CandidateSpec] = _build_and_assign()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers used by run_candidate.py
# ──────────────────────────────────────────────────────────────────────────────

def stage_to_request(stage: StageConfig, json_schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    request = {
        "model": stage.model,
        "temperature": stage.temperature,
        "max_tokens": stage.max_tokens,
    }
    if stage.seed is not None:
        request["seed"] = stage.seed
    if stage.extra_body:
        request["extra_body"] = stage.extra_body
    return request
