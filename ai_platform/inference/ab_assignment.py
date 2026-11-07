"""Deterministic traffic assignment shared by the model and agent experiments.

An assignment depends only on the experiment, its salt and the unit key, so it is
stable across pods, restarts and replays. A new salt gives a new, independent
split. This module has no dependencies and holds no routing logic.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

BUCKETS = 10_000


class AssignmentError(ValueError):
    """A traffic split is invalid."""


@dataclass(frozen=True)
class WeightedArm:
    """One arm and its share of traffic in whole percent."""

    name: str
    weight: int


@dataclass(frozen=True)
class TrafficSplit:
    """A validated split; the first arm is the control."""

    experiment_id: str
    salt: str
    arms: tuple[WeightedArm, ...]

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.salt:
            raise AssignmentError("experiment_id and salt are required")
        if len(self.arms) < 2:
            raise AssignmentError("a split needs at least two arms")
        names = [arm.name for arm in self.arms]
        if len(set(names)) != len(names) or not all(names):
            raise AssignmentError("arm names must be unique and non-empty")
        if any(arm.weight < 0 for arm in self.arms):
            raise AssignmentError("arm weights must not be negative")
        if sum(arm.weight for arm in self.arms) != 100:
            raise AssignmentError("arm weights must total 100")

    @property
    def control(self) -> WeightedArm:
        """Return the arm that serves when a candidate cannot."""
        return self.arms[0]

    def bucket(self, unit_key: str) -> int:
        """Map a unit key to a stable bucket in ``[0, BUCKETS)``."""
        digest = hashlib.sha256(
            f"{self.experiment_id}\0{self.salt}\0{unit_key}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:8], "big") % BUCKETS

    def assign(self, unit_key: str) -> WeightedArm:
        """Return the arm for a unit key."""
        if not unit_key:
            raise AssignmentError("unit key must not be empty")
        bucket = self.bucket(unit_key)
        ceiling = 0
        for arm in self.arms:
            ceiling += arm.weight * (BUCKETS // 100)
            if bucket < ceiling:
                return arm
        raise AssignmentError("weights do not cover every bucket")
