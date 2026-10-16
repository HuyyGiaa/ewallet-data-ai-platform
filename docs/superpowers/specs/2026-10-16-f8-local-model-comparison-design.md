# F8 Local Fraud Model Comparison Design

## Status and intent

F8 turns the point-in-time-correct F7 training dataset into a reproducible
local model-comparison pipeline. The primary outcome is a reliable, observable
ML workflow rather than a maximized accuracy score. A single command must load
the bounded dataset, validate its contract, run a small set of meaningful
experiments, select a champion using validation data, evaluate that champion
once on test data, and write reviewable local artifacts.

MLflow, Kubeflow, Kubernetes, online Feast, model serving, hyperparameter
search, and full four-million-row materialization are outside this task. They
will consume this local pipeline only after its behavior and outputs have been
reviewed.

## Inputs and boundaries

The input is the ignored F7 runtime dataset at
`ai_platform/ml/data/fraud_training_v1/trainable_dataset/`, partitioned into
`split=train`, `split=validation`, and `split=test`. Its executable schema is
`ai_platform.ml.training.contract.OUTPUT_COLUMNS`:

- 9 identifier, evaluation, and split columns;
- 4 request-time model features;
- 31 point-in-time historical features retrieved through Feast;
- 6 deterministic derived features;
- 41 total raw model features and 50 total output columns.

F8 reads the materialized dataset and does not invoke Spark, Gold builders, or
Feast retrieval. It never recomputes temporal splits, resamples labels, or
oversamples fraud. `fraud_type` is available only for diagnostic evaluation;
identifiers, target, diagnostic, outcome, and ingestion columns never enter a
model matrix.

The expected current bounded population is 40,148 rows: 27,979 train, 6,205
validation, and 5,964 test. The implementation validates the on-disk contract
rather than silently accepting a different or incomplete dataset.

## Architecture and data flow

The implementation lives under `ai_platform/ml/modeling/` and has focused
modules for data loading, feature preprocessing, candidate construction,
evaluation, artifact writing, and the command-line entrypoint.

```text
F7 partitioned Parquet
  -> load and validate exact contract
  -> X/y/diagnostic frames per temporal split
  -> fit train-only preprocessing and feature strategy
  -> fit candidate model on train
  -> score validation and choose its threshold
  -> rank candidates by validation PR-AUC
  -> evaluate the champion once on test
  -> write deterministic JSON/CSV/joblib artifacts
```

The run is controlled by a versioned YAML configuration. Dataset paths,
random seed, candidate parameters, threshold objective, and output paths are
configuration values. Relative paths resolve from the repository root.

## Data contract and validation

The loader uses PyArrow to read Hive-style split partitions and returns one
Pandas frame per split. It validates all of the following before training:

- the exact ordered 50-column output contract;
- non-empty train, validation, and test partitions;
- unique, non-null `transaction_id` values with no cross-split overlap;
- binary, non-null labels;
- stored `split` values matching their containing partition;
- all 41 model features present exactly once;
- forbidden model columns absent from the model feature contract;
- both target classes present in every split.

Errors identify the failed condition and stop before any candidate is fitted.

## Feature processing

The raw contract has 41 model inputs. `type` and `channel` are categorical.
The remaining 39 columns are numeric or boolean, including `amount`,
`merchant_present`, 31 historical features, and 6 derived features.

Categorical preprocessing uses most-frequent imputation followed by
`OneHotEncoder(handle_unknown="ignore")`. Existing `UNKNOWN` channel values
remain a real category. Numeric preprocessing uses median imputation with
missing indicators. Missing indicators preserve cold-history information such
as an unavailable failed rate or standard deviation. `merchant_present` is
converted consistently to numeric 0/1.

Logistic and PCA paths standardize numeric values. Tree paths do not scale
numeric values. All fitted preprocessing is contained in the serialized
scikit-learn pipeline so later inference cannot bypass or recreate transforms
differently.

F8 recognizes three feature strategies:

1. `full`: the validated contract after preprocessing, with a variance filter
   that removes only constant transformed columns;
2. `selected`: the same preprocessing followed by train-only L1 logistic
   `SelectFromModel`, recording retained and removed transformed feature names;
3. `pca`: scaled numeric inputs reduced to the smallest component count that
   retains at least 95% train variance, while one-hot categorical inputs remain
   available as categorical signals.

No transformation is fitted on validation or test data. Selection and PCA are
pipeline demonstrations, not claims that reduced representations must improve
fraud accuracy.

## Candidate experiments

The fixed experiment matrix is intentionally small:

| Experiment | Feature strategy | Estimator |
|---|---|---|
| `dummy_full` | full | `DummyClassifier` |
| `logistic_full` | full | `LogisticRegression` |
| `logistic_selected` | selected | `LogisticRegression` |
| `logistic_pca` | pca | `LogisticRegression` |
| `random_forest_full` | full | `RandomForestClassifier` |
| `random_forest_pca` | pca | `RandomForestClassifier` |
| `hist_gradient_boosting_full` | full | `HistGradientBoostingClassifier` |
| `hist_gradient_boosting_pca` | pca | `HistGradientBoostingClassifier` |

