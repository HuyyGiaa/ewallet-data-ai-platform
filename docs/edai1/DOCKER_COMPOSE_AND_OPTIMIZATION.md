# Docker Compose and optimization

`infra/docker/compose.edai1.yml` adds a bounded, opt-in `edai1` profile for deterministic lineage-plan generation. It uses the optimized multi-stage target, UID/GID 65532, a read-only root filesystem, 16 MiB `/tmp`, no network, no Linux capabilities, `no-new-privileges`, 64 PIDs, 128 MiB memory, 0.5 CPU and no restart.

The Compose config gate passed. A real build and one-shot `run --rm` passed with image ID `sha256:328d201ca8fb513e0ebe7e964f44301d52ae3df8308cc9290a0e34d47877a499`, size 44,172,772 bytes and configured user `65532:65532`. No residual campaign container remained. The earlier controlled A/B used identical source/dependencies and produced 381,961,667 → 44,171,603 bytes (-88.44%) with identical smoke output.

