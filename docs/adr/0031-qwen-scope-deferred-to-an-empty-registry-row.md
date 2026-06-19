### ADR-0031 — Qwen-Scope deferred to an empty registry row
**Status:** Reserved-seam · **Source:** phase2 E7.
**Decision.** Consolidate SAE-era work on Gemma-2/3 + Llama-3.1-8B (fully tooled, permissive). Qwen-Scope is a registry row with `loader_format` recorded; the bespoke `.pt` loader is built when a Qwen experiment is real.
**Consequences.** `assets.loader_format` is mandatory day-one precisely so this row can exist inert (Qwen-Scope is non-SAELens `.pt`).
