"""The dedicated identity the streaming campaign runs as (runbook prerequisite P6).

The frozen Block A operator (``scripts/streaming/block_a_cloud.sh``) runs ``kubectl --context``
against ``ml-platform`` and ``feature-store``. The Jenkins ServiceAccount must not hold those
rights, and ``streaming_verify.py`` refuses the bare ``in-cluster`` context. Two ways to supply the
identity, both explicit:

* ``STREAMING_KUBECONFIG`` + ``STREAMING_KUBE_CONTEXT``: a kubeconfig file (for example a Jenkins
  file credential, or the operator workstation's kubeconfig) and the context to use;
* in-cluster on the Jenkins controller: Jenkins requests a one-hour token for the ServiceAccount
  ``ewallet-ci/ewallet-cicd-streaming`` (TokenRequest; Jenkins may mint tokens for that one account
  only; the reviewed RBAC proposal is listed in the CI/CD rehearsal runbook, P6) and writes a private
  kubeconfig whose only context is ``ewallet-cicd-streaming``.

Without either, the streaming unit is BLOCKED; nothing falls back to the Jenkins identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Callable, Mapping

from scripts.ci.k8s_api import SA_DIR, Client, KubeError, service_account_token

CONTEXT = "ewallet-cicd-streaming"
NAMESPACE = "ewallet-ci"
CONTEXT_NAME = re.compile(r"^[A-Za-z0-9._@:-]{1,253}$")


class StreamingIdentityError(RuntimeError):
    """No approved streaming identity is available."""


@dataclass(frozen=True)
class StreamingIdentity:
    kubeconfig: Path
    context: str
    source: str

    def environment(self) -> dict[str, str]:
        return {"KUBECONFIG": str(self.kubeconfig), "KUBE_CONTEXT": self.context}


def kubeconfig_text(server: str, ca_file: Path, token: str, context: str = CONTEXT) -> str:
    return (
        "apiVersion: v1\nkind: Config\n"
        f"clusters:\n- name: {context}\n  cluster:\n    server: {server}\n    certificate-authority: {ca_file}\n"
        f"users:\n- name: {context}\n  user:\n    token: {token}\n"
        f"contexts:\n- name: {context}\n  context:\n    cluster: {context}\n    user: {context}\n"
        f"current-context: {context}\n"
    )


def resolve(workdir: Path, environment: Mapping[str, str] | None = None,
            client_factory: Callable[[], Client] | None = None, seconds: int = 3600) -> StreamingIdentity:
    env = os.environ if environment is None else environment
    supplied = env.get("STREAMING_KUBECONFIG", "").strip()
    if supplied:
        context = env.get("STREAMING_KUBE_CONTEXT", "").strip()
        if not Path(supplied).is_file():
            raise StreamingIdentityError("STREAMING_KUBECONFIG does not name a file")
        if not CONTEXT_NAME.fullmatch(context) or context == "in-cluster":
            raise StreamingIdentityError("STREAMING_KUBE_CONTEXT must name a context of STREAMING_KUBECONFIG")
        return StreamingIdentity(Path(supplied), context, "supplied-kubeconfig")
    account = env.get("STREAMING_SERVICE_ACCOUNT", "").strip()
    if not account:
        raise StreamingIdentityError("BLOCKED: no streaming identity (set STREAMING_KUBECONFIG/STREAMING_KUBE_CONTEXT, "
                                     "or STREAMING_SERVICE_ACCOUNT on the controller; runbook P6)")
    host = env.get("KUBERNETES_SERVICE_HOST", "")
    if not host:
        raise StreamingIdentityError("STREAMING_SERVICE_ACCOUNT needs the in-cluster API (run on the controller)")
    client = client_factory() if client_factory else Client.from_environment(None)
    try:
        token = service_account_token(client, NAMESPACE, account, seconds)
    except KubeError as error:
        raise StreamingIdentityError(f"cannot obtain a token for {NAMESPACE}/{account}: {error}") from error
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "streaming-kubeconfig"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(kubeconfig_text(f"https://{host}:{env.get('KUBERNETES_SERVICE_PORT', '443')}",
                                     SA_DIR / "ca.crt", token))
    return StreamingIdentity(path, CONTEXT, f"token-request:{NAMESPACE}/{account}")
