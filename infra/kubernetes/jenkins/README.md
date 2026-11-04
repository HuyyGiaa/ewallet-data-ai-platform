# Bounded EDAI-2 Jenkins runtime

This manifest deploys one internal Jenkins controller for the runtime proof. It has no Ingress, Kubernetes API token, Docker socket, privileged container, or deployment credential. The jobs read an immutable Git bundle from a digest-pinned init image and reject a checkout whose full SHA does not match the reviewed job parameter.

`jenkins.yaml` is a template. Render it with `python3 -m scripts.ci.render_jenkins_runtime` and digest-qualified controller/source images. Create `Secret/jenkins-admin` separately; never place its values in Git or evidence. CI proof jobs keep `DEPLOY_APPROVED=false`.

The source snapshot is a staging-only substitute for a Git webhook because the approved branch is local and must not be pushed. It proves Jenkins execution and change selection, not remote webhook delivery.
