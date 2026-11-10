# EXP-V1

Binding vLLM's two OpenMP threads to CPUs 0-1 improves output throughput at concurrency 4 compared with automatic binding, on the same pod budget.

Variable: VLLM_CPU_OMP_THREADS_BIND: auto versus 0-1
