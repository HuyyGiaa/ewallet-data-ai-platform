# F9 MLflow Experiment Tracking and Model Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Conda-reproducible, multi-model fraud experiment that tracks nested MLflow runs, selects the winner using validation data, packages its validated threshold, and supports guided manual registration followed by verified automatic registration.

**Architecture:** Keep model computation independent of MLflow. A shared experiment engine trains the fixed four-model matrix and returns typed results; a tracking adapter persists parent/child runs to a local MLflow Server backed by PostgreSQL and MinIO, then optionally registers only the winner. The operator runs infrastructure and UI steps from an explicit runbook while the automated suite covers computation, tracking contracts, failures, and parity without requiring persistent services.

**Tech Stack:** Python 3.10, Conda, Pandas 2.3.3, PyArrow 25.0.1, scikit-learn 1.7.2, PyYAML 6.0.3, MLflow 3.17.1, PostgreSQL/psycopg2-binary 2.9.13, MinIO/S3/boto3 1.43.114, unittest.

**Spec:** `docs/superpowers/specs/2026-10-16-f9-mlflow-experiment-registry-design.md`

## Global Constraints

- Preserve the committed F8 single-Random-Forest command and its measured behavior.
- Use the bounded F7 population; do not run or materialize the four-million-row training population.
- Keep the exact 41-feature leakage boundary and chronological train/validation/test partitions.
- Run exactly four F9 children: Dummy control, Logistic Regression, Random Forest, and Histogram Gradient Boosting.
- Exclude Dummy from winner selection; rank eligible models by validation PR-AUC, F1, recall, then configured order.
- Score test data only for the validation winner.
- Use Conda environment `ewallet-ml`; do not make `.venv` the active runtime.
- Pin direct packages to the exact versions in the approved spec.
- Keep MLflow model, client, and server on version 3.17.1.
- Use the existing PostgreSQL and MinIO services with dedicated `mlflow` database/user and `mlflow-artifacts` bucket.
- Keep every credential in runtime environment variables or ignored local files.
- Bind the initial MLflow Server to `127.0.0.1:5000` and do not add it to Compose in F9.
- Support `registry.mode=manual` and `registry.mode=automatic`; never move `champion` automatically.
- Do not modify or stage `data_platform/generation/config/settings.yaml` or `data_platform/ingestion/kafka/create_topic.sh`.
- Do not implement FastAPI, Feast Online, Dockerized MLflow, Kubernetes, Kubeflow, distributed training, drift detection, or incremental data-version storage.
- After Task 1, automated commands use `conda run -n ewallet-ml env PYTHONPATH=. python ...`; operator-facing commands may use `conda activate ewallet-ml` followed by the shorter `python ...` form.

## Review Focus

- A boolean or nonnumeric estimator hyperparameter must not pass as a numeric value; Task 2 adds estimator-specific config tests.
- Missing, duplicate, reordered, or unexpected candidates must fail before training; Task 2 pins the exact matrix and configured order.
- NaN/non-finite validation metrics and complete metric ties must not produce an unstable winner; Task 4 tests rejection and configured-order resolution.
- A Registry or MinIO failure after successful training must leave the previous `candidate` alias unchanged; Tasks 7 and 9 test the failure boundary.
- Missing, extra, or reordered serving columns must not silently change a prediction; Task 5 tests the exact 41-column model contract.

---

## File map

**Create**

