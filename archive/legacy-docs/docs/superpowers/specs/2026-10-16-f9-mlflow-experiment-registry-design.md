# F9 MLflow Experiment Tracking and Model Registry Design

## Status and intent

F9 adds experiment tracking, model packaging, and model versioning to the
bounded fraud-model workflow established by F7 and F8. The outcome is a local,
observable MLOps workflow in which one command trains a fixed comparison set,
records every candidate in MLflow, selects a winner using validation data,
evaluates only that winner on test data, and makes the winner available for
manual or automatic registration.

The project owner operates the infrastructure and UI while following a
step-by-step runbook. The implementation supplies reproducible commands,
preflight checks, deterministic behavior, tests, and actionable error output.
This division ensures that the operator learns how to start services, inspect
logs, compare runs, register models, apply tags and aliases, and diagnose
failures rather than receiving only a finished UI state.

F9 does not implement an inference API, Feast Online Store, Docker packaging
for MLflow, Kubernetes, Kubeflow, distributed training, drift detection,
MLflow Projects, automated champion promotion, or full four-million-row
training. Those systems will consume the model lifecycle established here.

## Existing inputs and preserved behavior

F9 reads the ignored F7 runtime dataset at
`ai_platform/ml/data/fraud_training_v1/trainable_dataset/`. The current bounded
population has 40,148 rows split chronologically into 27,979 train, 6,205
validation, and 5,964 test rows. The executable data contract remains:

- 50 total materialized columns;
- 41 model input features;
- 4 request-time features;
- 31 point-in-time historical Feast features;
- 6 deterministic derived features;
- identifier, label, split, diagnostic, outcome, balance, and ingestion
  columns excluded from every estimator.

F9 reuses the F7 loader and validation boundary. It does not invoke Spark,
Gold builders, Feast retrieval, Trino, or the four-million-row materialization.
It preserves chronological splits, natural fraud prevalence, merchant-null
rows, same-timestamp semantics, and target-leakage protections.

The committed F8 Random Forest command remains a local fallback that does not
require MLflow. F9 extends shared data, preprocessing, threshold, and metric
logic instead of duplicating them. F8's measured Random Forest results form a
regression baseline; F9 must reproduce them within numeric tolerance when it
runs the same Random Forest configuration.

## Selected architecture

F9 separates model computation from persistence. The modeling layer has no
dependency on PostgreSQL, MinIO, HTTP, or MLflow. The tracking layer receives
structured experiment results and persists them.

```text
F7 bounded training dataset
  -> contract validation and chronological splits
  -> shared candidate experiment engine
       -> Dummy Classifier control
       -> Logistic Regression candidate
       -> Random Forest candidate
       -> Histogram Gradient Boosting candidate
  -> validation-only threshold and winner selection
  -> one final test evaluation for the winner
  -> threshold-aware MLflow Model package
  -> MLflow Tracking Server on localhost:5000
       -> PostgreSQL `mlflow` database for metadata and Registry
       -> MinIO `mlflow-artifacts` bucket for artifacts
  -> manual or automatic Model Registry workflow
```

This boundary avoids three rejected designs:

1. MLflow calls embedded throughout `modeling/run.py`, which would couple
   training correctness to server availability and complicate unit tests;
2. a second F9 implementation that duplicates the F8 training loop and risks
   metric drift;
3. automatic registration of every candidate, which would turn Model Registry
   into an experiment artifact list rather than a deployable-model catalog.

## Module responsibilities

The design adds focused modules while preserving existing public behavior:

```text
ai_platform/ml/
├── environment.yml
├── requirements.txt
├── config/
│   └── f9_mlflow_experiment.yaml
├── modeling/
│   ├── config.py
│   ├── data.py
│   ├── features.py
│   ├── models.py
│   ├── evaluation.py
│   ├── experiment.py
│   └── run.py
└── tracking/
    ├── __init__.py
    ├── config.py
    ├── mlflow_tracker.py
    ├── model.py
    └── run.py
```

`modeling/experiment.py` owns candidate execution, validation thresholding,
winner selection, and the final test evaluation. It returns typed results and
never imports MLflow.

