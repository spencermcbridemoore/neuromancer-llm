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
column, but NOTHING IN THIS REPOSITORY ESTABLISHES THE IDENTIFICATION: the release id appears only in this
module and its test, and no ADR, manifest or registry here links it to "Qwen-Scope". ``loader_format`` below
therefore records the MEASURED format fact; the identification stays an inference, stated as one.
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
#: hf_revision is left NULL because NO IN-REPO ARTIFACT ESTABLISHES IT — the corpus manifest records the
#: HF repo and no revision. ⚠ NO ADR SANCTIONS THIS NULL: ADR-0032's accommodation is for `hf_repo` on
#: LOCALLY-TRAINED releases, and this is a vendor release; ADR-0032 is SILENT on hf_revision. And the
#: frozen DDL's own header for this table — "sha256 + hf_revision are durable identity" — names this
#: column IDENTITY. So registering without it is a deliberate, PERMANENT decision to hold the first-ever
#: assets row at PARTIAL IDENTITY, not a neutral omission.
#: ⚠ That NULL is PERMANENT: `register_asset` is INSERT-only and REFUSES to backfill a NULL
#: rather than silently no-op, so establishing the revision later needs an admin UPDATE, not a re-register.
#: `assets` has no `note` column (nine columns; the frozen DDL), so THIS COMMENT is the only in-repo home
#: for that reason today — a real cost of the design, recorded rather than glossed. (The corpus-import
#: runbook that will carry it at execution time is a LATER unit's deliverable and does not exist yet;
#: naming it here as an existing home would be a forward reference to nothing.)
QWEN_SCOPE_LAYER18 = AssetSpec(
    asset_key="qwen/sae-res-qwen3-8b-base-w64k-l0_100/layer18",
    asset_type="sae",
    loader_format="qwen_scope_pt",
    hf_repo="Qwen/SAE-Res-Qwen3-8B-Base-W64K-L0_100",
    hf_revision=None,
)
