# KFP UI NetworkPolicy: move the N10-E2 hotfix under Helm ownership

## Background

The release `llm-agent` renders `kubeflow/allow-ewallet-agents-otel-to-kfp`
(templates/observability-ml-network.yaml). That policy is the only Ingress
policy selecting `app=ml-pipeline`, so it blocks the KFP UI's call to the API.

N10-E2 restored the UI with the owner-approved object
`kubeflow/allow-kfp-ui-to-ml-pipeline`, applied outside Helm. Its manifest is
`kfp-ui-policy-hotfix-n10e2.yaml`, sha256 `b9da9f35…`.

The chart now renders the same object (same name and spec) when
`networkPolicy.kfpUiIngress.enabled=true`. It is rendered only together with
the restrictive policy, that is when `observabilityStack.mlPlatform.enabled`
is true.

## Why adoption must come first

Helm refuses to create an object that already exists without its release
metadata ("invalid ownership metadata"). Enabling the switch before adoption
therefore fails the upgrade and leaves the live policy untouched.

## Procedure (owner-approved change window only)

```bash
NS=kubeflow; NAME=allow-kfp-ui-to-ml-pipeline
kubectl -n $NS get networkpolicy $NAME -o yaml > before.yaml          # evidence
kubectl -n $NS annotate networkpolicy $NAME \
  meta.helm.sh/release-name=llm-agent meta.helm.sh/release-namespace=default
kubectl -n $NS label networkpolicy $NAME app.kubernetes.io/managed-by=Helm
helm -n default upgrade llm-agent infra/helm/llm-agent-platform \
  -f <live values of the current revision> \
  -f infra/helm/llm-agent-platform/n11/values-n11-kfp-ui-policy.yaml \
  --dry-run=server                                                    # review
helm -n default upgrade llm-agent infra/helm/llm-agent-platform \
  -f <live values of the current revision> \
  -f infra/helm/llm-agent-platform/n11/values-n11-kfp-ui-policy.yaml --wait
```

The adoption metadata changes only annotations and labels; the policy spec,
and therefore traffic, is unchanged throughout.

## Acceptance

- `helm -n default get manifest llm-agent` contains `allow-kfp-ui-to-ml-pipeline`.
- The live spec equals `kfp-ui-policy-hotfix-n10e2.yaml`.
- `kubectl -n kubeflow get networkpolicy` lists the same 3 policies as before.
- The KFP UI shows run `dc29f5cc-cc9e-49ae-b861-eacddd6269b5`. No new pipeline is run.

## Rollback

- Before the upgrade: remove the two annotations and the label.
- After the upgrade: `helm rollback llm-agent <previous revision>`. Helm then
  deletes the adopted policy and the KFP UI breaks again. To keep the UI
  working, re-apply `kfp-ui-policy-hotfix-n10e2.yaml` immediately.
