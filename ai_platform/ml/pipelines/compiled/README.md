# Generated KFP artifact

`PENDING_REGENERATION`: the checked-in pipeline artifact was removed because its
source contract changed. Generate it only after the deployment lockfiles and the
explicit runtime image have been verified:

```bash
uv run --project ai_platform/ml/pipelines \
  python -m ai_platform.ml.pipelines.compile_pipeline \
  --runtime-image "$F9_RUNTIME_IMAGE" \
  --output ai_platform/ml/pipelines/compiled/f9_canonical_pipeline.yaml
```

Do not edit the generated YAML by hand.
