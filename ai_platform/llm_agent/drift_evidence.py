"""Validated drift evidence shared by the Drift Specialist and the Coordinator.

The Drift Specialist copies the ``drift_detection`` result into this model and
the Coordinator re-validates it, so neither agent can pass on a drift result
whose identity (feature, baseline, window) differs from the request, whose PSI
is not a finite non-negative number, or whose classification disagrees with
the baseline's own thresholds.
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from ai_platform.llm_agent.business_request import G3DriftRequest

DriftStatus = Literal["no_drift", "warning", "drift", "unclassified", "insufficient_data"]


class _Window(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: AwareDatetime
    end: AwareDatetime


class _Revisions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: str = Field(min_length=1, max_length=128)
    current: str = Field(min_length=1, max_length=128)


class _Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warning: float = Field(gt=0, allow_inf_nan=False)
    drift: float = Field(gt=0, allow_inf_nan=False)


class _Bin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    upper_bound: float | None = Field(default=None, allow_inf_nan=False)
    reference_proportion: float = Field(ge=0, le=1, allow_inf_nan=False)
    current_proportion: float = Field(ge=0, le=1, allow_inf_nan=False)
    contribution: float = Field(allow_inf_nan=False)


class _BaselineSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str | None = Field(default=None, max_length=1024)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reference_count: int | None = Field(default=None, ge=1)


class _CurrentSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    excluded_null_count: int = Field(ge=0)


class DriftFinding(BaseModel):
    """Exact shape of one ``drift_detection`` result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feature_name: str
    baseline_id: str
    method: Literal["psi"]
    reference_window: _Window
    current_window: _Window
    current_count: int = Field(ge=0)
    min_current_count: int = Field(ge=1)
    dataset_revisions: _Revisions
    thresholds: _Thresholds | None
    live_metrics_verified: bool
    drift_status: DriftStatus
    psi: float | None = Field(default=None, allow_inf_nan=False)
    bins: list[_Bin] = Field(default_factory=list, max_length=50)
    baseline_source: _BaselineSource | None = None
    current_source: _CurrentSource | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "DriftFinding":
        if self.drift_status == "insufficient_data":
            if self.psi is not None or self.bins or self.current_count >= self.min_current_count:
                raise ValueError("insufficient_data must carry no PSI and too few observations")
            return self
        if self.psi is None or self.psi < 0 or not self.bins or self.current_count < self.min_current_count:
            raise ValueError("a computed drift result needs a non-negative PSI, bins and enough observations")
        if not math.isclose(sum(item.contribution for item in self.bins), self.psi,
                            rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("PSI must equal the sum of its bin contributions")
        if self.thresholds is None:
            expected = "unclassified"
        elif self.thresholds.warning >= self.thresholds.drift:
            raise ValueError("thresholds must satisfy warning < drift")
        elif self.psi >= self.thresholds.drift:
            expected = "drift"
        elif self.psi >= self.thresholds.warning:
            expected = "warning"
        else:
            expected = "no_drift"
        if self.drift_status != expected:
            raise ValueError("drift_status disagrees with the baseline thresholds")
        return self

    def matches(self, request: G3DriftRequest) -> bool:
        """The result answers exactly the requested feature, baseline and window."""

        def same(left: datetime, right: datetime) -> bool:
            return left == right

        return (
            self.feature_name == request.feature_name
            and self.baseline_id == request.baseline_id
            and same(self.current_window.start, request.current_window.start)
            and same(self.current_window.end, request.current_window.end)
        )
