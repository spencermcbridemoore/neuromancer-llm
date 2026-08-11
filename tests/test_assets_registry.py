"""Unit 1 — the assets first-writer (`Repository.register_asset` + `registry/assets.py`).

The FIRST writer into the reserved `neuro.assets` table (ADR-0031 "Qwen-Scope is a registry row with
loader_format recorded"; ADR-0032 accommodates the local-release nulls).

The guards are MODULE-LEVEL pure functions, so their probes need no database — a Repository cannot be
constructed without a live, identity-verified engine, and tiering a guard probe as non-pg while routing it
through a Repository would make it unreachable.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest
from sqlalchemy import text

from neuromancer_llm.db.identity import sha256_bytes
from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.db.repository import (
    IdentityMismatchError,
    Repository,
    require_asset_coordinates,
    require_asset_sha256,
)
from neuromancer_llm.registry.assets import QWEN_SCOPE_LAYER18, AssetSpec

_OK = {"asset_key": "k", "asset_type": "sae", "loader_format": "qwen_scope_pt"}
_DIGEST = sha256_bytes(b"the-sae-bytes")


# --- the registry vocabulary (registry/assets.py holds the spec; Repository holds the write) -------


def test_qwen_constant_carries_the_owner_ruled_key_and_the_adr0031_fields() -> None:
    """The asset_key is a COMMITTED CONSTANT, not runbook prose: `assets.asset_key` is NOT NULL UNIQUE and
    there is no delete verb, so pinning the exact string here is what makes the one-way door a mechanism."""
    assert QWEN_SCOPE_LAYER18.asset_key == "qwen/sae-res-qwen3-8b-base-w64k-l0_100/layer18"
    assert QWEN_SCOPE_LAYER18.asset_type == "sae"
    assert QWEN_SCOPE_LAYER18.loader_format == "qwen_scope_pt"  # ADR-0031: mandatory day-one
    assert QWEN_SCOPE_LAYER18.hf_repo == "Qwen/SAE-Res-Qwen3-8B-Base-W64K-L0_100"
    # NOT ESTABLISHABLE IN-REPO, and ADR-0032 does NOT sanction this null (that accommodation is hf_repo's,
    # for locally-trained releases). The frozen DDL calls hf_revision durable identity, so this is a
    # deliberate PERMANENT partial identity — left NULL rather than invented.
    assert QWEN_SCOPE_LAYER18.hf_revision is None


def test_asset_spec_is_frozen() -> None:
    """The module's central argument — that the key is a COMMITTED CONSTANT rather than a mutable value a
    caller can rewrite at runtime — rests on this, and it was previously unprobed."""
    with pytest.raises(Exception):  # noqa: B017 — dataclasses raises FrozenInstanceError
        QWEN_SCOPE_LAYER18.asset_key = "something-else"  # type: ignore[misc]


def test_asset_spec_carries_no_sha256_field() -> None:
    """sha256 is a MATERIALIZED fact about bytes on a machine, streamed at registration — not a declarative
    property of the release. Putting it in the spec would invite a hardcoded digest in `src/`."""
    assert "sha256" not in AssetSpec.__dataclass_fields__


def test_registry_module_performs_no_io() -> None:
    """The house split: the registry module owns the VOCABULARY, `Repository` owns the WRITE.
    `registry/metric_keys.py` holds no INSERT either — it names one only in prose, pointing at its writer.

    ⚠ AST-BASED ON PURPOSE. A string scan for "INSERT"/"engine" REDDENS on this module's own docstring,
    which legitimately says "registrar/admin-only INSERT per grants.sql" exactly as metric_keys.py does —
    the mirror of the banked trap where a string scan is SATISFIED by a docstring. Prose must neither
    satisfy nor break a structural claim.

    ⚠ SCOPE: this is a TRIPWIRE, not a proof of no-I/O. It checks exactly two things — that `sqlalchemy` is
    not imported, and that no `.execute`/`.begin`/`.connect` attribute call appears — so I/O reached by any
    other name (a stdlib file read, an indirect import, a call bound to a local) passes it. It is sized to
    catch the drift that would actually happen here (someone adding the write to the vocabulary module),
    not to be exhaustive, and it is recorded as a tripwire so no reader mistakes it for coverage."""
    tree = ast.parse(pathlib.Path(inspect.getsourcefile(AssetSpec)).read_text(encoding="utf-8"))
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "sqlalchemy" not in imported, "the vocabulary module must not reach the database layer"
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & {"execute", "begin", "connect"}, "the vocabulary module performs no I/O"


# --- the fail-closed guards (module-level pure functions; no DB) -----------------------------------


@pytest.mark.parametrize("field", ["asset_key", "asset_type", "loader_format"])
@pytest.mark.parametrize("bad", ["", "   ", "\t", "\n"])
def test_blank_coordinate_is_refused_fail_closed(field: str, bad: str) -> None:
    """`assets` has no non-empty CHECK on any of the three NOT NULL columns and no delete verb, so a blank
    would mint a PERMANENT row — the hole wave-2 Phase 1 closed on `campaigns.campaign_key`."""
    with pytest.raises(ConfigurationError, match=field):
        require_asset_coordinates(**{**_OK, field: bad})


def test_valid_coordinates_pass() -> None:
    assert require_asset_coordinates(**_OK) is None


def test_sha256_rejects_a_str() -> None:
    with pytest.raises(ConfigurationError, match="never hex"):
        require_asset_sha256(_DIGEST.hex())  # type: ignore[arg-type]


def test_sha256_rejects_an_encoded_hex_digest() -> None:
    """THE HAZARD THE TYPE CHECK ALONE DOES NOT CLOSE: `.encode()` on a hex digest yields 64 bytes of ASCII,
    which IS bytes and passes any isinstance test, then lands as a permanent identity matching nothing."""
    encoded = _DIGEST.hex().encode()
    assert isinstance(encoded, bytes) and len(encoded) == 64  # the guard cannot lean on the type
    with pytest.raises(ConfigurationError, match="64 bytes"):
        require_asset_sha256(encoded)


@pytest.mark.parametrize("n", [0, 31, 33, 64])
def test_sha256_rejects_any_wrong_length(n: int) -> None:
    with pytest.raises(ConfigurationError, match="32-byte"):
        require_asset_sha256(b"x" * n)


def test_sha256_accepts_the_house_producers_output_and_none() -> None:
    assert require_asset_sha256(_DIGEST) is None  # db/identity.py::sha256_bytes -> .digest(), 32 bytes
    assert require_asset_sha256(None) is None  # nullable column; ADR-0031's row may precede its bytes


# --- the required/non-defaulted surface -----------------------------------------------------------


@pytest.mark.parametrize("param", ["asset_key", "asset_type", "loader_format"])
def test_the_three_not_null_params_are_required_and_non_defaulted(param: str) -> None:
    sig = inspect.signature(Repository.register_asset)
    assert sig.parameters[param].default is inspect.Parameter.empty
    assert sig.parameters[param].kind is inspect.Parameter.KEYWORD_ONLY


def test_register_asset_does_not_consult_the_durability_gate() -> None:
    """MODULE-SCOPED structural pin for the standing invariant "`assert_durability_ok` is on no `Repository`
    path". The gate is the pre-blob-write consult (ADR-0020); `register_asset` writes no bytes, opens no
    backend and holds no ImportBatchHandle. Repo-wide the invariant is prose plus five `importer/**` AST
    pins, so nothing else would catch a violation in THIS file. AST-based: a prose mention in a docstring
    must not satisfy it."""
    tree = ast.parse(pathlib.Path(inspect.getsourcefile(Repository)).read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "assert_durability_ok"
    ]
    assert calls == [], (
        "db/repository.py must NOT call assert_durability_ok — the ADR-0020 consult is once-per-batch at "
        "the importer choke point and pre-put at the two capture choke points; it is on NO Repository path."
    )


# --- pg: the write path ---------------------------------------------------------------------------


def _rows(repo: Repository) -> list[dict]:
    with repo.engine.connect() as conn:
        return [
            dict(r) for r in conn.execute(text("SELECT * FROM neuro.assets ORDER BY asset_id")).mappings()
        ]


@pytest.mark.pg
def test_first_register_writes_every_field_and_reads_back(repo) -> None:
    asset_id = repo.register_asset(
        asset_key=QWEN_SCOPE_LAYER18.asset_key,
        asset_type=QWEN_SCOPE_LAYER18.asset_type,
        loader_format=QWEN_SCOPE_LAYER18.loader_format,
        sha256=_DIGEST,
        hf_repo=QWEN_SCOPE_LAYER18.hf_repo,
        hf_revision=QWEN_SCOPE_LAYER18.hf_revision,
    )
    (row,) = _rows(repo)
    assert row["asset_id"] == asset_id
    assert row["asset_key"] == QWEN_SCOPE_LAYER18.asset_key
    assert row["asset_type"] == "sae"
    assert row["loader_format"] == "qwen_scope_pt"
    assert row["hf_repo"] == "Qwen/SAE-Res-Qwen3-8B-Base-W64K-L0_100"
    assert row["hf_revision"] is None
    # bytea round-trips as the 32 RAW digest bytes, never hex text.
    assert row["sha256"] == _DIGEST
    assert isinstance(row["sha256"], bytes) and len(row["sha256"]) == 32


@pytest.mark.pg
def test_identical_re_register_is_a_keep_first_no_op(repo) -> None:
    first = repo.register_asset(**_OK, sha256=_DIGEST, hf_repo="r", hf_revision="v1")
    again = repo.register_asset(**_OK, sha256=_DIGEST, hf_repo="r", hf_revision="v1")
    assert again == first
    assert len(_rows(repo)) == 1  # keep-first: no second row


@pytest.mark.pg
@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        ("asset_type", "sae", "transcoder"),
        ("loader_format", "qwen_scope_pt", "saelens"),
        ("hf_repo", "Qwen/A", "Qwen/B"),
        ("hf_revision", "abc", "def"),
    ],
)
def test_divergent_re_register_raises_per_field(repo, field: str, first: str, second: str) -> None:
    base = {**_OK, "hf_repo": "Qwen/A", "hf_revision": "abc"}
    repo.register_asset(**{**base, field: first})
    with pytest.raises(IdentityMismatchError, match=field):
        repo.register_asset(**{**base, field: second})


@pytest.mark.pg
def test_divergent_sha256_raises(repo) -> None:
    """The integrity case, and the one the UNIQUE placement makes reachable: the key names the row, so
    "right key, WRONG BYTES" lands on the conflict branch where this comparison catches it."""
    repo.register_asset(**_OK, sha256=_DIGEST)
    with pytest.raises(IdentityMismatchError, match="sha256"):
        repo.register_asset(**_OK, sha256=sha256_bytes(b"different-bytes"))


@pytest.mark.pg
@pytest.mark.parametrize(
    ("field", "value"),
    [("sha256", _DIGEST), ("hf_repo", "Qwen/A"), ("hf_revision", "abc")],
)
def test_backfilling_a_stored_null_raises_instead_of_returning_a_false_green(repo, field: str, value) -> None:
    """THE FAIL-OPEN THIS CLOSES. Under plain NULL-means-unspecified this call matches no drift, raises
    nothing and returns an id — while the column stays NULL FOREVER, because the API is INSERT-only and no
    UPDATE path for `assets` exists in src/. A green that means "nothing happened" where the caller reads
    "it worked" is the defect."""
    repo.register_asset(**_OK)  # every nullable column stored NULL
    with pytest.raises(IdentityMismatchError, match="INSERT-only"):
        repo.register_asset(**_OK, **{field: value})


@pytest.mark.pg
@pytest.mark.parametrize(
    ("field", "value"),
    [("sha256", _DIGEST), ("hf_repo", "Qwen/A"), ("hf_revision", "abc")],
)
def test_incoming_null_against_a_stored_value_keeps_first(repo, field: str, value) -> None:
    """THE MIRROR asymmetric-NULL case, decided explicitly and deliberately NOT a raise: the caller asserted
    nothing, which is genuinely "unspecified". Both directions are pinned, per the standing obligation."""
    first = repo.register_asset(**_OK, **{field: value})
    assert repo.register_asset(**_OK) == first
    (row,) = _rows(repo)
    assert row[field] == value  # the stored value survives; nothing was nulled out


@pytest.mark.pg
def test_asset_id_is_de_blinded_against_other_tables_and_never_assumed_contiguous(repo) -> None:
    """RESTART IDENTITY makes the first row of every table pk=1, so a probe binding an id to a role can go
    blind. ⚠ ONE DECOY PER TABLE IS NOT ENOUGH — decoys in different tables re-collide at the next integer,
    which is exactly how the FIRST cut of this probe went blind (actor_id == campaign_id == first == 1). The
    decoy COUNTS must DIFFER PER TABLE: 1 actor / 2 campaigns / 3 decoy assets, so the row under test lands
    on 4 and every id is distinct BY CONSTRUCTION.

    ⚠ And `ON CONFLICT DO NOTHING` still BURNS an identity value, so asset_ids are NOT contiguous — assert
    ordering and distinctness, never id arithmetic that assumes a gapless sequence."""
    actor_id = repo.create_actor("a1")  # 1 actor -> actor_id == 1
    repo.create_campaign("c1", actor_id)
    campaign_id = repo.create_campaign("c2", actor_id)  # 2 campaigns -> campaign_id == 2
    for i in range(3):  # 3 decoy assets -> the row under test lands on 4
        repo.register_asset(asset_key=f"decoy{i}", asset_type="sae", loader_format="lf")

    first = repo.register_asset(asset_key="k1", asset_type="sae", loader_format="lf")
    repo.register_asset(asset_key="k1", asset_type="sae", loader_format="lf")  # conflict: burns an id
    second = repo.register_asset(asset_key="k2", asset_type="sae", loader_format="lf")

    assert {actor_id, campaign_id} == {1, 2}, "decoy counts must differ per table"
    assert len({first, second, actor_id, campaign_id}) == 4, "ids must be pairwise distinct"
    assert second > first + 1, "the conflicting INSERT must have burned an identity value"
    assert [r["asset_key"] for r in _rows(repo)] == ["decoy0", "decoy1", "decoy2", "k1", "k2"]


@pytest.mark.pg
@pytest.mark.parametrize("field", ["asset_key", "asset_type", "loader_format"])
def test_a_blank_coordinate_never_reaches_the_database(repo, field: str) -> None:
    """Pins the WIRING, not the guard. `require_asset_coordinates` is probed directly above as a pure
    function; this asserts `register_asset` actually FORWARDS all three coordinates to it. Previously only
    `asset_key` was covered, so dropping either of the other two from the call would have shipped green."""
    with pytest.raises(ConfigurationError):
        repo.register_asset(**{**_OK, field: "  "})
    assert _rows(repo) == []


@pytest.mark.pg
def test_an_encoded_hex_digest_never_reaches_the_database(repo) -> None:
    """Pins that `register_asset` calls `require_asset_sha256` AT ALL. `assets.sha256` is bytea with no
    length constraint, so without the call these 64 ASCII bytes land as a PERMANENT identity matching
    nothing — the guard is probed as a pure function above, but its wiring was unprobed."""
    with pytest.raises(ConfigurationError):
        repo.register_asset(**_OK, sha256=_DIGEST.hex().encode())
    assert _rows(repo) == []


@pytest.mark.pg
def test_one_file_under_two_keys_mints_two_honest_rows_and_is_deliberately_not_a_raise(repo) -> None:
    """THE E-8 ADJUDICATION, PINNED — the mirror of the divergent-sha256 probe. Idempotency keys on
    `asset_key` ALONE: the UNIQUE sits on the caller-chosen LABEL, so two honest keys naming one file are
    two honest rows, and raising would be FALSE-LOUD. Porting the tokenizer label-collision guard would
    invert this AND block the module's own sanctioned remedy — both raise paths tell a caller to register
    under a new asset_key, the only correction available on a table with no delete verb.

    ⚠ A REGRESSION fixture, not a structural bar: nothing in the schema stops a future author adding that
    guard, which is exactly why the decision needs a probe rather than a paragraph (precedent 20)."""
    a = repo.register_asset(asset_key="k1", asset_type="sae", loader_format="lf", sha256=_DIGEST)
    b = repo.register_asset(asset_key="k2", asset_type="sae", loader_format="lf", sha256=_DIGEST)
    assert a != b
    rows = _rows(repo)
    assert [r["asset_key"] for r in rows] == ["k1", "k2"]
    assert [r["sha256"] for r in rows] == [_DIGEST, _DIGEST]  # ONE file, TWO honest rows


@pytest.mark.pg
def test_conflict_branch_adjudicates_the_row_the_key_names_in_a_multi_row_table(repo) -> None:
    """THE KEEP-FIRST BRANCH, PINNED AGAINST THE WRONG ROW. Every other conflict probe runs with a
    single-row table, where a re-SELECT that ignored its WHERE clause would still fetch the right row and
    ship green — MEASURED: a `... OR true ORDER BY asset_id LIMIT 1` mutation survived the whole file.

    ⚠ The assertion must match the STORED VALUE from the correct row, not the field NAME: a wrong-row
    lookup raises IdentityMismatchError naming the same field either way, so matching on the name is not
    discriminating."""
    for i in range(3):  # decoys sit AHEAD in asset_id order carrying DIFFERENT non-NULL values
        repo.register_asset(
            asset_key=f"decoy{i}",
            asset_type="transcoder",
            loader_format="saelens",
            hf_repo="Qwen/DECOY",
            hf_revision="decoy-rev",
        )
    target = repo.register_asset(**_OK, hf_repo="Qwen/A", hf_revision="abc")

    # keep-first must return the TARGET's id, not a decoy's
    assert repo.register_asset(**_OK, hf_repo="Qwen/A", hf_revision="abc") == target
    # and the drift raise must quote the TARGET's stored value, not a decoy's
    with pytest.raises(IdentityMismatchError, match="Qwen/A"):
        repo.register_asset(**_OK, hf_repo="Qwen/B", hf_revision="abc")
