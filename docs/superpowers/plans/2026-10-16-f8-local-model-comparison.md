# F8 Local Fraud Model Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a reproducible local fraud-model comparison that validates the F7 dataset, compares six model/feature experiments, chooses a champion on validation PR-AUC, evaluates it once on test, and writes reviewable artifacts.

**Architecture:** A versioned YAML config drives focused modules under `ai_platform/ml/modeling`. The loader enforces the existing 50-column F7 contract; scikit-learn pipelines own preprocessing, selection/extraction, and estimation; validation-only evaluation chooses thresholds and the champion; an atomic artifact publisher writes the fitted champion and measured reports. Tests use small real Pandas/Parquet fixtures, while the final integration run uses the ignored 40,148-row dataset.

**Tech Stack:** Python 3.10, Pandas 2.3.3, PyArrow 25.0.1, scikit-learn 1.7.2, PyYAML 6.0.3, joblib, `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-16-f8-local-model-comparison-design.md`

## Global Constraints

- Keep `data_platform/generation/config/settings.yaml` and `data_platform/ingestion/kafka/create_topic.sh` untouched and unstaged.
- Read only `ai_platform/ml/data/fraud_training_v1/trainable_dataset`; do not invoke Spark, Feast, Trino, Gold, or canonical regeneration.
- Fit preprocessing, feature selection, and PCA only on train; validation selects thresholds/champion; test is scored only for the final champion.
- Never include identifiers, `label`, `fraud_type`, `status`, `new_balance`, or `ingested_at` in estimator inputs.
- Preserve natural class prevalence; no oversampling, label-dependent sampling, grid search, or full-scale materialization.
- Do not add MLflow, Kubeflow, Kubernetes, online Feast, or serving dependencies or code.
- Use seed `42` for every stochastic component.
- Generated data, virtual environments, fitted models, and runtime reports remain ignored; only source, tests, config, docs, and measured evidence are tracked.

## Review Focus

- Null cold-history features must remain distinguishable after imputation; `test_preprocessor_adds_missing_indicators_and_handles_unknown_categories` owns this behavior.
- Validation/test-only categories must transform without refitting or failure; the same preprocessing test covers the unknown-category path.
- A duplicated ID across different split folders must fail before training; `test_loader_rejects_cross_split_transaction_overlap` owns this behavior.
- Continuous validation scores with tied best F1 must choose greater recall and then lower threshold; `test_threshold_tie_prefers_recall_then_lower_threshold` owns this behavior.
- Test labels must not be read during candidate ranking; `test_select_champion_uses_validation_results_only` owns this behavior.

---

### Task 1: Reproducible environment and configuration

**Files:**
- Create: `ai_platform/ml/requirements.txt`
- Create: `ai_platform/ml/config/f8_baseline.yaml`
- Create: `ai_platform/ml/modeling/__init__.py`
- Create: `ai_platform/ml/modeling/config.py`
- Modify: `.gitignore`
- Test: `tests/test_fraud_modeling.py`

**Interfaces:**
- Consumes: repository-root relative YAML path and Python 3.10 runtime.
- Produces: `F8Config` dataclass and `load_config(path: Path, repo_root: Path) -> F8Config` with resolved dataset/output paths, seed, PCA variance, selector settings, and ordered candidate definitions.

- [ ] **Step 1: Write the failing configuration test**

Add `FraudModelingConfigTest.test_load_config_resolves_paths_and_six_ordered_candidates`. Use a temporary YAML with literal relative paths and assert resolved paths, seed 42, PCA variance 0.95, and exact candidate order `dummy_full`, `logistic_full`, `logistic_selected`, `logistic_pca`, `random_forest_full`, `hist_gradient_boosting_full`.

- [ ] **Step 2: Run the configuration test and verify RED**

Run: `PYTHONPATH=. ai_platform/features/feast/.venv/bin/python -m unittest tests.test_fraud_modeling.FraudModelingConfigTest -v`

Expected: import failure because `ai_platform.ml.modeling.config` does not exist.

- [ ] **Step 3: Implement the config boundary and dependency declaration**

Implement immutable nested dataclasses for runtime and candidate settings. Validate unknown candidate names, duplicate names, non-positive model parameters, PCA variance outside `(0, 1]`, and a seed other than an integer. Pin `pandas==2.3.3`, `pyarrow==25.0.1`, `scikit-learn==1.7.2`, and `PyYAML==6.0.3`. Add `ai_platform/ml/artifacts/` to `.gitignore`.

- [ ] **Step 4: Create the dedicated Python 3.10 environment**

Run:
```bash
ai_platform/features/feast/.venv/bin/python -m venv ai_platform/ml/.venv
ai_platform/ml/.venv/bin/python -m pip install -r ai_platform/ml/requirements.txt
```