- `ai_platform/ml/environment.yml`: canonical Conda environment.
- `ai_platform/ml/config/f9_mlflow_experiment.yaml`: versioned experiment and tracking configuration.
- `ai_platform/ml/modeling/experiment.py`: typed candidate execution and winner selection.
- `ai_platform/ml/tracking/__init__.py`: tracking package boundary.
- `ai_platform/ml/tracking/config.py`: F9 and MLflow config parser.
- `ai_platform/ml/tracking/model.py`: threshold-aware MLflow PyFunc model.
- `ai_platform/ml/tracking/mlflow_tracker.py`: nested tracking and Registry lifecycle.
- `ai_platform/ml/tracking/run.py`: F9 CLI.
- `ai_platform/ml/tracking/verify.py`: manual Registry-version parity CLI.
- `infra/mlflow/bootstrap_backend.py`: idempotent PostgreSQL role/database and MinIO bucket bootstrap.
- `infra/mlflow/start_server.sh`: validated local Tracking Server launcher.
- `infra/mlflow/check_health.py`: Tracking Server health check.
- `infra/mlflow/.env.example`: variable names and non-secret placeholders.
- `tests/test_fraud_experiment.py`: model-matrix and selection tests.
- `tests/test_mlflow_tracking.py`: config, PyFunc, tracking, Registry, and CLI tests.
- `tests/test_mlflow_infra.py`: bootstrap and launcher contract tests.
- `docs/ml/mlflow_tracking.md`: operator runbook.
- `docs/evidence/f9_mlflow_baseline.json`: measured evidence added only after real acceptance.
- `docs/assets/mlflow/f9_*.png`: real UI evidence added only when the operator captures and reviews it; never fabricate screenshots.

**Modify**

- `ai_platform/ml/requirements.txt`: add exact MLflow/PostgreSQL/S3 dependencies.
- `ai_platform/ml/modeling/config.py`: add reusable candidate and experiment dataclasses without changing `load_config()`.
- `ai_platform/ml/modeling/features.py`: support estimator-specific numeric scaling while preserving the current default.
- `ai_platform/ml/modeling/models.py`: preserve `build_model()` and add the F9 candidate factory.
- `ai_platform/ml/modeling/data.py`: expose a reusable dataset fingerprint and summary.
- `ai_platform/ml/modeling/evaluation.py`: add deterministic eligible-winner selection support.
- `tests/test_fraud_modeling.py`: pin F8 backward compatibility after shared-module changes.
- `.gitignore`: ignore only new local MLflow logs/state and keep `.env.example` trackable.
- `README.md`: summarize F9 commands and status.
- `docs/README.md`: link the MLflow runbook.
- `docs/ml/fraud_modeling.md`: replace active `.venv` commands with Conda commands.

## Task 1: Establish the Conda runtime contract

**Files:**
- Create: `ai_platform/ml/environment.yml`
- Modify: `ai_platform/ml/requirements.txt`
- Test: `tests/test_mlflow_tracking.py`

**Interfaces:**
- Consumes: the existing four direct F8 pins in `ai_platform/ml/requirements.txt`.
- Produces: a reproducible `ewallet-ml` environment with Python 3.10 and all exact F9 direct dependencies.

- [ ] **Step 1: Write the failing environment-manifest test**

Add `MlflowEnvironmentContractTest` assertions that the YAML name is
`ewallet-ml`, Python is `3.10`, pip consumes the sibling requirements file,
and the requirements contain exact pins for MLflow 3.17.1,
psycopg2-binary 2.9.13, and boto3 1.43.114 exactly once.

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `ai_platform/ml/.venv/bin/python -m unittest tests.test_mlflow_tracking.MlflowEnvironmentContractTest -v`

Expected: FAIL because `environment.yml` and the new pins do not exist.

- [ ] **Step 3: Add the minimal manifests**

Create `environment.yml` with `name: ewallet-ml`, `conda-forge`, `python=3.10`,
`pip`, and a pip requirements reference resolved relative to the environment
file. Add the three exact pins without changing existing versions.

- [ ] **Step 4: Parse manifests and confirm GREEN**

Run: `ai_platform/ml/.venv/bin/python -m unittest tests.test_mlflow_tracking.MlflowEnvironmentContractTest -v`

Expected: PASS.

- [ ] **Step 5: Operator creates and validates the environment**

Run under guidance:

```bash
conda env create -f ai_platform/ml/environment.yml
conda activate ewallet-ml
python --version
python -m pip check
mlflow --version
```

Expected: Python 3.10.x, no broken requirements, MLflow 3.17.1.

