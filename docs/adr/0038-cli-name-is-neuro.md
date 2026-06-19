### ADR-0038 — CLI name is `neuro`
**Status:** Accepted · **Source:** phase2 E16.
**Decision.** `neuro` is the single console entrypoint in `[project.scripts]`, runbooks, systemd/scheduler units, generated docs, and CI smoke commands.
**Consequences.** Footnote: legacy PyPI `neuro-cli` historically claimed the `neuro` command — PATH collision only if that tool is ever installed alongside.
