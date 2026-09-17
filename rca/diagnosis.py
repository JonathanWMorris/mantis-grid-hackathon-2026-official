from typing import Literal
from pydantic import BaseModel, Field
from .schema import REASONS

Reason = Literal[*REASONS]


class Diagnosis(BaseModel):
    component: str
    reason: Reason
    onset: int
    evidence_ids: list[str] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]


class LogicSupport(BaseModel):
    execution_id: str
    binding_indices: list[int] = Field(
        min_length=1, description="Zero-based indices into returned bindings"
    )
    explanation: str = Field(min_length=10)
    assumptions: list[str]


class SupportedDiagnosis(Diagnosis):
    prolog_support: list[LogicSupport] = Field(min_length=1)
