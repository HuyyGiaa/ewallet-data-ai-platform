"""Route fraud predictions between two KServe model versions.

KServe Standard mode has no canary split, so the Fraud API chooses the backend. The
unit is ``user_id``: one user stays on one model, so replayed or repeated
transactions cannot flip between versions.

Attribution comes from the backend, not the plan. ``KServeV2Client`` rejects a
response whose ``model_version`` differs from the one expected for that arm, so
``served_arm`` and ``model_version`` record what actually answered. A failing
candidate falls back to the control and is counted. A failing control raises:
candidate output is never used to cover for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Protocol
from urllib.parse import urlsplit

from ai_platform.inference.ab_assignment import AssignmentError, TrafficSplit, WeightedArm
from ai_platform.inference.assembler import ModelInputVector
from ai_platform.inference.errors import ConfigurationError, InferenceError
from ai_platform.inference.schemas import ExperimentAttribution, ModelPrediction

ROUTING_UNIT = "user_id"


class ArmPredictor(Protocol):
    """The part of ``KServeV2Client`` an arm needs."""

    async def predict(
        self, vector: ModelInputVector, *, correlation_id: str
    ) -> ModelPrediction: ...

    async def ready(self) -> bool: ...


class ABTelemetry(Protocol):
    """Record per-arm outcomes; label values come from the experiment config."""

    def record(
        self,
        attribution: ExperimentAttribution,
        *,
        latency_seconds: float,
        prediction: ModelPrediction | None,
    ) -> None: ...

    def record_candidate_failure(self, arm: str, code: str) -> None: ...

    def record_availability(self, arm: str, available: bool) -> None: ...


class NoopABTelemetry:
    """Discard experiment telemetry."""

    def record(self, attribution, *, latency_seconds, prediction) -> None:
        """Ignore one prediction."""

    def record_candidate_failure(self, arm, code) -> None:
        """Ignore one candidate failure."""

    def record_availability(self, arm, available) -> None:
        """Ignore one readiness result."""


@dataclass(frozen=True)
class ArmConfig:
    """One deployed model version behind one KServe endpoint."""

    name: str
    weight: int
    base_url: str
    model_version: str
    timeout_seconds: float


@dataclass(frozen=True)
class ABExperimentConfig:
    """A validated model experiment; the first arm is the control."""

    experiment_id: str
    salt: str
    model_name: str
    arms: tuple[ArmConfig, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ABExperimentConfig":
        """Validate a decoded configuration document."""
        if raw.get("unit") != ROUTING_UNIT:
            raise ConfigurationError("experiment unit must be user_id")
        try:
            arms = tuple(
                ArmConfig(
                    name=str(arm["name"]),
                    weight=int(arm["weight"]),
                    base_url=str(arm["base_url"]).rstrip("/"),
                    model_version=str(arm["model_version"]),
                    timeout_seconds=float(arm.get("timeout_seconds", 2.0)),
                )
                for arm in raw["arms"]
            )
            config = cls(str(raw["experiment_id"]), str(raw["salt"]), str(raw["model_name"]), arms)
            config.split()
        except (KeyError, TypeError, ValueError, AssignmentError) as exc:
            raise ConfigurationError("experiment configuration is invalid") from exc
        versions = [arm.model_version for arm in arms]
        if len(set(versions)) != len(versions):
            raise ConfigurationError("arms must serve different model versions")
        for arm in arms:
            if urlsplit(arm.base_url).scheme not in {"http", "https"} or arm.timeout_seconds <= 0:
                raise ConfigurationError("arm endpoint or timeout is invalid")
        return config

    @classmethod
    def from_file(cls, path: Path) -> "ABExperimentConfig":
        """Load the mounted configuration; a broken file stops the service."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError("experiment configuration is unreadable") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("experiment configuration must be an object")
        return cls.from_mapping(raw)

    def split(self) -> TrafficSplit:
        """Return the traffic split of this experiment."""
        return TrafficSplit(
            self.experiment_id,
            self.salt,
            tuple(WeightedArm(arm.name, arm.weight) for arm in self.arms),
        )


@dataclass(frozen=True)
class RoutedPrediction:
    """A prediction and the record of how it was produced."""

    prediction: ModelPrediction
    attribution: ExperimentAttribution


class ABRouter:
    """Choose an arm per user, call it, and attribute what really answered."""

    def __init__(
        self,
        split: TrafficSplit,
        predictors: Mapping[str, ArmPredictor],
        *,
        telemetry: ABTelemetry | None = None,
    ) -> None:
        if set(predictors) != {arm.name for arm in split.arms}:
            raise ConfigurationError("every arm needs exactly one predictor")
        self._split = split
        self._predictors = dict(predictors)
        self._telemetry = telemetry or NoopABTelemetry()

    def _attribution(
        self, assigned: str, served: str, prediction: ModelPrediction | None, *, fallback: bool
    ) -> ExperimentAttribution:
        return ExperimentAttribution(
            experiment_id=self._split.experiment_id,
            assigned_arm=assigned,
            served_arm=served,
            model_version=prediction.model_version if prediction else "none",
            fallback=fallback,
        )

    async def _timed(
        self, arm: str, vector: ModelInputVector, correlation_id: str
    ) -> tuple[ModelPrediction, float]:
        started = time.perf_counter()
        prediction = await self._predictors[arm].predict(vector, correlation_id=correlation_id)
        return prediction, time.perf_counter() - started

    async def predict(
        self, vector: ModelInputVector, *, correlation_id: str, routing_key: str
    ) -> RoutedPrediction:
        """Serve one prediction from the arm assigned to ``routing_key``."""
        assigned = self._split.assign(routing_key).name
        control = self._split.control.name
        served, fallback = assigned, False
        started = time.perf_counter()
        try:
            prediction, latency = await self._timed(served, vector, correlation_id)
        except InferenceError as error:
            if assigned == control:
                self._telemetry.record(
                    self._attribution(assigned, control, None, fallback=False),
                    latency_seconds=time.perf_counter() - started,
                    prediction=None,
                )
                raise
            self._telemetry.record_candidate_failure(assigned, error.code)
            served, fallback = control, True
            try:
                prediction, latency = await self._timed(control, vector, correlation_id)
            except InferenceError:
                self._telemetry.record(
                    self._attribution(assigned, control, None, fallback=True),
                    latency_seconds=time.perf_counter() - started,
                    prediction=None,
                )
                raise
        attribution = self._attribution(assigned, served, prediction, fallback=fallback)
        self._telemetry.record(attribution, latency_seconds=latency, prediction=prediction)
        return RoutedPrediction(prediction, attribution)

    async def ready(self) -> bool:
        """Require the control; report candidates without failing readiness."""
        control = self._split.control.name
        await self._predictors[control].ready()
        self._telemetry.record_availability(control, True)
        for name, predictor in self._predictors.items():
            if name == control:
                continue
            try:
                await predictor.ready()
                self._telemetry.record_availability(name, True)
            except InferenceError:
                self._telemetry.record_availability(name, False)
        return True