Expected: the pinned versions install without requesting MLflow or Kubeflow.

- [ ] **Step 5: Run the test and verify GREEN**

Run: `PYTHONPATH=. ai_platform/ml/.venv/bin/python -m unittest tests.test_fraud_modeling.FraudModelingConfigTest -v`

Expected: `OK`.

- [ ] **Step 6: Commit Task 1 explicitly**

```bash
git add .gitignore ai_platform/ml/requirements.txt ai_platform/ml/config/f8_baseline.yaml \
  ai_platform/ml/modeling/__init__.py ai_platform/ml/modeling/config.py \
  tests/test_fraud_modeling.py
git commit -m "feat: configure local fraud model comparison"
```

### Task 2: Contract-safe partition loader

**Files:**
- Create: `ai_platform/ml/modeling/data.py`
- Modify: `tests/test_fraud_modeling.py`

**Interfaces:**
- Consumes: `load_dataset(path: Path)`, existing `OUTPUT_COLUMNS`, `MODEL_FEATURE_COLUMNS`, and split directories.
- Produces: `DatasetSplits(train: DataFrame, validation: DataFrame, test: DataFrame)`, `ModelingSplit(X: DataFrame, y: Series, diagnostics: DataFrame)`, and `prepare_modeling_splits(dataset: DatasetSplits) -> dict[str, ModelingSplit]`.

- [ ] **Step 1: Write loader fixture and failing happy-path test**

Add a temporary Hive-style dataset with three literal rows per split and exact 50-column order. `test_loader_returns_exact_temporal_partitions_and_leakage_safe_X` asserts split sizes, 41 ordered X columns, binary y, diagnostic IDs/type, and absence of every forbidden model column.

- [ ] **Step 2: Run the happy-path test and verify RED**

Run the single unittest. Expected: import failure for `modeling.data`.

- [ ] **Step 3: Implement minimal loader and modeling split preparation**

Read each explicit split directory with PyArrow Parquet, restore the contract order, validate it, and return stable reset indexes. Do not rely on automatic Hive partition-column injection.

- [ ] **Step 4: Run the happy-path test and verify GREEN**

Expected: `OK`.

- [ ] **Step 5: Write failing invalid-dataset tests**

Add independent tests for a missing column, invalid/null label, empty partition, stored split mismatch, duplicate transaction inside a split, duplicate transaction across splits, and a split containing only one target class. Each test asserts `ValueError` and the failed condition, not an exact full message.

- [ ] **Step 6: Run invalid-dataset tests and verify RED**

Expected: each new test fails because its validation branch is missing.

- [ ] **Step 7: Implement the validation branches**

Validate each condition before returning `DatasetSplits`; aggregate only safe summary facts, never concatenate model matrices merely to validate them.

- [ ] **Step 8: Run all loader tests and existing F7 contract tests**

Run:
`PYTHONPATH=. ai_platform/features/feast/.venv/bin/python -m unittest tests.test_fraud_modeling.FraudModelingDataTest tests.test_fraud_training_dataset -v`

Expected: all tests `OK`.

- [ ] **Step 9: Commit Task 2 explicitly**

```bash
git add ai_platform/ml/modeling/data.py tests/test_fraud_modeling.py
git commit -m "feat: validate fraud modeling dataset"
```

### Task 3: Train-only preprocessing and feature strategies

**Files:**
- Create: `ai_platform/ml/modeling/features.py`
- Modify: `tests/test_fraud_modeling.py`

**Interfaces:**
- Consumes: the 41-column train X frame and `FeatureStrategy(name, scale_numeric, selector_c, pca_variance)` settings.
- Produces: `build_feature_transformer(strategy: FeatureStrategy, seed: int) -> Pipeline` and `transformed_feature_summary(fitted: Pipeline) -> dict[str, object]`.

- [ ] **Step 1: Write failing full-strategy preprocessing test**

`test_preprocessor_adds_missing_indicators_and_handles_unknown_categories` fits a tiny train frame containing numeric nulls and transforms validation rows containing an unseen `type`/`channel`. Assert finite dense output, unchanged row count, more than 41 transformed columns when indicators/categories require it, and no exception.

- [ ] **Step 2: Run the test and verify RED**

Expected: import failure for `modeling.features`.

- [ ] **Step 3: Implement full preprocessing**

Build numeric median-imputation-with-indicator and categorical most-frequent/OHE branches in a `ColumnTransformer`, force dense output, append `VarianceThreshold`, and expose transformed feature names. Convert boolean inputs without mutating caller frames.

- [ ] **Step 4: Run the full-strategy test and verify GREEN**

Expected: `OK`.

