"""system_health + the durability-staleness gate (ADR-0020): if the DB backup is >8d stale OR WAL lag
exceeds threshold, status flips and the registrar/dispatch REFUSE loudly. STAGE 2.
"""

from __future__ import annotations
