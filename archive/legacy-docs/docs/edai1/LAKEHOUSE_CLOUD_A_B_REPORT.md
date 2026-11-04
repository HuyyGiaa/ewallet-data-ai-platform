# Lakehouse cloud A/B report

No paid cloud A/B was started. The current harness can select a canonical Delta table and shallow-clone it, but a full source selection could rewrite a large historical table and violates the V5 prohibition on cloning the complete 100 GB dataset. In addition, delayed billing plus still-running GKE resources prevent a credible conservative upper bound below the 5,000,000 VND cap.

The safe next implementation must select a small immutable partition/sample, write only to a unique TTL scratch prefix, record object/file/byte/partition counts before and after, execute at least five identical query repetitions per side, and require row count plus deterministic content-hash equality before cleanup. The local bounded proof remains 2,188→151 files and 0.2190→0.1079 seconds.