`tracking/mlflow_tracker.py` owns parent and child runs, parameters, metrics,
tags, artifacts, model logging, registration, version readiness, aliases, and
reload parity. It does not decide which candidate wins.

`tracking/model.py` owns the deployable threshold-aware model contract.

`tracking/run.py` is the F9 command-line entrypoint. It validates configuration,
performs preflight checks, invokes the experiment engine, publishes the result,
and returns a nonzero code for runtime or persistence failures.

The main classes documented for the low-level ML design are:

- `ExperimentConfig`: validated data, candidate, selection, and tracking
  configuration;
- `CandidateTrainer`: fit and validate one configured candidate;
- `ExperimentRunner`: run the fixed matrix and select its validation winner;
- `MlflowTracker`: persist the experiment and Registry lifecycle;
- `FraudPyFuncModel`: expose probability and thresholded prediction outputs.

Functions remain appropriate for stateless transformations. The design does
not force every operation into a class.

## Conda environment and dependency contract

The canonical runtime is one Conda environment named `ewallet-ml`. It replaces
the ignored `ai_platform/ml/.venv` only after F7 and F8 regression checks pass
inside the new environment.

`ai_platform/ml/environment.yml` declares Python 3.10 and pip, then installs
the repository-relative `requirements.txt`. The pip file remains the exact
Python-package manifest used by the environment and the MLflow Model package.
The initial direct pins are:

```text
pandas==2.3.3
pyarrow==25.0.1
scikit-learn==1.7.2
PyYAML==6.0.3
mlflow==3.17.1
psycopg2-binary==2.9.13
boto3==1.43.114
```

The environment is created from the repository root:

```bash
conda env create -f ai_platform/ml/environment.yml
conda activate ewallet-ml
python --version
python -m pip check
mlflow --version
```

The expected interpreter is Python 3.10 and the expected MLflow version is
3.17.1. Client and server use this same environment and MLflow version.
Dependency changes are made in tracked manifests and verified in a recreated
environment. Ad hoc global or base-environment installation is unsupported.

The old `.venv` remains ignored and is deleted manually only after the Conda
environment reproduces relevant tests and the F8 Random Forest baseline.
Historical implementation plans may retain their original `.venv` commands;
active operational documentation uses `conda activate ewallet-ml`.

## Infrastructure and storage

F9 uses the existing PostgreSQL and MinIO services. It does not add MLflow to
Docker Compose in this phase. The operator starts MLflow as a local process so
server logs, process lifetime, health checks, and UI behavior remain visible
during the first learning cycle.

The existing PostgreSQL instance receives a dedicated logical database and
user:

```text
PostgreSQL instance
├── database `ewallet`: existing CDC/business source
└── database `mlflow`: MLflow tracking and Registry metadata
    └── owner/user `mlflow`
```

F9 never creates MLflow tables in the `ewallet` database. A reproducible,
idempotent bootstrap command creates the role and database from environment
variables. Existing PostgreSQL data and volumes are not recreated.

MinIO receives a separate `mlflow-artifacts` bucket. It does not share a
prefix with canonical Bronze, Silver, Gold, Feast, or training data. Bucket
creation is idempotent and does not delete existing objects.

The local server binds to `127.0.0.1:5000`. Its effective architecture is:

```text
Training client
  -> HTTP http://127.0.0.1:5000
  -> MLflow Tracking Server
       -> PostgreSQL metadata through MLFLOW_BACKEND_STORE_URI
       -> MinIO artifacts through the S3-compatible endpoint
```

The server proxies artifact access with `--artifacts-destination
s3://mlflow-artifacts`. The client does not receive PostgreSQL or MinIO
credentials. Database and object-store credentials exist only in the server
process environment and an ignored local environment file or interactive
shell. No secret or developer-specific absolute path is committed.

Database schema changes use `mlflow db upgrade` before the server starts. A
version change requires a database backup and explicit migration review; F9
does not silently migrate a shared database during a training command.

## Experiment configuration

