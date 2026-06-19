### ADR-0035 — 4090 host: WSL2, HF cache on ext4
**Status:** Accepted · **Source:** phase2 E11.
**Decision.** The desktop stays Windows; the worker runs under **WSL2** with the HF cache + dense lane on **ext4 inside WSL2** (avoids the 9p I/O penalty). cuda-checkpoint/CRIU stays off the table (acceptable — checkpoint-first design targets vast.ai). The Windows Scheduler hosts the desktop health agent (ADR-0015).
**Consequences.** Cold-start row of the worker math table assumes ext4-resident cache; the `[research]` benchmark (WSL2-ext4 vs native) is an open implementation item, not a blocker.