- [ ] **Step 6: Run F7/F8 regression before retiring `.venv`**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_fraud_training_dataset tests.test_fraud_modeling -v`

Expected: all existing tests PASS. Keep `.venv` until this check passes; its
later deletion is an operator cleanup action and creates no Git diff.

- [ ] **Step 7: Commit the environment contract explicitly**

```bash
git add ai_platform/ml/environment.yml ai_platform/ml/requirements.txt tests/test_mlflow_tracking.py
git commit -m "build: add Conda environment for MLflow"
```

## Task 2: Add the exact F9 configuration contract

**Files:**
- Create: `ai_platform/ml/config/f9_mlflow_experiment.yaml`
- Create: `ai_platform/ml/tracking/__init__.py`
- Create: `ai_platform/ml/tracking/config.py`
- Modify: `ai_platform/ml/modeling/config.py`
- Test: `tests/test_mlflow_tracking.py`

**Interfaces:**
- Consumes: `_resolved_path()` and the existing F8 dataclasses without changing their behavior.
- Produces: `CandidateConfig`, `SelectionConfig`, `ExperimentConfig`, `RegistryConfig`, `MlflowConfig`, `F9Config`, and `load_f9_config(path: Path, repo_root: Path | None = None) -> F9Config`.

- [ ] **Step 1: Write failing valid-config and matrix tests**

Assert the checked-in config resolves the bounded dataset path, seed 42,
primary metric `pr_auc`, tie-breakers `("f1", "recall")`, exactly four names
in approved order, only Dummy ineligible, tracking URI
`http://127.0.0.1:5000`, manual mode, registered name
`ewallet-fraud-classifier`, and alias `candidate`.

- [ ] **Step 2: Write failing invalid-config tests**

Cover duplicate/missing/reordered candidates, unsupported estimator, boolean
numeric parameters, Dummy marked eligible, real candidate marked ineligible,
unknown metric, invalid URI, invalid Registry mode, empty names, and credentials
embedded in tracked YAML.

- [ ] **Step 3: Run config tests and confirm RED**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_tracking.MlflowConfigTest -v`

Expected: import or missing-file failures.

- [ ] **Step 4: Implement the dataclasses and strict parser**

Keep F8 `ModelConfig`, `F8Config`, and `load_config()` intact. Put model-only
dataclasses in `modeling/config.py`; put tracking/Registry dataclasses and
`load_f9_config()` in `tracking/config.py`. Validate each estimator's allowed
parameter names and types rather than applying the F8 all-positive-numeric
rule to Dummy's string strategy.

- [ ] **Step 5: Run F9 config and F8 regression tests**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_tracking.MlflowConfigTest tests.test_fraud_modeling.FraudModelingConfigTest -v`

Expected: PASS.

- [ ] **Step 6: Commit the config boundary**

```bash
git add ai_platform/ml/config/f9_mlflow_experiment.yaml ai_platform/ml/modeling/config.py ai_platform/ml/tracking/__init__.py ai_platform/ml/tracking/config.py tests/test_mlflow_tracking.py
git commit -m "feat: define F9 MLflow experiment config"
```

## Task 3: Restore only the approved candidate builders

**Files:**
- Modify: `ai_platform/ml/modeling/features.py`
- Modify: `ai_platform/ml/modeling/models.py`
- Modify: `tests/test_fraud_modeling.py`
- Test: `tests/test_fraud_experiment.py`

**Interfaces:**
- Consumes: `CandidateConfig`, `ModelConfig`, and `MODEL_FEATURE_COLUMNS`.
- Produces: `build_feature_transformer(*, scale_numeric: bool = False) -> Pipeline` and `build_candidate(candidate: CandidateConfig, seed: int) -> Pipeline`; preserves `build_model(model: ModelConfig, seed: int) -> Pipeline`.

- [ ] **Step 1: Write failing candidate-factory tests**

Assert exact estimator classes, balanced classes for eligible models, seed 42,
approved parameters, Logistic numeric scaling, no tree scaling, and no PCA,
selector, XGBoost, or unsupported estimator branch.

- [ ] **Step 2: Write the F8 backward-compatibility test**

Assert `build_model()` still creates the active Random Forest preprocessing and
estimator with the same feature count and deterministic probabilities.

