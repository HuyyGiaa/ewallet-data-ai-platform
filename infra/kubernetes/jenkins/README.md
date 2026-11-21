# Bounded EDAI-2 Jenkins runtime

This manifest deploys one internal Jenkins controller for the runtime proof. It has no Ingress, Docker socket, privileged container, or production deployment credential. Its Kubernetes API access is a projected, one-hour ServiceAccount token bound only to namespace-scoped Roles: `rbac/batch-b-isolated.yaml` (rehearsal namespace `ewallet-cicd`, Prompt Registry, release-lock Leases) and, for the Agent staging release, `rbac/agent-staging-cd.proposed.yaml`. The streaming campaign runs as its own ServiceAccount (`rbac/streaming-cd-identity.proposed.yaml`), never as Jenkins. The jobs read an immutable Git bundle from a digest-pinned init image and reject a checkout whose full SHA does not match the reviewed job parameter.

`jenkins.yaml` is a template. Render it with `python3 -m scripts.ci.render_jenkins_runtime` and digest-qualified controller/source images. Create `Secret/jenkins-admin` separately; never place its values in Git or evidence. CI proof jobs keep `DEPLOY_APPROVED=false`.

The source snapshot replaces a Git webhook on purpose: Jenkins has no route to GitHub, and every release SHA needs a new bundle, snapshot image and controller rollout. It proves Jenkins execution and change selection, not remote webhook delivery.

Architecture, operations and the real release evidence: [CICD_ARCHITECTURE.md](../../../docs/edai2/cicd/CICD_ARCHITECTURE.md), [CICD_OPERATIONS.md](../../../docs/edai2/cicd/CICD_OPERATIONS.md), [CICD_RELEASE_EVIDENCE.md](../../../docs/edai2/cicd/CICD_RELEASE_EVIDENCE.md).