- [ ] **Step 5: Write failing selector and PCA tests**

Add `test_selected_strategy_records_retained_original_transformed_features` with a hand-separable literal target and `test_pca_strategy_retains_requested_train_variance_and_categorical_signals`. Assert the selector reduces features without returning zero and PCA cumulative explained variance is at least 0.95 while categorical output remains present.

- [ ] **Step 6: Run the new tests and verify RED**

Expected: failures because `selected` and `pca` strategies are not implemented.

- [ ] **Step 7: Implement selected and PCA strategies**

Use train-only `SelectFromModel(LogisticRegression(penalty="l1", solver="liblinear", class_weight="balanced", random_state=42))`. Put PCA inside the numeric branch after imputation/scaling and leave categorical OHE outside PCA. Return stable selected/component metadata for reports.

- [ ] **Step 8: Run all feature tests and verify GREEN**

Run the feature test class twice to establish deterministic output. Expected: both runs `OK`.

- [ ] **Step 9: Commit Task 3 explicitly**

```bash
git add ai_platform/ml/modeling/features.py tests/test_fraud_modeling.py
git commit -m "feat: add fraud feature processing strategies"
```

### Task 4: Candidate models, thresholding, and evaluation

**Files:**
- Create: `ai_platform/ml/modeling/models.py`
- Create: `ai_platform/ml/modeling/evaluation.py`
- Modify: `tests/test_fraud_modeling.py`

**Interfaces:**
- Consumes: `CandidateConfig`, feature transformer, train/validation X/y, validation diagnostics.
- Produces: `build_candidate(config, seed) -> Pipeline`, `choose_threshold(y_true, scores) -> ThresholdResult`, `evaluate_scores(...) -> MetricReport`, `scenario_report(...) -> dict`, and `select_champion(results, configured_order) -> CandidateResult`.

- [ ] **Step 1: Write failing candidate-construction test**

`test_build_candidates_returns_all_six_deterministic_estimators` asserts real estimator classes, configured bounded parameters, seed propagation, and that every candidate is a single serializable scikit-learn pipeline.

- [ ] **Step 2: Run it and verify RED**

Expected: missing `models` module.

- [ ] **Step 3: Implement candidate construction**

Construct Dummy prior, balanced Logistic Regression, balanced Random Forest, and Histogram Gradient Boosting candidates with their configured feature strategies. Keep all estimator parameters in YAML and reject unsupported estimator/strategy combinations.

- [ ] **Step 4: Run candidate test and verify GREEN**

Expected: `OK`.

- [ ] **Step 5: Write failing evaluation tests**

Add literal-score tests for maximum-F1 threshold selection, recall/lower-threshold tie-breaking, PR-AUC/ROC-AUC/confusion matrix values, scenario recall including zero-support scenarios, false-positive rate, and validation-only champion ranking. Hand-calculate the expected small-array values.

- [ ] **Step 6: Run evaluation tests and verify RED**

Expected: missing evaluation functions.

- [ ] **Step 7: Implement evaluation functions**

Use scikit-learn metrics, document PR-AUC as average precision, return JSON-safe primitives, preserve scenario order, and rank by validation PR-AUC then validation F1 then configured order. Evaluation accepts no test frame in `select_champion`.

- [ ] **Step 8: Run model/evaluation tests and verify GREEN**

Expected: all tests `OK`.

- [ ] **Step 9: Commit Task 4 explicitly**

```bash
git add ai_platform/ml/modeling/models.py ai_platform/ml/modeling/evaluation.py \
  tests/test_fraud_modeling.py
git commit -m "feat: compare fraud model candidates"
```

### Task 5: Atomic artifacts and end-to-end CLI

**Files:**
- Create: `ai_platform/ml/modeling/artifacts.py`
- Create: `ai_platform/ml/modeling/run.py`
- Modify: `tests/test_fraud_modeling.py`

**Interfaces:**
- Consumes: `F8Config`, validated splits, candidates, evaluation reports, input manifest.
- Produces: `run_comparison(config: F8Config) -> RunResult`, `publish_artifacts(result, output_dir) -> Path`, and CLI `python -m ai_platform.ml.modeling.run --config <path>` returning 0/1.

- [ ] **Step 1: Write failing atomic-publish test**

`test_publish_artifacts_creates_complete_reloadable_run` supplies a fitted tiny real pipeline/result, publishes to a temporary path, asserts the exact six-file inventory, reloads `champion.joblib`, and verifies identical probabilities. A separate failing test injects an unserializable result and asserts no completed output directory remains.

- [ ] **Step 2: Run artifact tests and verify RED**

Expected: missing `artifacts` module.

- [ ] **Step 3: Implement atomic artifact publication**