- [ ] **Step 3: Run focused tests and confirm RED**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_fraud_experiment.CandidateFactoryTest tests.test_fraud_modeling.FraudModelingModelTest -v`

Expected: FAIL because `build_candidate()` and scaling support do not exist.

- [ ] **Step 4: Add optional numeric scaling and candidate construction**

Use `StandardScaler` only after numeric imputation for Logistic Regression.
Build Dummy, Logistic, Random Forest, and HGB with approved defaults. Have
`build_model()` use the unchanged no-scaling Random Forest path.

- [ ] **Step 5: Run candidate and full F8 tests**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_fraud_experiment.CandidateFactoryTest tests.test_fraud_modeling -v`

Expected: PASS with no F8 metric or artifact-contract regression.

- [ ] **Step 6: Commit the candidate factory**

```bash
git add ai_platform/ml/modeling/features.py ai_platform/ml/modeling/models.py tests/test_fraud_experiment.py tests/test_fraud_modeling.py
git commit -m "feat: add tracked fraud model candidates"
```

## Task 4: Implement validation-only experiment selection

**Files:**
- Create: `ai_platform/ml/modeling/experiment.py`
- Modify: `ai_platform/ml/modeling/data.py`
- Modify: `ai_platform/ml/modeling/evaluation.py`
- Modify: `ai_platform/ml/modeling/run.py`
- Test: `tests/test_fraud_experiment.py`

**Interfaces:**
- Consumes: `dict[str, ModelingSplit]`, `ExperimentConfig`, `build_candidate()`, `choose_threshold()`, `evaluate_scores()`, and `scenario_report()`.
- Produces: `CandidateResult`, `ExperimentResult`, `CandidateTrainer.fit_validate(...) -> CandidateResult`, `select_winner(results, selection) -> CandidateResult`, `ExperimentRunner.run(splits, dataset_fingerprint) -> ExperimentResult`, `input_fingerprint(path: Path) -> str`, and `dataset_summary(splits) -> dict[str, dict[str, float | int]]`.

- [ ] **Step 1: Write failing candidate-result tests**

Use deterministic fake estimators to assert every candidate sees identical
train and validation transaction IDs, receives no test frame, records its own
threshold/metrics/timings, and releases no diagnostic column into `X`.

- [ ] **Step 2: Write failing winner-selection tests**

Assert Dummy exclusion; PR-AUC, F1, recall, configured-order tie-breaks;
rejection of no eligible results and non-finite metrics; and independence from
test values.

- [ ] **Step 3: Write the test-isolation test**

Assert `ExperimentRunner` invokes test scoring exactly once with the selected
pipeline and adds scenario diagnostics only to the winner.

- [ ] **Step 4: Run focused tests and confirm RED**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_fraud_experiment.ExperimentRunnerTest -v`

Expected: import failure for `modeling.experiment`.

- [ ] **Step 5: Implement typed results and sequential execution**

Store the fitted pipeline in each `CandidateResult` for later logging, but keep
test results only on `ExperimentResult`. Select a deterministic validation
input example and parity sample without labels or diagnostics. Move reusable
fingerprint/summary helpers out of F8 `run.py` without changing F8 output.

- [ ] **Step 6: Run experiment, F7, and F8 regressions**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_fraud_experiment tests.test_fraud_training_dataset tests.test_fraud_modeling -v`

Expected: PASS.

- [ ] **Step 7: Commit the experiment engine**

```bash
git add ai_platform/ml/modeling/experiment.py ai_platform/ml/modeling/data.py ai_platform/ml/modeling/evaluation.py ai_platform/ml/modeling/run.py tests/test_fraud_experiment.py
git commit -m "feat: add validation-only fraud experiment runner"
```

## Task 5: Package the validated threshold as an MLflow Model

**Files:**
- Create: `ai_platform/ml/tracking/model.py`
- Test: `tests/test_mlflow_tracking.py`

**Interfaces:**
- Consumes: a fitted scikit-learn pipeline, selected threshold, and `MODEL_FEATURE_COLUMNS`.
- Produces: `FraudPyFuncModel(pipeline, threshold, feature_columns)` implementing `predict(context, model_input, params=None) -> pd.DataFrame`, `model_signature(input_example) -> ModelSignature`, `ParityReport`, and `compare_predictions(source_model, target_model, sample) -> ParityReport`.

