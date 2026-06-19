### ADR-0024 — CI / branch-protection precondition (condition 13 + E14)
**Status:** Accepted. **Decision.** The repository is **public** — satisfying the governance binding's precondition (rulesets + branch protection enforceable, unlimited Actions minutes). Exam data never lives in git by design; the exam-text soft rule is untouched. **Consequences.** A repository ruleset on `main` requires PR + required status checks {fast, tests-full, wheel-smoke, docker}, blocks force-push, restricts deletion; required reviews stay OFF (solo + agents).

---
