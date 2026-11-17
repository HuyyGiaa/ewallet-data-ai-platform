"""The streaming campaign's dedicated identity: explicit, never the Jenkins identity, token kept private."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from scripts.ci.k8s_api import KubeError
from scripts.ci.streaming_identity import CONTEXT, StreamingIdentityError, resolve


class FakeClient:
    def __init__(self, token: str | None = "tok-123", error: str | None = None) -> None:
        self.token, self.error, self.calls = token, error, []

    def request(self, method, path, body=None, content_type="", raw=False):  # type: ignore[no-untyped-def]
        self.calls.append((method, path, body))
        if self.error:
            raise KubeError(self.error)
        return {"status": {"token": self.token}} if self.token else {"status": {}}


def test_nothing_configured_is_blocked(tmp_path) -> None:
    with pytest.raises(StreamingIdentityError, match="BLOCKED"):
        resolve(tmp_path, {})


def test_a_supplied_kubeconfig_needs_a_named_context(tmp_path) -> None:
    config = tmp_path / "kubeconfig"
    config.write_text("apiVersion: v1\n")
    identity = resolve(tmp_path, {"STREAMING_KUBECONFIG": str(config), "STREAMING_KUBE_CONTEXT": "gke_streaming"})
    assert identity.environment() == {"KUBECONFIG": str(config), "KUBE_CONTEXT": "gke_streaming"}
    for context in ("", "in-cluster"):
        with pytest.raises(StreamingIdentityError, match="STREAMING_KUBE_CONTEXT"):
            resolve(tmp_path, {"STREAMING_KUBECONFIG": str(config), "STREAMING_KUBE_CONTEXT": context})


def test_the_controller_mints_a_token_for_the_streaming_account_only(tmp_path) -> None:
    client = FakeClient()
    identity = resolve(tmp_path, {"STREAMING_SERVICE_ACCOUNT": "ewallet-cicd-streaming",
                                  "KUBERNETES_SERVICE_HOST": "10.0.0.1"}, client_factory=lambda: client)
    method, path, body = client.calls[0]
    assert method == "POST" and path == "api/v1/namespaces/ewallet-ci/serviceaccounts/ewallet-cicd-streaming/token"
    assert body["spec"]["expirationSeconds"] == 3600
    assert identity.context == CONTEXT
    text = identity.kubeconfig.read_text()
    assert "server: https://10.0.0.1:443" in text and "token: tok-123" in text
    assert stat.S_IMODE(os.stat(identity.kubeconfig).st_mode) == 0o600


def test_a_refused_token_request_blocks_the_campaign(tmp_path) -> None:
    for client in (FakeClient(error="HTTP 403: forbidden"), FakeClient(token=None)):
        with pytest.raises(StreamingIdentityError, match="cannot obtain a token"):
            resolve(tmp_path, {"STREAMING_SERVICE_ACCOUNT": "ewallet-cicd-streaming",
                               "KUBERNETES_SERVICE_HOST": "10.0.0.1"}, client_factory=lambda: client)