- [ ] **Step 1: Write failing prediction-contract tests**

Assert the output columns and dtypes, positive-class probability, inclusive
`>= threshold` decision, input immutability, deterministic repeatability,
unknown categorical handling, and exact equality with the local pipeline.

- [ ] **Step 2: Write failing schema-boundary tests**

Assert missing, extra, duplicate, and reordered columns produce clear contract
errors rather than silent reordering; labels and diagnostics are rejected.

- [ ] **Step 3: Run focused tests and confirm RED**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_tracking.FraudPyFuncModelTest -v`

Expected: missing module/class failure.

- [ ] **Step 4: Implement the minimal PyFunc wrapper and signature**

Return only `fraud_probability` as float and `fraud_prediction` as integer.
Build the signature from a validated feature-only example and the actual output
frame; do not infer it from label-bearing data. Compare probabilities with an
explicit tolerance and predictions exactly in the shared parity helper.

- [ ] **Step 5: Run model-package tests**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_tracking.FraudPyFuncModelTest -v`

Expected: PASS.

- [ ] **Step 6: Commit the deployable contract**

```bash
git add ai_platform/ml/tracking/model.py tests/test_mlflow_tracking.py
git commit -m "feat: package threshold-aware fraud model"
```

## Task 6: Track parent and child runs in manual mode

**Files:**
- Create: `ai_platform/ml/tracking/mlflow_tracker.py`
- Test: `tests/test_mlflow_tracking.py`

**Interfaces:**
- Consumes: `F9Config`, `ExperimentResult`, `FraudPyFuncModel`, and a feature-only input example.
- Produces: `TrackingResult(parent_run_id, child_run_ids, winner_model_uri, parity_sample_uri, registered_model_version, registry_status, registry_parity)`, `MlflowTracker.check_health() -> None`, and `MlflowTracker.log_experiment(result) -> TrackingResult`.

- [ ] **Step 1: Write failing parent/child logging tests**

With a fake MLflow facade/client, assert one parent and exactly four nested
children, shared provenance on parent, flattened params and validation metrics
on children, Dummy eligibility tag, winner run linkage, test metrics only on
the winner child, and one feature-only parity sample artifact on that child.

- [ ] **Step 2: Write failing manual-mode tests**

Assert all child model artifacts are logged, winner URI uses the winning child,
parent status is `awaiting_manual_registration`, no Registry API method is
called, and returned `registered_model_version` is `None`.

- [ ] **Step 3: Run tracker tests and confirm RED**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_tracking.MlflowManualTrackingTest -v`

Expected: missing `MlflowTracker` failure.

- [ ] **Step 4: Implement explicit logging without autolog**

Use explicit parameters, metrics, tags, JSON artifacts, dataset lineage, and
`mlflow.pyfunc.log_model()`. Do not enable `mlflow.autolog()`. Keep backend and
artifact credentials outside the client config.

- [ ] **Step 5: Run manual tracking tests**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_tracking.MlflowManualTrackingTest -v`

Expected: PASS.

- [ ] **Step 6: Commit manual tracking**

```bash
git add ai_platform/ml/tracking/mlflow_tracker.py tests/test_mlflow_tracking.py
git commit -m "feat: track nested fraud model runs"
```

## Task 7: Add automatic Registry verification and safe aliasing

**Files:**
- Modify: `ai_platform/ml/tracking/mlflow_tracker.py`
- Test: `tests/test_mlflow_tracking.py`

**Interfaces:**
- Consumes: winner model URI returned by MLflow, deterministic parity sample, Registry config, shared `compare_predictions()`, and `MlflowClient`.
- Produces: automatic immutable version creation, readiness wait, exact-version reload parity, provenance tags, and post-parity `candidate` alias assignment.

- [ ] **Step 1: Write failing automatic registration tests**

Assert only the winner is registered, the expected registered name is used,
readiness is awaited, exact-version URI is reloaded, required tags are applied,
and `candidate` moves only after probability/prediction parity passes.

- [ ] **Step 2: Write failure-boundary tests**

