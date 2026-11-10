#!/usr/bin/env bash
# Owner launcher for the EDAI-2 single-run evidence session (one run per arm, every level).
# usage: EXP_APPROVED=yes owner_single_run.sh <exp-v1|exp-v2> <campaign-dir>
#
# It only wraps vllm_bench.sh, so the runner's behaviour, limits, watchdog and cleanup are
# unchanged. What it adds:
#  - EXP_PROFILE=single EXP_RUNS=1 EXP_CAPTURE_MODE=interactive EXP_CHECKPOINT_RUNS=first
#    EXP_MAX_MINUTES=215 are always set here and override the shell, so this command cannot
#    start a three-run or a smoke session by accident;
#  - it refuses to start outside tmux, so closing an editor or a terminal window cannot kill
#    the session (detach with Ctrl-b d, reattach with tmux attach -t <name>);
#  - it holds a systemd-inhibit lock against sleep, idle and the lid switch while it runs,
#    including the runner's cleanup after Ctrl-C (the lock does not survive a logout or a
#    shutdown);
#  - Ctrl-C stops the runner, which cleans up; the log copy (tee) survives it, so the
#    runner's cleanup output cannot die of a broken pipe;
#  - it refuses source changes outside the campaign directory, so manifest.source_sha names
#    the code that ran;
#  - the runner's output goes to <campaign>/<experiment>.terminal.log as well as the screen,
#    followed by a "runner exit <status>" line.
# Checkpoints read CONTINUE / ABORT from this terminal, exactly as the runner does.
# OWNER_INHIBIT=off skips the inhibit lock (offline tests only).
set -euo pipefail
die() { echo "owner_single_run: $*" >&2; exit 2; }
experiment=${1:-} campaign=${2:-}
case "$experiment" in exp-v1|exp-v2) ;; *) die "usage: owner_single_run.sh <exp-v1|exp-v2> <campaign-dir>";; esac
[ -n "$campaign" ] || die "campaign directory required"
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
[ -n "${TMUX:-}" ] || die "start tmux first (tmux new -s bench), then run this inside it"
[ "${EXP_APPROVED:-}" = yes ] || die "set EXP_APPROVED=yes after owner approval"
: "${KUBE_CONTEXT:?KUBE_CONTEXT required}" "${VLLM_IMAGE:?VLLM_IMAGE required}"
campaign=${campaign%/}
case "$campaign" in
  "$root"/*) campaign=${campaign#"$root"/};;
  /*) die "the campaign directory must be inside $root";;
esac
[ ! -e "$campaign/$experiment" ] || die "$campaign/$experiment exists; evidence is never overwritten"
outside=$(git status --porcelain --untracked-files=all | grep -v " $campaign/" || true)
[ -z "$outside" ] || die "source changes outside $campaign; commit or remove them first:
$outside"
mkdir -p "$campaign/screenshots"
log_file="$campaign/$experiment.terminal.log"
if [ "${OWNER_INHIBIT:-on}" != off ]; then
  command -v systemd-inhibit >/dev/null || die "systemd-inhibit not found; keep the laptop awake by hand and set OWNER_INHIBIT=off"
  # A separate holder, not a wrapper around the runner: a background job of this script
  # ignores Ctrl-C, so the lock stays while the runner cleans up, and `tail --pid` ends it
  # when this script exits.
  systemd-inhibit --what=sleep:idle:handle-lid-switch --who=ewallet-bench \
    --why="$experiment single-run evidence session" --mode=block tail --pid=$$ -f /dev/null </dev/null >/dev/null 2>&1 &
  sleep 1
  kill -0 $! 2>/dev/null || die "could not take the sleep/idle/lid inhibit lock"
fi
# Ctrl-C goes to the runner, which stops and cleans up. This script keeps waiting so the
# exit line is written, and tee ignores Ctrl-C: if tee died, the runner's next line of
# output during cleanup would end it with SIGPIPE.
trap 'echo "owner_single_run: Ctrl-C sent to the runner; wait for it to clean up" >&2' INT
echo "owner_single_run: $experiment single-run (1 run per arm, all levels), log $log_file"
set +e
env EXP_PROFILE=single EXP_RUNS=1 EXP_CAPTURE_MODE=interactive EXP_CHECKPOINT_RUNS=first EXP_MAX_MINUTES=215 \
  "$root/scripts/experiments/vllm_bench.sh" "$experiment" session "$campaign/$experiment" 2>&1 \
  | (trap '' INT; exec tee "$log_file")
status=${PIPESTATUS[0]}
set -e
echo "runner exit $status at $(date -u +%FT%TZ)" | tee -a "$log_file"
exit "$status"
