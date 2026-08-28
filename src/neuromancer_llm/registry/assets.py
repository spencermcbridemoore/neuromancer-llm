"""assets registry (SAE / steering / transcoder) — loader_format MANDATORY (ADR-0031); local-release
case has no hf_repo (ADR-0032). Qwen-Scope is an inert registry row until a Qwen experiment is real. STAGE 2.

Register these through ``Repository.register_asset`` (keep-first on ``asset_key``, fail-loud on a divergent
re-register; registrar/admin-only INSERT per grants.sql). This module holds the VOCABULARY only and performs
no I/O, following ``registry/metric_keys.py`` + ``Repository.seed_metric_key``. ⚠ Stated precisely, because
it is easy to overclaim: that is ONE prior vocabulary-module instance, not a universal house pattern.
``registry/backends.py`` corroborates only the weaker half — it holds no INSERT either, and its write is
``Repository.get_or_create_storage_backend`` — but it is a substantial logic module, not a spec module, so
it is evidence that the WRITE lives on ``Repository``, not that every registry gets a spec module.

WHY THE KEY IS A CONSTANT HERE AND NOT A STRING IN A RUNBOOK. ``assets.asset_key`` is ``text NOT NULL
UNIQUE`` and there is NO delete verb for assets anywhere in ``src/`` (the only DELETE is ``bundles/gc.py``,
on unsealed bundles), so the first value written is permanent. Committing it as a reviewed constant makes
the one-way door a MECHANISM rather than a typing-accuracy hope at execution time.

⚠ ADR-0031 says "Qwen-Scope"; the artifact below is ``Qwen/SAE-Res-Qwen3-8B-Base-W64K-L0_100``. That these
name the same release family is a STRONG INFERENCE, NOT AN IN-REPO MEASUREMENT — the vendor, the base model
and the non-SAELens ``.pt`` format all agree, and ``qwen_scope_pt`` is the frozen DDL's own example for this
column, but NOTHING IN THIS REPOSITORY ESTABLISHES THE IDENTIFICATION: no ADR, manifest or registry here
links the release id to the name "Qwen-Scope". ``loader_format`` below therefore records the MEASURED format
fact; the identification stays an inference, stated as one.

⚠ CORRECTED 2026-08-28: this paragraph used to support that conclusion with "the release id appears only in
this module and its test", which is FALSE at HEAD — `ops/corpus-import/import_manifest.csv`,
`part_01_mi_core.csv` and `ops/runbook-corpus-import.md` all carry it, the manifest row even noting the local
file is a copy of that HF repo. The CONCLUSION is unaffected (none of them uses the name "Qwen-Scope", so
none links the two), but the enumeration was a checkable falsehood and is replaced by the claim it was
supposed to support. ★ EXTERNAL corroboration now exists and is worth knowing while changing nothing here:
the vendor's own Hub metadata tags the repo `qwen-scope`. That is still not an in-repo fact.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetSpec:
    """One row of the assets registry: an SAE / steering vector / transcoder / probe.

    ``sha256`` is deliberately ABSENT from this spec. It is a MATERIALIZED fact about bytes on a particular
    machine, streamed at registration time and passed to ``Repository.register_asset`` by the caller — not a
    declarative property of the release, which is all this vocabulary records.
    """

    asset_key: str
    asset_type: str  # 'sae' | 'steering_vector' | 'transcoder' | 'probe' (a DDL COMMENT, not a PG enum)
    loader_format: str  # MANDATORY day-one (ADR-0031) precisely so an inert row can exist
    hf_repo: str | None = None  # NULL for locally-trained releases (ADR-0032)
    hf_revision: str | None = None


#: The Qwen-Scope layer-18 SAE — ADR-0031's own worked example ("a registry row with loader_format
#: recorded"), registered inert: the bespoke `.pt` loader stays DEFERRED until a Qwen experiment is real,
#: and writing this row does NOT flip ADR-0031's Reserved-seam status (the seam is the LOADER, not the row).
#:
#: ★ hf_revision IS ESTABLISHED, SO THIS ROW IS FULL IDENTITY — and HOW it was established is recorded
#: here because the value alone cannot carry it (2026-08-28; the row was written to canonical 2026-08-27).
#:
#: THE METHOD. The `layer18.sae.pt` file was streamed locally to a sha256, and that digest was matched
#: against the HF repo's LFS object ids. It returned a UNIQUE hit among the repo's 36 LFS files.
#: ⚠ WHY THAT IS DECISIVE, and it is not obvious: layers 10-35 ALL SHARE the byte count 2,147,764,603, so
#: SIZE DISCRIMINATES NOTHING and only the digest identifies which layer's file this is.
#:
#: OWNER-RULED, and what it was ruled AGAINST. The commit chosen is the one at which the content was
#: ESTABLISHED. The equally-true current-`main` commit (`f308c1df…27bd`, elided in the record and NOT
#: recoverable from this repo) was declined, as was NULL: the narrowest measured claim was preferred
#: because it is invariant to future unrelated commits to that repo.
#: ⚠ THE HONEST LIMIT: neither candidate establishes WHICH REVISION WAS DOWNLOADED. The claim is about
#: where these bytes are found, not about the provenance of the local copy.
#: ⚠ The oid was fetched with `curl.exe` and compared IN SCRIPT — a 64-char hex transcribed by a
#: summarising model is not a measurement, and this field is permanent and unbackfillable.
#: ⚠ EVERY step above is an OUT-OF-REPO measurement, in the idiom of the identification note in this
#: module's docstring: nothing here re-checks it. The one half that IS in-repo checkable is the byte count,
#: recorded in `ops/corpus-import/import_manifest.csv`.
#:
#: ⚠ PERMANENCE GOVERNS EVERY FUTURE AssetSpec ADDED HERE, not just this one: `register_asset` is
#: INSERT-only and REFUSES to backfill a NULL rather than silently no-op, so a field left NULL at the
#: first write needs an admin UPDATE, never a re-register. Establish it BEFORE writing or accept it
#: permanently. `assets` has no `note` column (nine columns; the frozen DDL), so this comment is the
#: DURABLE home for that reasoning; `ops/runbook-corpus-import.md` §7 carries the operational half.
QWEN_SCOPE_LAYER18 = AssetSpec(
    asset_key="qwen/sae-res-qwen3-8b-base-w64k-l0_100/layer18",
    asset_type="sae",
    loader_format="qwen_scope_pt",
    hf_repo="Qwen/SAE-Res-Qwen3-8B-Base-W64K-L0_100",
    hf_revision="f7addb7d4ac77ff30a59503916b4cf5636e6f881",
)