Cover registration exception, terminal failed version, readiness timeout,
download/reload failure, probability mismatch, prediction mismatch, and MinIO
artifact failure. In every case assert the old alias target remains unchanged,
the run receives a diagnostic status/tag, and an exception reaches the CLI.

- [ ] **Step 3: Run automatic tests and confirm RED**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_tracking.MlflowAutomaticRegistryTest -v`

Expected: missing automatic path or alias assertions fail.

- [ ] **Step 4: Implement registration, parity, tags, and alias ordering**

Use immutable version IDs for parity. Compare probabilities with an explicit
floating tolerance and predictions exactly. Never call a `champion` alias API.

- [ ] **Step 5: Run all tracking tests**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_tracking -v`

Expected: PASS.

- [ ] **Step 6: Commit Registry automation**

```bash
git add ai_platform/ml/tracking/mlflow_tracker.py tests/test_mlflow_tracking.py
git commit -m "feat: verify and register winning fraud model"
```

## Task 8: Add safe PostgreSQL and MinIO bootstrap tooling

**Files:**
- Create: `infra/mlflow/bootstrap_backend.py`
- Create: `infra/mlflow/start_server.sh`
- Create: `infra/mlflow/check_health.py`
- Create: `infra/mlflow/.env.example`
- Modify: `.gitignore`
- Test: `tests/test_mlflow_infra.py`

**Interfaces:**
- Consumes: `POSTGRES_ADMIN_DSN`, `MLFLOW_DB_NAME`, `MLFLOW_DB_USER`, `MLFLOW_DB_PASSWORD`, `MLFLOW_BACKEND_STORE_URI`, `MLFLOW_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `MLFLOW_ARTIFACT_BUCKET`.
- Produces: idempotent database/role/bucket bootstrap, localhost server launch, and `check_health(uri: str) -> None`.

- [ ] **Step 1: Write failing environment and identifier tests**

Assert missing variables fail with names only, SQL identifiers accept the
approved local values and reject unsafe characters, and no error or command
renders a password/access key.

- [ ] **Step 2: Write failing idempotency tests with mocked clients**

Assert an existing role/database/bucket is preserved, an absent one is
created, no object or database is deleted, and rerunning produces the same
state.

- [ ] **Step 3: Write failing launcher/health tests**

Assert the shell script binds `127.0.0.1:5000`, uses PostgreSQL backend and
`s3://mlflow-artifacts`, does not contain credentials, does not run schema
migration implicitly, and health-check errors identify URI/status.

- [ ] **Step 4: Run infra tests and confirm RED**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_infra -v`

Expected: missing file/module failures.

- [ ] **Step 5: Implement minimal bootstrap and lifecycle tools**

Use psycopg2 autocommit for `CREATE DATABASE`, safe SQL identifier/literal
composition, boto3 `head_bucket`/`create_bucket`, POSIX shell environment
validation, and standard-library HTTP health checking. Keep `mlflow db upgrade`
as a separate documented operator command.

- [ ] **Step 6: Run infra tests and secret/path scans**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_infra -v`

Run: `rg -n "/home/|postgresql://[^$]|AWS_SECRET_ACCESS_KEY=." infra/mlflow ai_platform/ml/config tests/test_mlflow_infra.py`

Expected: tests PASS; scan contains only variable names/placeholders, no real
secret or developer path.

- [ ] **Step 7: Commit infrastructure tooling**

```bash
git add .gitignore \
  infra/mlflow/bootstrap_backend.py \
  infra/mlflow/start_server.sh \
  infra/mlflow/check_health.py \
  infra/mlflow/.env.example \
  tests/test_mlflow_infra.py
git commit -m "feat: add local MLflow backend bootstrap"
```

## Task 9: Wire the F9 CLI and isolated MLflow integration test

**Files:**
- Create: `ai_platform/ml/tracking/run.py`
- Create: `ai_platform/ml/tracking/verify.py`
- Modify: `ai_platform/ml/tracking/__init__.py`
- Test: `tests/test_mlflow_tracking.py`