`f9_mlflow_experiment.yaml` contains repository-relative data paths, seed,
selection rules, candidate definitions, Tracking Server URI, experiment name,
registered-model name, and Registry mode. It contains no infrastructure
credential.

The effective model matrix is fixed:

| Name | Estimator | Selection eligible | Main parameters |
|---|---|---:|---|
| `dummy_baseline` | `DummyClassifier` | No | `strategy=prior` |
| `logistic_regression` | `LogisticRegression` | Yes | `C=1.0`, `max_iter=1000`, balanced classes |
| `random_forest` | `RandomForestClassifier` | Yes | 200 trees, depth 12, leaf minimum 2, one job, balanced classes |
| `hist_gradient_boosting` | `HistGradientBoostingClassifier` | Yes | 150 iterations, 31 leaf nodes, learning rate 0.1, L2 0.1, balanced classes |

All stochastic estimators use seed 42. All candidates receive the same rows,
labels, split boundaries, feature contract, null semantics, and categorical
semantics. Logistic Regression scales numeric features. Tree estimators do
not. All fitted preprocessing remains inside each serialized pipeline.

PCA variants, selected-feature Logistic Regression, PCA tree variants,
XGBoost, broad hyperparameter search, cross-validation across temporal splits,
and oversampling are excluded. Their absence keeps F9 focused on lifecycle
tracking rather than reopening F8's retired feature-representation matrix.

Registry mode is explicit:

```yaml
registry:
  mode: manual  # or automatic
```

Unsupported values fail configuration validation before infrastructure or
dataset access.

## Tracking hierarchy and logged data

Each invocation creates one parent run named with the fraud comparison and UTC
timestamp. The parent records shared provenance:

- Git branch and commit;
- dataset path, contract version, and SHA-256 fingerprint;
- train, validation, and test counts and fraud rates;
- input and transformed feature counts;
- candidate inventory and selection-eligibility flags;
- primary metric and deterministic tie-break order;
- winning candidate and winning child-run ID;
- total runtime and final Registry status.

The parent has four nested child runs. Each child records:

- estimator, strategy, seed, and flattened hyperparameters;
- fit and validation-prediction time;
- validation PR-AUC, ROC-AUC, precision, recall, F1, and confusion counts;
- its validation-selected threshold;
- feature summary and validation report artifacts;
- a threshold-aware MLflow Model artifact.

The Dummy child is tagged `eligible_for_selection=false`. The three real
candidates are tagged `eligible_for_selection=true`.

Only the winning child receives test metrics and scenario diagnostics. Losing
children do not receive test scores. The parent records the winner's test
summary for navigation without using it for ranking.

F9 logs dataset metadata and fingerprint, not another copy of the 40,148-row
Parquet dataset. MLflow dataset lineage records the bounded source and its
digest. Incremental data-version storage remains a separate rubric task;
fingerprinting alone is not represented as full incremental data versioning.

## Thresholding and winner selection

Each candidate chooses its decision threshold from validation predictions by
maximizing F1, then recall, then choosing the lower threshold. Candidate
ranking is independent of those threshold tie-breaks.

Eligible candidates are ranked by:

1. greater validation PR-AUC;
2. greater validation F1;
3. greater validation recall;
4. configured candidate order for a deterministic final tie.

Dummy can never win. Test labels, test probabilities, test metrics, model
size, and scenario-specific test recall never influence ranking.

After selection, only the winner is scored on test. Its report includes
overall metrics, confusion matrix, normal-row false-positive behavior, and
recall for `velocity`, `amount_anomaly`, `account_takeover`, and
`merchant_burst`.

## Deployable MLflow Model contract

Logging a raw joblib file is insufficient because it does not provide an
MLflow serving contract. Logging only the fitted scikit-learn pipeline is also
insufficient because its default classifier threshold may differ from the
validation-selected threshold.

`FraudPyFuncModel` packages:

- fitted preprocessing;
- fitted estimator;
- the validation-selected threshold;
- the exact ordered 41-column input contract.

Its prediction output is a Pandas frame with:

```text
fraud_probability: double
fraud_prediction: integer 0 or 1
```

`fraud_probability` comes from the positive-class probability.
`fraud_prediction` is `fraud_probability >= selected_threshold`.