All stochastic components use seed 42. Candidate settings favor bounded local
runtime and reproducibility over broad tuning. F8 does not perform grid search,
cross-validation over temporal partitions, or automated hyperparameter
optimization.

## Thresholding and model selection

Every candidate exposes a validation fraud score or probability. Validation
PR-AUC is the primary ranking metric because fraud prevalence is approximately
2%. ROC-AUC, precision, recall, F1, confusion matrix, prediction time, and
training time are also recorded.

For each candidate, the decision threshold is selected only from validation
predictions by maximizing F1. If multiple thresholds have the same maximum F1,
the candidate uses the threshold with greater recall; any remaining tie uses
the lowest threshold. The champion is the candidate with the highest
validation PR-AUC. Deterministic secondary ordering uses validation F1 and then
the configured experiment order.

Only the champion is scored on test. Test labels do not influence candidate
ranking, feature selection, PCA, preprocessing, hyperparameters, or threshold.
The final test report includes overall metrics and recall for `velocity`,
`amount_anomaly`, `account_takeover`, and `merchant_burst`. Normal rows are
reported through false-positive counts/rates. `fraud_type` never enters model
features.

## Outputs and lifecycle

Generated outputs live under
`ai_platform/ml/artifacts/f8_local_baseline/` and remain ignored runtime
artifacts:

- `comparison.json`: configuration, dataset summary, feature counts, timings,
  validation metrics, and candidate ordering;
- `comparison.csv`: compact human-readable candidate table;
- `champion.joblib`: fitted preprocessing, feature strategy, and estimator;
- `selected_features.json`: selected/transformed feature details for applicable
  experiments;
- `test_report.json`: champion threshold, test metrics, confusion matrix, and
  per-scenario recall;
- `run_manifest.json`: input manifest fingerprint, dependency versions, seed,
  timestamps, and artifact inventory.

Artifact writing uses a temporary run directory and promotes it only after all
experiments and the final test evaluation pass. An interrupted or failed run
must not look complete. Re-running replaces only the F8 output directory and
never modifies F7 data.

The CLI prints a concise comparison table and final champion report so results
are reviewable without another service. A tracked evidence JSON and modeling
documentation may be added only after the real run has been reviewed; measured
values are never fabricated or copied from validation into test fields.

## Dependencies and runtime

F8 receives a versioned dependency declaration under `ai_platform/ml/` and a
dedicated local virtual environment that is ignored by Git. Direct runtime
dependencies include the already used Pandas and PyArrow versions plus a
Python-3.10-compatible pinned scikit-learn release and PyYAML. Joblib is used
through scikit-learn's dependency. MLflow and Kubeflow packages are absent.

The 40,148-row bounded dataset must fit in local memory, but candidate models
run sequentially and released models are not retained in memory after their
validation result is captured. The champion is refitted deterministically only
if needed for serialization; test data remains isolated. No Trino, Spark,
Feast, Docker, or Kubernetes service is started.

## Tests and verification

Implementation follows test-driven development. Unit tests use small synthetic
frames and temporary Parquet/artifact directories. They cover:

- exact contract loading and rejection of missing columns, invalid labels,
  duplicate IDs, split mismatch, and cross-split overlap;
- leakage-safe separation of model, target, and diagnostic columns;
- train-only preprocessing behavior, unknown categorical values, null numeric
  values, feature-name tracking, selection, and PCA variance behavior;
- deterministic candidate construction and random seeds;
- threshold selection and deterministic tie-breaking;
- PR-AUC champion selection without test access;
- confusion matrix and fraud-scenario recall;
- atomic artifact publication and stable JSON/CSV schemas;
- loading `champion.joblib` and reproducing predictions.

After unit tests pass, the real bounded F7 dataset is used for one local F8 run.
Verification includes Python compilation, dependency/config parsing, relevant
F7 regression tests, F8 unit tests, exact artifact inventory, clean model
reload, and `git diff --check`.

## Acceptance criteria

F8 local model comparison is complete when:

- all eight configured experiments run from one command, including PCA paired
  with Random Forest and Histogram Gradient Boosting;
- no forbidden or diagnostic column enters any estimator;
- preprocessing, selection, and extraction fit only train data;
- validation alone selects thresholds and champion;
- the champion is evaluated once on test and reloads with identical scores;
- model, data, feature, timing, and scenario results are written as reviewable
  local artifacts;
- generated datasets and artifacts remain untracked;
- existing F7 point-in-time and dataset tests still pass;
- MLflow, Kubeflow, Kubernetes, online Feast, serving, and full-scale
  materialization remain untouched.