**Interfaces:**
- Consumes: `load_f9_config()`, F7 loader, `ExperimentRunner`, and `MlflowTracker`.
- Produces: `run_f9(config: F9Config) -> F9RunResult`, F9 `parse_args() -> argparse.Namespace`, F9 `main() -> int`, and a parity CLI that accepts source model URI, registered model URI, and winner-run parity-sample artifact URI.

- [ ] **Step 1: Write failing preflight and CLI tests**

Assert config and dataset validation precede training, unavailable server fails
before estimator fit, successful manual mode prints parent/winner/model and
parity-sample URIs plus the next action, automatic mode prints registered
version/alias/parity, the parity CLI returns 0 only for equivalent predictions,
and every runtime exception returns 1 without exposing secrets.

- [ ] **Step 2: Write the isolated server integration test**

Start MLflow 3.17.1 on an ephemeral localhost port with a temporary SQLite
database and local artifacts. Use a tiny valid dataset to verify one parent,
four nested runs, manual no-registration, automatic winner registration,
model reload, exact signatures, and `candidate` parity. This test must not
touch real PostgreSQL, MinIO, or port 5000.

- [ ] **Step 3: Run focused tests and confirm RED**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_tracking.MlflowCliTest tests.test_mlflow_tracking.MlflowIsolatedIntegrationTest -v`

Expected: missing CLI/integration behavior.

- [ ] **Step 4: Implement orchestration and output**

Load/validate once, calculate the dataset fingerprint once, run candidates
sequentially, publish once, and avoid collecting extra full-dataset copies.
Print actionable failure phase and next command while preserving traceback in
test-visible exceptions below `main()`.

- [ ] **Step 5: Run F9, F8, and F7 automated suites**

Run: `conda run -n ewallet-ml env PYTHONPATH=. python -m unittest tests.test_mlflow_tracking tests.test_mlflow_infra tests.test_fraud_experiment tests.test_fraud_modeling tests.test_fraud_training_dataset -v`

Expected: PASS.

- [ ] **Step 6: Compile and parse all new sources/configs**

Run: `conda run -n ewallet-ml python -m compileall -q ai_platform/ml/modeling ai_platform/ml/tracking infra/mlflow tests`

Run: `conda run -n ewallet-ml python -c 'import yaml; yaml.safe_load(open("ai_platform/ml/config/f9_mlflow_experiment.yaml")); yaml.safe_load(open("ai_platform/ml/environment.yml"))'`

Expected: exit 0.

- [ ] **Step 7: Commit the F9 command**

```bash
git add ai_platform/ml/tracking/run.py ai_platform/ml/tracking/verify.py ai_platform/ml/tracking/__init__.py tests/test_mlflow_tracking.py
git commit -m "feat: run tracked fraud model comparison"
```

## Task 10: Write the operator runbook before real infrastructure use

**Files:**
- Create: `docs/ml/mlflow_tracking.md`
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/ml/fraud_modeling.md`

**Interfaces:**
- Consumes: exact commands and output contracts from Tasks 1-9.
- Produces: a repository-root runbook covering Conda, services, migration, server logs, UI navigation, manual registration, automatic registration, aliases, rollback, troubleshooting, and evidence capture.

- [ ] **Step 1: Write the runbook as ordered checkpoints**

For every checkpoint include purpose, exact command/UI location, expected
output, PASS condition, common failures, log location, safe stop command, and
screenshot requirement. Explicitly explain Experiments, parent/child runs,
Models, versions, tags, `candidate`, `champion`, and why test metrics do not
select the winner.

- [ ] **Step 2: Update active environment and index references**

Replace active `.venv` commands with `conda activate ewallet-ml`; leave the
historical F8 implementation plan unchanged. Add concise README/index links
without claiming a runtime baseline before it exists.

- [ ] **Step 3: Verify commands, links, and deferred scope**

Run a repository-root script that resolves every relative Markdown link in the
changed documents. Search for stale active `.venv` commands and unsupported
claims that F9, serving, Kubeflow, or champion promotion already passed.

Expected: links PASS; only explicitly historical `.venv` references remain.

- [ ] **Step 4: Run diff checks and commit documentation**

