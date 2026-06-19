"""Ad-hoc capture: auto-mint + label-later (ADR-0036). STAGE 2.

Every uncontexted call auto-mints into an `adhoc` run (closes the repository=None bypass); rows are
flagged is_unlabeled, the preflight banner counts them, and `neuro runs adopt` labels them retroactively
(the assign-once trigger guards the NULL->value fingerprint adoption).
"""

from __future__ import annotations
