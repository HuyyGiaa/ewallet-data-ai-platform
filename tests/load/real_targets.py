"""Request builders and response contracts for real-backend load tests.

Dependency-free so it can be unit tested without Locust. A response counts as a success
only when it honours the target's contract: an expected error is still a failure here.
Invalid-request and fault scenarios stay in the separate mock-backed harness.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import random
from typing import Any, Callable, Iterator, Mapping
from uuid import uuid4

FEATURE = "feature"
INFERENCE = "inference"
TARGETS = (FEATURE, INFERENCE)
ENTITY_FIELDS = ("user_id", "account_id", "device_id", "merchant_id", "event_timestamp")
# Small, stable projection: the four features the accepted business request reads.
DEFAULT_FEATURES = (
    "account_behavior__account_tx_count_1h",
    "account_behavior__account_tx_count_24h",
    "user_behavior__user_avg_amount_30d",
    "merchant_behavior__merchant_tx_count_24h",
)
USER_ONLY_FEATURES = DEFAULT_FEATURES[:3]


class LoadConfigurationError(ValueError):
    """A load run is unsafe or malformed."""


@dataclass(frozen=True)
class Target:
    """One selectable load target."""

    name: str
    path: str
    build: Callable[[Mapping[str, Any]], dict[str, Any]]
    check: Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, str]], str | None]


def feature_request(entity: Mapping[str, Any]) -> dict[str, Any]:
    """Build a lookup; merchant features apply only to entities with a merchant."""
    body = {name: entity.get(name) for name in ENTITY_FIELDS}
    body["feature_names"] = list(DEFAULT_FEATURES if entity.get("merchant_id") else USER_ONLY_FEATURES)
    return body


def inference_request(entity: Mapping[str, Any]) -> dict[str, Any]:
    """Build a prediction with a new transaction id, so no two requests are identical."""
    return {**entity, "transaction_id": f"{entity['transaction_id']}-{uuid4().hex[:12]}"}


def check_feature(sent: Mapping[str, Any], body: Mapping[str, Any], _: Mapping[str, str]) -> str | None:
    """Return a failure reason, or None when the lookup honours its contract."""
    features = body.get("features")
    if not isinstance(features, dict) or set(features) != set(sent["feature_names"]):
        return "features differ from the requested names"
    if not body.get("oldest_feature_timestamp") or not body.get("correlation_id"):
        return "response lacks its timestamp or correlation id"
    return None


def check_inference(sent: Mapping[str, Any], body: Mapping[str, Any], options: Mapping[str, str]) -> str | None:
    """Return a failure reason, or None when the prediction honours its contract."""
    if body.get("transaction_id") != sent["transaction_id"]:
        return "response is for another transaction"
    probability = body.get("fraud_probability")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not 0 <= probability <= 1:
        return "fraud_probability is out of range"
    if body.get("fraud_prediction") not in (0, 1):
        return "fraud_prediction is not 0 or 1"
    expected = options.get("model_version")
    if expected and str(body.get("model_version")) != expected:
        return f"model_version is not {expected}"
    return None


REGISTRY = {
    FEATURE: Target(FEATURE, "/v1/features/lookup", feature_request, check_feature),
    INFERENCE: Target(INFERENCE, "/v1/fraud/predict", inference_request, check_inference),
}


def entity_order(count: int, seed: int) -> Iterator[int]:
    """Yield fixture indexes forever: one seeded permutation, repeated.

    Every entity is sent equally often, and a rerun with the same seed sends the same sequence.
    """
    if count < 1:
        raise LoadConfigurationError("the entity fixture is empty")
    order = list(range(count))
    random.Random(seed).shuffle(order)
    return itertools.cycle(order)


def select_target(name: str) -> Target:
    """Return a target by name."""
    if name not in REGISTRY:
        raise LoadConfigurationError(f"target must be one of {TARGETS}")
    return REGISTRY[name]


def validate_host(host: str, acknowledgement: str | None) -> None:
    """Refuse a non-loopback target unless the owner acknowledged it."""
    from urllib.parse import urlsplit

    parsed = urlsplit(host)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LoadConfigurationError(f"invalid host {host!r}")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} and acknowledgement != "I_OWN_THIS_TARGET":
        raise LoadConfigurationError("a non-loopback host needs EWALLET_LOAD_ACKNOWLEDGE_REMOTE=I_OWN_THIS_TARGET")