The logged model includes an input example that contains only model features,
an explicit input signature for all 41 columns, an output signature for the
two result columns, and pinned pip requirements. It excludes identifiers,
label, `fraud_type`, split, outcomes, balances, and ingestion metadata.

A deterministic parity sample proves that the local wrapper and the
MLflow-loaded model return equivalent probabilities and identical predictions.

## Manual Registry workflow

The first canonical F9 run uses `registry.mode=manual`. The pipeline trains,
tracks, selects, tests, logs the winner model, and prints its `runs:/` model
URI. It does not create a registered-model version. The parent receives:

```text
registry_status=awaiting_manual_registration
```

The operator then uses MLflow UI under guidance to:

1. open the experiment and comparison parent;
2. compare candidate validation metrics;
3. open the winning child and inspect parameters, metrics, artifacts, input
   signature, and model output signature;
4. register the winner as `ewallet-fraud-classifier`;
5. inspect the new immutable model version;
6. add project, phase, model-family, dataset-contract, feature-contract, Git
   commit, and selection-metric tags;
7. assign the `candidate` alias;
8. reload the registered version and verify prediction parity.

Manual registration is a successful training/tracking outcome with a pending
human action, not a failed run. The runbook specifies the expected screen,
click path, PASS condition, common failure symptoms, relevant logs, and
required coursework screenshot at every step.

## Automatic Registry workflow

After the operator understands and verifies the manual workflow, the final F9
acceptance run uses `registry.mode=automatic`.

The tracking adapter:

1. registers only the winner under `ewallet-fraud-classifier`;
2. waits until the immutable version is ready;
3. applies the same provenance and evaluation tags;
4. reloads that exact version from Model Registry;
5. performs deterministic prediction parity;
6. updates the `candidate` alias only after parity passes.

It never assigns or moves `champion`. Promotion remains a deliberate human
decision after reviewing test metrics, signatures, lineage, and parity. A
future serving API will load `models:/ewallet-fraud-classifier@champion`.
Moving `champion` back to an earlier version provides a Registry-level
rollback without changing API source code.

## Failure behavior and idempotency

The training command checks configuration, dataset validity, and Tracking
Server health before fitting a candidate. An unreachable server fails fast
with its URI and a next diagnostic command.

Every configured candidate is required. If any candidate cannot build, fit,
score, package, or log, the parent experiment fails. F9 never silently drops a
failed model and ranks an incomplete matrix. Completed child runs remain in
MLflow as debugging evidence; no cleanup command deletes them.

An artifact upload failure, PostgreSQL failure, or MinIO failure marks the
affected run failed, prevents registration and alias movement, and returns
exit code 1. Error output identifies whether the failure occurred during
preflight, training, tracking, packaging, registration, or parity.

In automatic mode, registration failure leaves Tracking runs and artifacts
intact but does not modify `candidate`. A version that fails reload parity is
tagged `registry_parity_failed`; the alias continues to reference its previous
version and the command exits 1.

Bootstrap operations are idempotent. Re-running them preserves an existing
PostgreSQL database, role, MinIO bucket, runs, artifacts, versions, and
aliases. Training runs are intentionally append-only and create new run IDs.
Automatic registration intentionally creates a new immutable model version;
it does not overwrite an earlier version.

## Security and runtime safety

Tracked YAML, Python, shell scripts, documentation, and evidence contain no
password, access key, secret key, token, or absolute developer path. Local
credentials are supplied through environment variables or an ignored local
environment file.

The initial MLflow Server binds only to `127.0.0.1`. F9 does not expose it to
the LAN or internet and does not add authentication. External exposure,
gateway authentication, TLS, secret management, and service-to-service
authorization are later deployment tasks.

Generated state remains ignored:

- the Conda environment itself;
- the retired `.venv` until manual removal;
- MLflow temporary files and local logs;
- model artifacts in MinIO;
- PostgreSQL metadata in the existing named volume;
- any local F9 runtime summary;
- Python caches.

F9 does not delete or recreate existing Docker volumes, platform buckets,
canonical data, Feast snapshots, or F7/F8 artifacts.