Write JSON with sorted keys/indentation, CSV in configured order, joblib model, manifest SHA-256, dependency versions, and inventory into a sibling temporary directory. Replace the completed output only after every file is readable; clean temp output on failure.

- [ ] **Step 4: Run artifact tests and verify GREEN**

Expected: `OK`.

- [ ] **Step 5: Write failing end-to-end synthetic CLI test**

Run `run_comparison` on a deterministic temporary three-split Parquet fixture large enough for all six candidates. Assert all six validation results, configured ordering, exactly one test report for the selected champion, non-zero measured durations, and a reloadable champion.

- [ ] **Step 6: Run integration test and verify RED**

Expected: missing orchestration behavior.

- [ ] **Step 7: Implement sequential orchestration and CLI**

Load once, fit candidates sequentially, capture validation metrics/thresholds and feature summaries, release non-champion pipelines, deterministically refit only the selected candidate on train, score test once, publish outputs, and print the comparison/champion tables. Catch top-level runtime errors only to print `ERROR | ...` and return 1.

- [ ] **Step 8: Run integration and full F8 tests**

Run: `PYTHONPATH=. ai_platform/ml/.venv/bin/python -m unittest tests.test_fraud_modeling -v`

Expected: all tests `OK` and no warnings caused by invalid metric calls.

- [ ] **Step 9: Commit Task 5 explicitly**

```bash
git add ai_platform/ml/modeling/artifacts.py ai_platform/ml/modeling/run.py \
  tests/test_fraud_modeling.py
git commit -m "feat: run local fraud model comparison"
```

### Task 6: Real bounded run, documentation, and evidence

**Files:**
- Create: `docs/ml/fraud_modeling.md`
- Create: `docs/evidence/f8_local_model_baseline.json`
- Modify: `docs/README.md`
- Modify: `docs/07_performance_benchmarks.md` only if that exact file exists; otherwise update the repository's actual performance benchmark document.

**Interfaces:**
- Consumes: real ignored F7 dataset, completed F8 CLI, runtime artifacts.
- Produces: measured human documentation and tracked evidence that distinguish validation selection from final test evaluation.

- [ ] **Step 1: Verify the dedicated environment**

Run:
```bash
ai_platform/ml/.venv/bin/python --version
ai_platform/ml/.venv/bin/python -m pip check
ai_platform/ml/.venv/bin/python - <<'PY'
import pandas, pyarrow, sklearn, yaml
print(pandas.__version__, pyarrow.__version__, sklearn.__version__, yaml.__version__)
PY
```

Expected: Python 3.10 and the pinned dependency versions; `pip check` passes.

- [ ] **Step 2: Run lightweight verification before real training**

Run Python compile and YAML parse, then run `tests.test_fraud_modeling` and
`tests.test_fraud_training_dataset` with `ai_platform/ml/.venv/bin/python`.
Run `tests.test_feast_offline` separately with
`ai_platform/features/feast/.venv/bin/python`, because the ML environment
deliberately does not install Feast. Expected: all pass.

- [ ] **Step 3: Run the real comparison once**

Run:
`PYTHONPATH=. ai_platform/ml/.venv/bin/python -m ai_platform.ml.modeling.run --config ai_platform/ml/config/f8_baseline.yaml`

Expected: six validation rows, one champion test report, exit 0, no Spark/Feast/Trino/Kubernetes process started.

- [ ] **Step 4: Verify runtime artifacts and resource behavior**

Parse every JSON/CSV, load `champion.joblib`, reproduce test probabilities/metrics, confirm exact artifact inventory, record wall time/output size/final free disk, and verify `git status --short` shows no artifact or virtual-environment files.

- [ ] **Step 5: Write measured docs/evidence**

Document the data flow, six experiments, feature selection/PCA semantics, exact validation comparison, champion threshold, final test metrics/scenario recall, runtime, artifact lifecycle, and the deferred MLflow/Kubeflow path. Evidence values come only from runtime artifacts.

- [ ] **Step 6: Run final repository validation**

Run all project unittests, Python compile for changed Python, YAML/JSON parsing, documentation relative-link checks, `git diff --check`, and explicit status/diff review. Report pre-existing failures separately and do not change unrelated files.

- [ ] **Step 7: Commit Task 6 explicitly**

```bash
git add docs/ml/fraud_modeling.md docs/evidence/f8_local_model_baseline.json \
  docs/README.md docs/07_performance_benchmarks.md
git commit -m "docs: record F8 local fraud model baseline"
```

- [ ] **Step 8: Final state audit**

Confirm the branch, HEAD commits, runtime artifacts ignored, F7 data unchanged, two pre-existing user files still modified but untouched, and no MLflow/Kubeflow/Kubernetes/online-store changes.
