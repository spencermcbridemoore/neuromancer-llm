### ADR-0033 — Dense-lane ≤500 GB; single-layer default for 8–9B
**Status:** Accepted · **Source:** phase2 E9.
**Decision.** The TTL dense lane consumes **≤500 GB** desktop NVMe. **Single-layer capture is the default for 8–9B models**; all-layer is reserved for explicitly planned sweeps; TTLs are short (days). Cluster scratch is in-scope TTL territory (ADR-0010 purge-window).
**Consequences.** Sizing constant for the worker's VRAM/disk preflight and the TTL reaper. Drives the capture contract's default `layer_selection`.
