#!/usr/bin/env bash
# Host resource guard for local LLM/Agent runtime steps. Prints one sample
# every INTERVAL seconds and exits 3 on the first breach of the runbook gate
# (MemAvailable < 8 GiB, normalized load > 0.8, Airflow 8084 unhealthy).
# usage: scripts/llm_agent/resource_sample.sh [interval_s] [count]
set -uo pipefail
interval=${1:-10}
count=${2:-0}
cpus=$(nproc)
i=0
while :; do
  read -r load1 _ < /proc/loadavg
  mem_kib=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
  psi=$(awk '/^some/ {print $2}' /proc/pressure/memory)
  health=$(curl -s -m 5 http://localhost:8084/api/v2/monitor/health || true)
  airflow=unhealthy
  if grep -q '"scheduler":{"status":"healthy"' <<<"$health"; then airflow=healthy; fi
  echo "$(date -u +%FT%TZ) load1=$load1 cpus=$cpus mem_avail_gib=$((mem_kib / 1048576)) psi_mem_$psi airflow=$airflow"
  if [ "$mem_kib" -lt $((8 * 1048576)) ] || [ "$airflow" != healthy ] || \
     awk -v l="$load1" -v c="$cpus" 'BEGIN {exit !(l / c > 0.8)}'; then
    echo "BREACH: stop the runtime step and scale LLM workloads to zero" >&2
    exit 3
  fi
  i=$((i + 1))
  if [ "$count" -gt 0 ] && [ "$i" -ge "$count" ]; then exit 0; fi
  sleep "$interval"
done
