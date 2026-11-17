# Bounded EDAI-2 Jenkins runtime

This manifest deploys one internal Jenkins controller for the runtime proof. It has no Ingress, Docker socket, privileged container, or production deployment credential. Its Kubernetes API access is a projected, one-hour ServiceAccount token bound only to the namespace-scoped Roles in `rbac/batch-b-isolated.yaml` (rehearsal namespace `ewallet-cicd`, Prompt Registry, release-lock Leases). The jobs read an immutable Git bundle from a digest-pinned init image and reject a checkout whose full SHA does not match the reviewed job parameter.

`jenkins.yaml` is a template. Render it with `python3 -m scripts.ci.render_jenkins_runtime` and digest-qualified controller/source images. Create `Secret/jenkins-admin` separately; never place its values in Git or evidence. CI proof jobs keep `DEPLOY_APPROVED=false`.

The source snapshot is a staging-only substitute for a Git webhook because the approved branch is local and must not be pushed. It proves Jenkins execution and change selection, not remote webhook delivery.
