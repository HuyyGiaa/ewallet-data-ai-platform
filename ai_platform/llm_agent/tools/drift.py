"""drift_detection: explainable comparison of a current window with a versioned baseline.

No drift policy is approved in source yet, so nothing here invents one: the
method, bins, smoothing, minimum sample size and thresholds all come from the
versioned baseline. Without thresholds the result is ``unclassified``. The
production baseline registry and window sampler are explicitly unavailable.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints, field_validator, model_validator

from ai_platform.llm_agent.contracts import REVISION_RE, RequestContext
from ai_platform.llm_agent.tools.runtime import (
    ToolContractError,
    ToolOutcome,
    ToolRejectedError,
    ToolUnavailableError,
)
from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS

MONITORED_FEATURES = frozenset((*HISTORICAL_FEATURE_COLUMNS, "amount"))
# A resource bound on how much data one call may scan, not a drift policy.
MAX_WINDOW = timedelta(days=31)
MAX_BINS = 50


class DriftWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _ordered_and_bounded(self) -> "DriftWindow":
        if self.end <= self.start:
            raise ValueError("window end must be after start")
        if self.end - self.start > MAX_WINDOW:
            raise ValueError("window must not exceed 31 days")
        return self


class DriftDetectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feature_name: str
    baseline_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    current_window: DriftWindow

    @field_validator("feature_name")
    @classmethod
    def _monitored(cls, value: str) -> str:
        if value not in MONITORED_FEATURES:
            raise ValueError("feature_name is not a monitored model input")
        return value

    @field_validator("baseline_id")
    @classmethod
    def _revision(cls, value: str) -> str:
        if not REVISION_RE.fullmatch(value) or value in {"latest", "current", "active"}:
            raise ValueError("baseline_id must be an immutable revision identifier")
        return value


def _finite(values: tuple[float, ...], name: str) -> None:
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError(f"{name} must contain finite numbers")


@dataclass(frozen=True)
class DriftBaseline:
    """One approved reference distribution; bins are (-inf, c1], (c1, c2], ..., (ck, inf)."""

    baseline_id: str
    feature_name: str
    method: Literal["psi"]
    cut_points: tuple[float, ...]
    reference_proportions: tuple[float, ...]
    smoothing: float
    min_current_count: int
    reference_start: datetime
    reference_end: datetime
    dataset_revision: str
    thresholds: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if self.method != "psi":
            raise ValueError("only the psi method is implemented")
        if not REVISION_RE.fullmatch(self.baseline_id) or not REVISION_RE.fullmatch(self.dataset_revision):
            raise ValueError("baseline and dataset revisions must be immutable identifiers")
        if self.feature_name not in MONITORED_FEATURES:
            raise ValueError("baseline feature is not monitored")
        _finite(self.cut_points, "cut_points")
        _finite(self.reference_proportions, "reference_proportions")
        if not 1 <= len(self.cut_points) < MAX_BINS:
            raise ValueError(f"baseline needs 2..{MAX_BINS} bins")
        if any(left >= right for left, right in zip(self.cut_points, self.cut_points[1:])):
            raise ValueError("cut_points must be strictly increasing")
        if len(self.reference_proportions) != len(self.cut_points) + 1:
            raise ValueError("reference_proportions must have one value per bin")
        if any(value < 0 for value in self.reference_proportions) or not math.isclose(
            sum(self.reference_proportions), 1.0, abs_tol=1e-9
        ):
            raise ValueError("reference_proportions must be non-negative and sum to 1")
        if not 0 < self.smoothing <= 0.01:
            raise ValueError("smoothing must be in (0, 0.01]")
        if isinstance(self.min_current_count, bool) or self.min_current_count < 1:
            raise ValueError("min_current_count must be positive")
        for value in (self.reference_start, self.reference_end):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("reference window must be timezone-aware")
        if self.reference_end <= self.reference_start:
            raise ValueError("reference window end must be after start")
        if self.thresholds is not None:
            _finite(self.thresholds, "thresholds")
            warning, drift = self.thresholds
            if not 0 < warning < drift:
                raise ValueError("thresholds must satisfy 0 < warning < drift")


@dataclass(frozen=True)
class WindowSample:
    feature_name: str
    start: datetime
    end: datetime
    values: tuple[float, ...]
    dataset_revision: str


class BaselineStore(Protocol):
    async def get(self, baseline_id: str, feature_name: str) -> DriftBaseline | None: ...

    async def ready(self) -> None: ...


class WindowSampler(Protocol):
    async def sample(self, feature_name: str, start: datetime, end: datetime) -> WindowSample: ...

    async def ready(self) -> None: ...


class UnavailableBaselineStore:
    _REASON = "No approved drift baseline registry is configured"

    async def get(self, baseline_id: str, feature_name: str) -> DriftBaseline | None:
        raise ToolUnavailableError("drift_baseline_unavailable", self._REASON)

    async def ready(self) -> None:
        raise ToolUnavailableError("drift_baseline_unavailable", self._REASON)


class UnavailableWindowSampler:
    _REASON = "No current-window feature statistics source is configured"

    async def sample(self, feature_name: str, start: datetime, end: datetime) -> WindowSample:
        raise ToolUnavailableError("drift_window_unavailable", self._REASON)

    async def ready(self) -> None:
        raise ToolUnavailableError("drift_window_unavailable", self._REASON)


def population_stability_index(
    baseline: DriftBaseline, values: tuple[float, ...],
) -> tuple[float, list[dict[str, float | None]]]:
    """Return PSI and per-bin contributions; proportions are floored at ``smoothing``."""

    counts = [0] * len(baseline.reference_proportions)
    for value in values:
        counts[bisect_left(baseline.cut_points, value)] += 1
    total = len(values)
    bins: list[dict[str, float | None]] = []
    psi = 0.0
    for position, (count, reference) in enumerate(zip(counts, baseline.reference_proportions)):
        current = count / total
        p = max(reference, baseline.smoothing)
        q = max(current, baseline.smoothing)
        contribution = (q - p) * math.log(q / p)
        psi += contribution
        bins.append({
            "upper_bound": baseline.cut_points[position] if position < len(baseline.cut_points) else None,
            "reference_proportion": reference,
            "current_proportion": current,
            "contribution": contribution,
        })
    return psi, bins


class DriftDetectionTool:
    name = "drift_detection"
    description = (
        "Compare one monitored feature's current window with a versioned baseline and return "
        "an explainable PSI breakdown. Classification uses only the baseline's own thresholds."
    )
    input_model = DriftDetectionInput

    def __init__(self, baselines: BaselineStore, sampler: WindowSampler) -> None:
        self._baselines = baselines
        self._sampler = sampler

    async def run(self, arguments: DriftDetectionInput, context: RequestContext) -> ToolOutcome:
        baseline = await self._baselines.get(arguments.baseline_id, arguments.feature_name)
        if baseline is None:
            raise ToolRejectedError("unknown_baseline", "No baseline with this id exists for the feature")
        if (baseline.baseline_id, baseline.feature_name) != (arguments.baseline_id, arguments.feature_name):
            raise ToolContractError("baseline_identity_mismatch", "Baseline store returned another baseline")
        window = arguments.current_window
        if window.start < baseline.reference_end and baseline.reference_start < window.end:
            raise ToolRejectedError("window_overlaps_reference", "Current window overlaps the baseline reference window")
        sample = await self._sampler.sample(arguments.feature_name, window.start, window.end)
        if (sample.feature_name, sample.start, sample.end) != (arguments.feature_name, window.start, window.end):
            raise ToolContractError("window_identity_mismatch", "Sampler returned another feature or window")
        if not REVISION_RE.fullmatch(sample.dataset_revision):
            raise ToolContractError("window_identity_mismatch", "Sampler returned no immutable dataset revision")
        try:
            _finite(sample.values, "values")
        except ValueError as exc:
            raise ToolContractError("non_finite_feature_value", "Current window contains non-finite values") from exc

        result: dict[str, object] = {
            "feature_name": arguments.feature_name,
            "baseline_id": baseline.baseline_id,
            "method": baseline.method,
            "reference_window": {"start": baseline.reference_start.isoformat(), "end": baseline.reference_end.isoformat()},
            "current_window": {"start": window.start.isoformat(), "end": window.end.isoformat()},
            "current_count": len(sample.values),
            "min_current_count": baseline.min_current_count,
            "dataset_revisions": {"reference": baseline.dataset_revision, "current": sample.dataset_revision},
            "thresholds": (
                {"warning": baseline.thresholds[0], "drift": baseline.thresholds[1]}
                if baseline.thresholds is not None else None
            ),
            "live_metrics_verified": False,
        }
        if len(sample.values) < baseline.min_current_count:
            return ToolOutcome(result={**result, "drift_status": "insufficient_data", "psi": None, "bins": []})
        psi, bins = population_stability_index(baseline, sample.values)
        if baseline.thresholds is None:
            status = "unclassified"
        elif psi >= baseline.thresholds[1]:
            status = "drift"
        elif psi >= baseline.thresholds[0]:
            status = "warning"
        else:
            status = "no_drift"
        return ToolOutcome(result={**result, "drift_status": status, "psi": psi, "bins": bins})

    async def ready(self) -> None:
        await self._baselines.ready()
        await self._sampler.ready()