## Testing and verification

Implementation follows test-driven development. Unit tests use small frames,
temporary directories, fake clients, and deterministic estimators. They do
not require PostgreSQL, MinIO, Docker, or a persistent MLflow Server.

Unit coverage includes:

- Conda and YAML manifest parsing expectations;
- exact candidate inventory and eligibility;
- rejection of missing, duplicate, or unsupported candidates;
- estimator construction, preprocessing differences, and shared split inputs;
- threshold selection and deterministic candidate tie-breaks;
- proof that test data does not participate in selection;
- Dummy exclusion;
- parent/child parameter, metric, tag, and artifact mapping;
- manual mode producing no Registry API call;
- automatic mode registering only the winner;
- alias movement only after Registry reload parity;
- threshold-aware probability and classification outputs;
- exact 41-column signature without forbidden columns;
- candidate, artifact, Registry, and parity failure behavior.

A lightweight integration test starts an isolated temporary MLflow server
with a database-backed temporary store and local artifacts. It verifies parent
and nested runs, model logging, model loading, manual-mode behavior, automatic
registration, immutable version creation, aliases, and parity without touching
the real PostgreSQL or MinIO state.

Runtime acceptance is operator-driven against the real local stack:

1. create and validate `ewallet-ml`;
2. run F7 and F8 regressions in that environment;
3. start existing PostgreSQL and MinIO safely;
4. bootstrap the dedicated database/user and artifact bucket;
5. migrate the MLflow database schema explicitly;
6. start MLflow Server and inspect its logs and health endpoint;
7. run the manual experiment and complete the UI Registry workflow;
8. run the automatic experiment and verify version reload and alias behavior;
9. compare Random Forest metrics with the F8 baseline;
10. confirm no generated or secret artifact is tracked.

The operator records commands, observed run and version IDs, real metrics,
runtime, parity result, and screenshot paths. Evidence is never inferred from
old JSON or fabricated from expected values.

## Documentation and operator runbook

`docs/ml/mlflow_tracking.md` explains architecture, storage roles, the Conda
environment, every command, UI navigation, model comparison, manual and
automatic Registry flows, tags, aliases, promotion, rollback, failure
diagnosis, and current limitations.

Each manual checkpoint states:

- why the step exists;
- the exact command or UI location;
- expected terminal/UI output;
- the PASS condition;
- common failures and which log to inspect;
- required coursework evidence.

README and the documentation index summarize F9 and link to the detailed
runbook. `docs/evidence/f9_mlflow_baseline.json` is created only after the real
acceptance run and stores measured, non-secret evidence. Screenshots are added
only when captured from the real UI and are accompanied by explanatory text.

## Acceptance criteria

F9 is complete when all of the following are true:

- `ewallet-ml` can be recreated from tracked manifests and passes `pip check`;
- F7 and F8 regressions pass in the Conda environment;
- MLflow Server uses the dedicated PostgreSQL database and MinIO bucket;
- one parent and exactly four nested candidate runs are visible;
- Dummy is recorded but cannot win;
- all candidates use identical validated temporal splits and feature inputs;
- validation PR-AUC and deterministic tie-breaks select the winner without
  test access;
- only the winner is evaluated on test;
- all candidate parameters, validation metrics, thresholds, timings, and
  artifacts are reviewable in MLflow UI;
- the winner is a threshold-aware MLflow Model with exact signatures;
- the manual workflow creates a reviewed Registry version and `candidate`
  alias under operator control;
- the automatic workflow registers only the winner, reloads the exact version,
  proves parity, and then moves `candidate`;
- no workflow automatically moves `champion`;
- Random Forest behavior remains compatible with the F8 baseline;
- infrastructure, tracking, packaging, Registry, and parity failures return a
  clear nonzero result without corrupting earlier state;
- credentials, generated artifacts, database state, object-store state, and
  developer-specific paths remain untracked;
- FastAPI serving, Feast Online, Dockerized MLflow, Kubernetes, Kubeflow,
  distributed training, drift detection, and incremental data-version storage
  remain outside F9.
