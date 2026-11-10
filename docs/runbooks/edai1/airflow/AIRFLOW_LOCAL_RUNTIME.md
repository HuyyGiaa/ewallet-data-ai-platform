# Local Airflow runtime

Audience: the project owner, running Airflow 3.3.1 on the laptop for the EDAI-1 and RAG DAGs.

The runtime has three components, all bound to this repository's `AIRFLOW_HOME`
(`data_platform/orchestration/airflow`):

| Component | Process | Health key |
|---|---|---|
| API server and UI | `airflow api-server --host 127.0.0.1 --port 8084` | `metadatabase` |
| Scheduler | `airflow scheduler` (LocalExecutor) | `scheduler` |
| DAG processor | `airflow dag-processor` | `dag_processor` |

One helper manages them: [`scripts/start_airflow_local.sh`](../../../../scripts/start_airflow_local.sh),
backed by [`scripts/airflow_local_runtime.py`](../../../../scripts/airflow_local_runtime.py).
It never initializes, migrates or resets the metadata database, and never
triggers or unpauses a DAG.

## Start, status, stop

```bash
./scripts/start_airflow_local.sh            # reuse a healthy runtime, or start what is missing
./scripts/start_airflow_local.sh --status   # read-only; exit 0 only when all three are healthy
./scripts/start_airflow_local.sh --stop     # SIGTERM to this runtime's own processes only
```

**Start** does, in order:

1. If all three components run once each and the health endpoint reports them healthy, it reuses them and exits 0.
2. It refuses to continue (exit 1, nothing killed) if a component runs more than once, or if port 8084 is held by a process that is not this runtime's API server.
3. It runs `airflow db check`. If the database is unavailable, it stops before launching anything.
4. It starts each missing component **one at a time**, as a plain background process in its own session, and waits until the health endpoint reports it healthy before starting the next. A component that is running but not yet healthy is waited for, never duplicated.
5. If a component exits or does not become healthy within `AIRFLOW_LOCAL_START_TIMEOUT` seconds (default 120 per component), it prints that component's last log lines and a diagnosis, leaves the components started earlier running, and exits 1.

The UI is <http://127.0.0.1:8084>. Startup normally takes about 15 seconds.

**Status** reports each component as `RUNNING`, `STOPPED` or `CONFLICT` with its health and process ids. Health is read from the JSON of `/api/v2/monitor/health`. Processes are matched by command line plus `AIRFLOW_HOME` (the API server by its 127.0.0.1:8084 address), so other Airflow installations and the scheduler's executor workers are not counted. It creates no files.

**Stop** sends SIGTERM to the DAG processor, the scheduler and the API server of this runtime, waits up to 60 seconds and never force-kills. Running task instances are interrupted, so check the UI first.

## Environment set by the helper

| Variable | Value | Why |
|---|---|---|
| `AIRFLOW_HOME` | `data_platform/orchestration/airflow` | Configuration, metadata database, logs |
| `AIRFLOW__CORE__DAGS_FOLDER` | `$AIRFLOW_HOME/dags` | The repository DAGs |
| `AIRFLOW__CORE__EXECUTION_API_SERVER_URL` | `http://127.0.0.1:8084/execution/` | Task execution API of this runtime |
| `PYTHONPATH` | repository root, then any existing value | DAG files import `data_platform` and `ops`. Without it every repository DAG fails to import and is marked stale |

The Airflow CLI is taken from `AIRFLOW_BIN`, the active `airflow` conda environment, or `~/miniconda3/envs/airflow/bin/airflow`.

Component logs are in `$AIRFLOW_HOME/logs/local-runtime/<component>.{out,err}`. Each start moves the previous logs to `*.prev`. Process ids are in `<component>.pid`.

## Metadata database

The metadata database is SQLite at `$AIRFLOW_HOME/airflow.db` (`sql_alchemy_conn` in `airflow.cfg`), in WAL mode: Airflow sets `PRAGMA journal_mode=WAL` on every connection, so `airflow.db-wal` and `airflow.db-shm` exist while Airflow runs. Do not delete them, and never run `airflow db reset`.

**Why the helper never uses `--daemon`.** Airflow's `--daemon` flag uses python-daemon's `DaemonContext`, which closes every file descriptor the process inherited except stdout and stderr. When the CLI has already opened the SQLite database before forking, the daemon's SQLite connection loses its descriptors and its POSIX locks, and the next WAL operation fails with `sqlite3.OperationalError: locking protocol` (`SQLITE_PROTOCOL`). On isolated copies of this database, the old `--daemon` sequence failed in every run (8 of 8), in the DAG processor or the scheduler. Plain background processes started in sequence ran healthy with no locking errors. SQLite is therefore kept; a PostgreSQL metadata database is not needed for this single-user runtime.

**Backup before any maintenance**, with Airflow stopped or running (the SQLite backup API gives a consistent copy, WAL included):

```bash
B=~/Develop/ewallet-local-artifacts/airflow-metadata-backup-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -m 700 "$B"
python3 - "$B" <<'EOF'
import sqlite3, sys
src = sqlite3.connect("file:data_platform/orchestration/airflow/airflow.db?mode=ro", uri=True)
dst = sqlite3.connect(f"{sys.argv[1]}/airflow.db"); src.backup(dst); dst.close()
print(sqlite3.connect(f"{sys.argv[1]}/airflow.db").execute("pragma integrity_check").fetchone()[0])
EOF
(cd "$B" && sha256sum airflow.db > SHA256SUMS && chmod 600 airflow.db SHA256SUMS)
```

## Recovery

| Symptom | Cause | Action |
|---|---|---|
| `locking protocol` in a component's `.err`, component exits | Started with `--daemon` (old helper, or by hand) | `--stop`, then start with the helper. Do not delete the WAL/SHM files |
| `--status` shows `dag-processor STOPPED`, others healthy | Partial start or a crash | Run the helper again: it starts only the DAG processor |
| `CONFLICT` for a component | The same component started twice for this `AIRFLOW_HOME` | Inspect `ps -o pid,lstart,args -p <pids>`; stop the extra one with `kill -TERM <pid>`; then rerun the helper |
| `port 8084 is in use` | Another program, or an API server of another Airflow home | Stop that program, or free the port |
| `airflow db check failed` | Database file missing, unreadable, or locked by an external tool | Fix the file or close the tool; the helper changes nothing |
| Health shows `dag_processor: unhealthy` with an old heartbeat while nothing runs | An earlier DAG processor job was never closed | Harmless; a running DAG processor writes a new heartbeat and becomes the latest |
| DAGs missing, UI shows import errors `No module named 'data_platform'` | Components started without the repository on `PYTHONPATH` | `--stop`, then start with the helper |

Check DAG parsing after a start (read-only):

```bash
export AIRFLOW_HOME=$PWD/data_platform/orchestration/airflow PYTHONPATH=$PWD
~/miniconda3/envs/airflow/bin/airflow dags list-import-errors
~/miniconda3/envs/airflow/bin/airflow dags list | grep edai1_data_pipeline_campaign
```

## Tests

```bash
~/miniconda3/envs/airflow/bin/python -m unittest tests.unit.test_airflow_local_runtime
PATH=~/miniconda3/envs/fintech/bin:$PATH python -m unittest tests.unit.test_canonical_airflow_scripts
```

The unit tests cover healthy detection, missing and crashed components, the SQLite locking diagnosis, partial start, process-discovery races, reuse, duplicate protection, an unavailable database, read-only status, log rotation and the start timeout.
