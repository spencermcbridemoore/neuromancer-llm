### ADR-0042 — VM resize deferral; Cinder volume instead
**Status:** Accepted (from pole A, resize NOT adopted) · **Source:** checkpoint "From A".
**Decision.** Attach a **zero-SU 150 GB Cinder volume** for PGDATA/WAL (unbinds the 20 GB root disk with no resize, pole-independent win). The VM-resize itself is **NOT adopted** — only a `vm_resize` trigger table is reserved (deferral ADR), so the m3.small stands (ADR-0001). A `capture_events` partitioning trigger is likewise reserved as a deferral ADR, not pre-applied.
**Consequences.** Avoids A's ~35k-SU resize that scored A down on owner-fit. Partitioning is a documented trigger that fires only if `capture_events` cardinality demands it.

---
