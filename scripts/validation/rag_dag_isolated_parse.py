"""Isolated, read-only DagBag parse of rag_fraud_policy_ingestion; run via validate_rag_dag_import.sh."""
import copy, json, os, sys, tempfile
dag_file, report_path = sys.argv[1], sys.argv[2]
DAG_ID = "rag_fraud_policy_ingestion"
home = os.environ["AIRFLOW_HOME"]
if not os.path.basename(home).startswith("rag-dag-parse.") or not home.startswith(tempfile.gettempdir()):
    sys.exit("refusing a non-isolated AIRFLOW_HOME")
if os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] != f"sqlite:///{home}/isolated.db":
    sys.exit("refusing a non-isolated metadata database")
import airflow
from airflow.dag_processing.dagbag import DagBag
report = {"airflow_version": airflow.__version__, "python": sys.version.split()[0]}
bag = DagBag(dag_folder=dag_file, safe_mode=False)
report["import_errors"] = {k: v.splitlines()[-1] for k, v in bag.import_errors.items()}
report["dag_ids"] = sorted(bag.dag_ids)
dag = bag.dags.get(DAG_ID)
if dag is not None:
    report["tasks"] = {t.task_id: sorted(t.downstream_task_ids) for t in dag.tasks}
    report["operators"] = sorted({type(t).__name__ for t in dag.tasks})
    report["bash_commands_contain_jinja"] = {t.task_id: ("{{" in t.bash_command) for t in dag.tasks}
    report["env_param_templates"] = {t.task_id: sorted(k for k, v in (t.env or {}).items() if "{{" in str(v)) for t in dag.tasks}
    report["append_env"] = {t.task_id: t.append_env for t in dag.tasks}
    report["interpreter_env_present"] = all("EWALLET_RAG_PYTHON" in (t.env or {}) for t in dag.tasks)
    report["retries"] = {t.task_id: t.retries for t in dag.tasks}
    report["execution_timeout_seconds"] = {t.task_id: t.execution_timeout.total_seconds() for t in dag.tasks}
    defaults = dag.params.validate()
    report["param_defaults"] = defaults
    env = dag.get_template_env()
    t = dag.get_task("publish_feast_features")
    report["rendered_env_defaults"] = {k: env.from_string(v).render(params=defaults, run_id="manual__2026-11-01T00:00:00+00:00")
                                       for k, v in t.env.items() if k.startswith("RAG_")}
    rejected = {}
    for name, value in (("run_label", "x'; touch /tmp/p; '"), ("run_label", "run-001\n"),
                        ("source_location", "/etc"), ("source_location", "data/llm_agent/rag/../../etc"),
                        ("source_location", "http://attacker.example/x"), ("online_store", "memory"),
                        ("datahub_mode", "skip")):
        params = copy.deepcopy(dag.params)
        try:
            params[name] = value
            params.validate()
            rejected[f"{name}={value!r}"] = "ACCEPTED"
        except Exception as error:
            rejected[f"{name}={value!r}"] = type(error).__name__
    report["malicious_param_results"] = rejected
root = os.path.dirname(os.path.abspath(__file__)).rsplit("/scripts/", 1)[0]
with open(report_path, "w", encoding="utf-8") as stream:
    stream.write(json.dumps(report, indent=1, sort_keys=True).replace(root, "<repo>") + "\n")
sys.exit(1 if report["import_errors"] or DAG_ID not in report["dag_ids"] else 0)
