"""Production deployment identity: the approved principal, proven by the cluster itself.

A production release must satisfy two different conditions before any mutation:

A. approved identity: the principal the operator names equals the one approved in the Jenkins handoff
   (``production_identity``), and both are exact Kubernetes usernames, for example
   ``system:serviceaccount:ewallet-ci:ewallet-cicd-agent-release`` or a Google account e-mail;
B. authenticated principal: the explicit production context actually authenticates as that principal.

A kubeconfig context name or a typed identity string proves neither. B is established with the
read-only ``SelfSubjectReview`` API (``authentication.k8s.io/v1``, GA since Kubernetes 1.28; the
``v1beta1`` endpoint of 1.27 is tried only when ``v1`` is not served). Every authenticated user may
create a SelfSubjectReview through the default ``system:basic-user`` role, so no permission is added.

Two independent views are checked, because the release uses two clients:

* the ``kubectl --context <ctx> proxy`` client (locks, SandboxAgent units) - ``api_principal``;
* a fresh ``kubectl --context <ctx> auth whoami -o json`` (kubectl >= 1.28, the controller image pins
  1.37), which re-reads the kubeconfig the way every ``helm --kube-context <ctx>`` call does -
  ``kubectl_principal``. A context edited or switched after preflight is detected at the next
  checkpoint.

There is no fallback: no in-cluster Jenkins token, no current-context, no default credentials. Any
failure to obtain or parse the principal refuses the release before mutation, and stops it (without an
automatic rollback, since the credentials are no longer trusted) at a later checkpoint.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable, Sequence

from scripts.ci.release_lock import OwnershipUncertain

SERVICE_ACCOUNT = re.compile(r"^system:serviceaccount:[a-z0-9]([-a-z0-9]*[a-z0-9])?:[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
USER = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
Runner = Callable[[Sequence[str]], str]


class IdentityError(OwnershipUncertain):
    """The deployment identity is missing, not approved, or not the authenticated principal."""

    state = "IDENTITY_UNVERIFIED"


def valid_principal(value: str | None) -> bool:
    return bool(value) and bool(SERVICE_ACCOUNT.fullmatch(value) or USER.fullmatch(value))  # type: ignore[arg-type]


def require_approved(approved: str | None, operator: str | None) -> str:
    """Condition A (no cluster access)."""

    if not valid_principal(approved):
        raise IdentityError(f"IDENTITY_NOT_APPROVED: the handoff approves no exact principal ({approved!r})")
    if not operator:
        raise IdentityError("IDENTITY_MISSING: the operator must name the production identity")
    if operator != approved:
        raise IdentityError(f"IDENTITY_MISMATCH: the operator names {operator}, the handoff approved {approved}")
    return approved


def _username(review: Any, source: str) -> str:
    user = ((review or {}).get("status") or {}).get("userInfo") if isinstance(review, dict) else None
    name = user.get("username") if isinstance(user, dict) else None
    if not isinstance(name, str) or not name:
        raise IdentityError(f"IDENTITY_MALFORMED: {source} returned no status.userInfo.username")
    return name


def api_principal(client: Any) -> str:
    """The principal of the proxy client (SelfSubjectReview, v1 then v1beta1)."""

    from scripts.ci.k8s_api import KubeError

    body = {"kind": "SelfSubjectReview", "metadata": {}}
    for version in ("v1", "v1beta1"):
        try:
            review = client.request("POST", f"apis/authentication.k8s.io/{version}/selfsubjectreviews",
                                    {"apiVersion": f"authentication.k8s.io/{version}", **body})
        except KubeError as error:
            if version == "v1" and "HTTP 404" in str(error):
                continue  # 1.27 serves only v1beta1
            raise IdentityError(f"IDENTITY_UNAVAILABLE: SelfSubjectReview failed: {error}") from error
        return _username(review, f"SelfSubjectReview {version}")
    raise IdentityError("IDENTITY_UNAVAILABLE: the cluster serves no SelfSubjectReview API")


def kubectl_principal(context: str, run: Runner | None = None) -> str:
    """The principal the kubeconfig context resolves to now (as helm --kube-context will use it)."""

    if not context or context == "in-cluster":
        raise IdentityError("IDENTITY_UNAVAILABLE: an explicit production context is required")

    def default(command: Sequence[str]) -> str:
        result = subprocess.run(list(command), capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise IdentityError(f"IDENTITY_UNAVAILABLE: kubectl auth whoami failed with exit {result.returncode}")
        return result.stdout

    try:
        output = (run or default)(["kubectl", "--context", context, "auth", "whoami", "-o", "json"])
        return _username(json.loads(output), "kubectl auth whoami")
    except IdentityError:
        raise
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise IdentityError(f"IDENTITY_UNAVAILABLE: kubectl auth whoami: {type(error).__name__}") from error


def verify(approved: str, context: str, client: Any, run: Runner | None = None) -> str:
    """Condition B: both clients authenticate as exactly the approved principal."""

    seen = {"proxy": api_principal(client), "kubeconfig": kubectl_principal(context, run)}
    wrong = {source: name for source, name in seen.items() if name != approved}
    if wrong:
        raise IdentityError(f"IDENTITY_MISMATCH: the production context authenticates as {wrong}, approved {approved}")
    return approved
