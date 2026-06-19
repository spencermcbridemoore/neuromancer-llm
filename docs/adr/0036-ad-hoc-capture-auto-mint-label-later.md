### ADR-0036 — Ad-hoc capture: auto-mint + label-later
**Status:** Accepted · **Source:** phase2 E12.
**Decision.** `capture_events.run_id` is NOT NULL; every uncontexted call auto-mints into an `adhoc` session run (closes the `repository=None` bypass). Adhoc rows are flagged `unlabeled`; the preflight banner counts them; `neuro runs adopt` retroactively labels them.
**Consequences.** The engagement's answer to the "interactive inline lane grows via ad-hoc auto-minting" rot-watch — completeness without ceremony, with a visible nag and a cheap fix.