```bash
git diff --check -- README.md docs/README.md docs/ml/fraud_modeling.md docs/ml/mlflow_tracking.md
git add README.md docs/README.md docs/ml/fraud_modeling.md docs/ml/mlflow_tracking.md
git commit -m "docs: add MLflow operator runbook"
```

## Task 11: Perform the guided real manual workflow

**Files:**
- Modify after measurement: `docs/ml/mlflow_tracking.md`
- Create after measurement: `docs/evidence/f9_mlflow_baseline.json`

**Interfaces:**
- Consumes: real `ewallet-ml`, existing safe Compose services, manual F9 config, and the runbook.
- Produces: real parent/child run IDs, winner URI, manual Registry version, tags, candidate alias, parity result, metrics, timings, and screenshot references.

- [ ] **Step 1: Operator captures resource and Git baseline**

Run the runbook's RAM/disk, branch, HEAD, status, Docker service, volume, and
port checks. Confirm the two unrelated working-tree files remain unstaged.

- [ ] **Step 2: Operator starts PostgreSQL and MinIO without destructive commands**

Use the repository's canonical multi-file Compose project name and only the
required profiles/files. Do not use `down -v`, volume deletion, prune, or pull.

- [ ] **Step 3: Operator bootstraps database/user/bucket and runs schema migration**

Run the idempotent bootstrap twice and verify the second run preserves state.
Run `mlflow db upgrade` explicitly with the server-only backend URI.

- [ ] **Step 4: Operator starts MLflow Server and reads its log**

Verify bind address, version, PostgreSQL backend initialization, MinIO artifact
destination, `/health`, and UI at `http://127.0.0.1:5000`.

- [ ] **Step 5: Operator runs manual-mode F9**

Run the documented command. Expected: one parent, four child run IDs, one
winner, one `runs:/` model URI, `awaiting_manual_registration`, and exit 0.

- [ ] **Step 6: Operator completes the guided UI workflow**

Compare validation columns, confirm Dummy ineligible, inspect winner inputs and
outputs, register `ewallet-fraud-classifier`, add required tags, assign
`candidate`, then run the documented parity CLI against the exact registered
version and logged sample. Capture each required coursework screenshot with its
meaning.

- [ ] **Step 7: Record measured manual evidence**

Write actual IDs, versions, winner, metrics, threshold, resource usage, runtime,
parity, storage locations, and screenshot paths. Do not copy expected values
into observed fields.

## Task 12: Verify automatic registration and finalize F9 evidence

**Files:**
- Modify: `ai_platform/ml/config/f9_mlflow_experiment.yaml`
- Modify: `docs/evidence/f9_mlflow_baseline.json`
- Modify: `docs/ml/mlflow_tracking.md`
- Modify: `README.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: the reviewed manual Registry version and a known previous `candidate` alias target.
- Produces: a new automatically registered version, post-parity `candidate` movement, preserved manual version, final evidence, and a documented deferred `champion` action.

- [ ] **Step 1: Change only Registry mode to automatic**

Review the config diff and assert candidate matrix, dataset, seed, and metric
contract are unchanged.

- [ ] **Step 2: Operator runs automatic-mode F9**

Expected: the same four-run structure, automatic winner version, exact-version
reload parity PASS, and `candidate` moved only after parity.

- [ ] **Step 3: Exercise alias-failure safety without corrupting state**

Use mocked automated tests for destructive/failure paths. On the real server,
perform only read-back: confirm the old manual version remains accessible and
no `champion` alias exists unless the operator later assigns it explicitly.

- [ ] **Step 4: Run final verification**

Run all F7-F9 automated suites, Python compilation, YAML/JSON parsing, model
reload parity, documentation links, artifact/secret/path scans, scoped
`git diff --check`, `git status --short`, and `git diff --stat`.

Expected: all checks PASS; generated data, credentials, MLflow state, and the
two unrelated modified files are absent from the F9 staged set.

- [ ] **Step 5: Review and commit only verified F9 files**

Stage explicit F9 source/config/tests/infra/docs/evidence files. Review
`git diff --cached --check`, `--stat`, and the full cached diff before commit.
Use the final message agreed at review time; do not push, tag, start FastAPI,
or assign `champion` automatically.
