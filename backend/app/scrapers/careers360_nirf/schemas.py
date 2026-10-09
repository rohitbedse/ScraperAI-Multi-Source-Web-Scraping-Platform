"""Pydantic models and validated loaders for the scraper's input files and output records."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.scrapers.careers360_nirf import config

PARAM_TYPES = {"str", "int", "float", "bool", "list"}


class DataFileError(Exception):
    """A data file is missing, empty or malformed. The message says what to fix."""


# ======================================================================= input: seed
class Seed(BaseModel):
    """One NIRF row, preserved exactly as given (ties in `rank` are legal)."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    city: str = Field(min_length=1)
    state: str = Field(min_length=1)
    location: str = Field(min_length=1)
    score: float
    rank: int = Field(ge=1)


# ================================================================ input: parameters
class Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    type: str = Field(min_length=1)
    scope: Optional[str] = None
    allowed_values: Optional[list[str]] = None
    section_hint: Optional[str] = None
    path: Optional[str] = None   # optional dotted path into the overview page state (no code change needed)

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in PARAM_TYPES:
            raise ValueError(f"type must be one of {sorted(PARAM_TYPES)}, got {v!r}")
        return v


class CourseParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    degree_levels: list[str] = Field(min_length=1)
    fields: list[Parameter] = Field(min_length=1)

    @field_validator("degree_levels")
    @classmethod
    def _unique_levels(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("degree_levels must be unique")
        return v


class ParametersFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college_parameters: list[Parameter] = Field(min_length=1)
    course_parameters: CourseParameters
    study_destination: list[Parameter] = Field(default_factory=list)
    ranking_category: list[Parameter] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_keys(self) -> "ParametersFile":
        seen: set[str] = set()
        for p in self.all_parameters():
            if p.key in seen:
                raise ValueError(f"duplicate parameter key {p.key!r}")
            seen.add(p.key)
        return self

    def all_parameters(self) -> list[Parameter]:
        return [*self.college_parameters, *self.course_parameters.fields,
                *self.study_destination, *self.ranking_category]

    @property
    def degree_levels(self) -> list[str]:
        return self.course_parameters.degree_levels

    @property
    def course_fields(self) -> list[Parameter]:
        return self.course_parameters.fields


# ============================================================================ output
class Candidate(BaseModel):
    url: str
    score: float


class MatchInfo(BaseModel):
    status: Literal["matched", "ambiguous", "unmatched"]
    url: Optional[str] = None
    score: Optional[float] = None
    reason: str = ""
    candidates: list[Candidate] = Field(default_factory=list)
    state_verified: Optional[bool] = None
    city_match: Optional[bool] = None


class Course(BaseModel):
    course_id: Optional[int] = None
    url: Optional[str] = None
    degree: Optional[str] = None                                  # Careers360 degree label, e.g. "B.E /B.Tech"
    fields: dict[str, Any] = Field(default_factory=dict)          # keyed by parameters.yaml course field keys
    raw: dict[str, Any] = Field(default_factory=dict)             # original text/values before normalization
    normalized: dict[str, Any] = Field(default_factory=dict)      # structured forms (months, amount, currency)
    missing_fields: list[str] = Field(default_factory=list)


class CollegeRecord(BaseModel):
    seed: Seed
    match: MatchInfo
    status: Literal["scraped", "partial", "failed", "unmatched", "ambiguous"]
    college: dict[str, Any] = Field(default_factory=dict)
    courses: dict[str, list[Course]] = Field(default_factory=dict)
    rankings: dict[str, Any] = Field(default_factory=dict)
    study_destination: dict[str, Optional[bool]] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    duplicates_skipped: int = 0
    courses_skipped_other_level: int = 0
    course_details_skipped: int = 0
    pages_visited: list[str] = Field(default_factory=list)
    scraped_at: str


# ================================================================================ loaders
def _read_text(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise DataFileError(f"{path}: file not found")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise DataFileError(f"{path}: file is empty")
    return text


def _fmt_errors(exc: ValidationError) -> str:
    return "\n".join(f"  {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())


def load_seed(path: Path = config.SEED_FILE) -> list[Seed]:
    text = _read_text(path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataFileError(f"{path}: malformed JSON ({exc})") from exc
    if not isinstance(data, list) or not data:
        raise DataFileError(f"{path}: expected a non-empty JSON list")
    seeds, errors, seen = [], [], set()
    for i, row in enumerate(data):
        try:
            s = Seed.model_validate(row)
        except ValidationError as exc:
            errors.append(f"  entry #{i + 1}: " + "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
            continue
        if s.id in seen:
            errors.append(f"  entry #{i + 1}: duplicate id {s.id!r}")
        seen.add(s.id)
        seeds.append(s)
    if errors:
        raise DataFileError(f"{path}: invalid entries\n" + "\n".join(errors))
    return seeds   # ranks are not checked for uniqueness: ties (27, 64) are kept as given


def load_parameters(path: Path = config.PARAMETERS_FILE) -> ParametersFile:
    text = _read_text(path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DataFileError(f"{path}: malformed YAML ({exc})") from exc
    if not isinstance(data, dict) or not data:
        raise DataFileError(f"{path}: expected a mapping with college_parameters, course_parameters, "
                            f"study_destination, ranking_category")
    try:
        return ParametersFile.model_validate(data)
    except ValidationError as exc:
        raise DataFileError(f"{path}: invalid content\n{_fmt_errors(exc)}") from exc
