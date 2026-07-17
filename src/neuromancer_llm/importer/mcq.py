"""Importer rank 8b — LAYER 2: the MCQ CASCADE MAPPER (phase3-importer-spec.md; ADR-0023; ADR-0047; D1/D2/D3).

Maps the MOSART MCQ stimulus corpus into the native stimulus family: mirrors every question as a Layer-1
`external_records` row, builds `prompt_sets -> prompt_items -> mcq_items -> mcq_options`, and calls rank 8a to
record each promotion. First producer of all four stimulus-family tables.

THE CORPUS (read first-hand 2026-07-16; the survey of record is scratch/importer-corpus-survey-2026-07-16.md):
three parquet tables — `questions` (360x30), `question_options` (1800x6 = 360x5 exactly), `documents` (16x6). It is
NOT in the predecessor's database and NOT in its repo: it belongs to a THIRD system (MOSART) and arrives as loose
files, hence `source_system='local-fs'` (owner-ruled). It is well-formed, and that is why this module invents
nothing: `(document_id, item_num)` is unique 360/360, `item_num` is dense 1..N in all 16 documents, `question_id`
is globally unique, and every question has exactly 5 options.

============================ THE TWO GRAINS, AND WHY THEY MUST MATCH ============================

`prompt_sets.content_hash` is `bytea NOT NULL UNIQUE` = content-hash identity. OWNER-RULED (2026-07-16):
**ONE PROMPT SET PER DOCUMENT** — `content_hash` over a canonical manifest of that document's ordered items;
`item_ordinal` = the corpus's own `item_num`. A document IS a test (`documents.n_items_in_test`).

Set identity is therefore CONTENT-ADDRESSED and IMMUTABLE: edit one question and the document mints a NEW set,
with new `prompt_items`/`mcq_items` for every item including unchanged ones. That is correct — a run that consumed
a set must forever be able to say what the set contained — and the re-insert is the price of content-addressed
identity.

**BUT `promotions` is `UNIQUE (external_record_id, promoted_kind)` (phase3-ddl.sql:632) and rank 8a raises
UNCONDITIONALLY on `promoted_pk` drift (promote.py:326-333): ONE record binds to ONE native row, FOREVER.** So if a
record were addressed on the question's OWN bytes, an UNCHANGED question in an EDITED document would keep-first to
its existing record — already bound to the OLD set's `mcq_item` — and its promotion would RAISE, *after* the new
set/item/mcq_item/options had already committed (rank 5 and rank 8a each open their own transaction, so no
enclosing transaction exists). That is a one-way wedge plus orphan native rows carrying no promotion and no
`promoted_from` edge — the D1-confidentiality-recovery path (lineage.py:16) — reading CLEAN forever.

**OWNER-RULED (2026-07-17), option (B): THE RECORD GRAIN IS SET-SCOPED.** A record is not "a question"; it is
*"this question as it appeared in THIS VERSION of this document"*, so its canonical bytes carry the set's
`content_hash`. An edited document mints new records AND new promotions — no collision, and the cascade is fully
re-runnable (the binding phase0 constraint "importer re-runnable"). Immutability is applied CONSISTENTLY at both
layers; the wedge existed precisely because it was applied at one. Rejected: set-per-question (discards the
documents and ordinals the corpus gives us) and record-per-document promoted to a `prompt_set` (cleanest, but
amends the shipped rank 8a).
ACCEPTED COST, stated rather than hidden: a question re-mirrors when its document changes, and `payload_text`
carries one derived field (`set_content_hash`). `payload_text` is ALREADY a canonical serialization — a parquet row
has no byte form of its own — so this widens an existing rendering rather than corrupting a byte-exact mirror.

============================ LAYER 1 IS FAITHFUL; ONLY LAYER 2 IS SELECTIVE ============================

**EVERY question is mirrored** (all 360), including the ~58 this module refuses to promote. Only the CASCADE is
selective. Mirroring solely the promotable ones would invert the two-layer architecture and let 58 questions vanish
with no trace at all — a gap that reads clean. A refusal is returned in `McqMapResult.refusals` with its
`question_id` and reason, and it leaves an honest GAP in the ordinal sequence (`item_ordinal` = `item_num`, so a
missing ordinal is EVIDENCE; a re-minted 1..N would hide it). `UNIQUE(prompt_set_id, item_ordinal)` does not
require density.

Refusals are EXCLUDED from the manifest preimage, so a filter change legitimately mints a new set — and under the
ruled record grain that re-mirrors the document's questions too, which is the same price, paid consistently.

============================ IDEMPOTENCY — MIXED, AND NOT UNIFORM ============================

  * `prompt_sets` (key `content_hash`)            keep-first, NO drift guard — the key IS the content. The four
    out-of-key columns are `set_kind` (always 'imported'), `derived_from_run_id` (always NULL — this module
    touches no run, so the ADR-0048 layer-(b) watch-point cannot fire), `note` (NULL) and `created_at`.
  * `prompt_items` (key `(prompt_set_id, item_ordinal)`)  keep-first, NO drift guard — drift is STRUCTURALLY
    IMPOSSIBLE: the manifest covers exactly the ordered `(item_ordinal, prompt_text)` pairs, so a changed stem
    yields a different `content_hash` and therefore a DIFFERENT set. This is a derived fact, not an omission; a
    guard here would be an untestable no-RED-on-revert branch.
  * `mcq_items` (key `prompt_item_id`)            keep-first + RAISE-ON-DRIFT on `correct_letter`/`num_options` —
    genuinely REACHABLE: the manifest covers stems ONLY, so a corpus whose `correct_answer` changed while its stem
    did not yields the SAME hash, SAME set, SAME prompt_item and a CONTRADICTORY answer key.
  * `mcq_options` (key `(mcq_item_id, letter)`)   keep-first + RAISE-ON-DRIFT on `option_text` — same reachability.

Each drift guard compares ONLY the columns that genuinely contradict. Do NOT conjoin conditions across columns:
an extra conjunct is the `db/repository.py:667-670` post-mortem shape, where a guard went fail-open and a bad row
was silently adopted.

============================ THE TWO ANSWER-KEY SOURCES ============================

The corpus states the answer key TWICE: `questions.correct_answer` (a scalar) and `question_options.is_correct` (a
flag on each of the 5 option rows). Neither is checked by the schema — `mcq_items.correct_letter` and
`mcq_options.letter` are bare `text NOT NULL` with NO CHECK and NO FK between them, so nothing in the database can
catch an answer key naming an option that does not exist. Code is the only mechanism, so this module:
  * RAISES if the two sources CONTRADICT (more than one `is_correct`, or one that differs from `correct_answer`) —
    a corpus contradiction is not something to silently pick a winner from;
  * ADOPTS `correct_answer` when no option carries the flag (NULL means UNSPECIFIED — `repository.py:423` — NOT
    `repository.py:671`'s NULL-means-UNVERIFIABLE, which refuses);
  * REFUSES the question unless `correct_answer` names one of the letters actually written for it.
Measured over the real corpus at build time: 350 agree, 0 contradict, 0 unflagged, 10 with a NULL `correct_answer`
(refused). Both sources are lowercase `a`-`e`.

Durability is NOT re-consulted here — the rank-4 gate `open_import_batch` consulted `assert_durability_ok` ONCE per
batch (readiness section 4.3); a per-row consult is forbidden and structurally pinned
(tests/redteam/test_rt_importer_no_cloud_put.py). The promotion method is registered ONCE PER BATCH by the caller
and passed in (ruling C); this module never mints one.

NOT here: `mcq_permutations` (DEFERRED with the runs — VERIFIED: the stimulus corpus records no permutations; they
exist only in run output as a deterministic function of a strategy, so writing them would materialize a computable
enumeration — the ADR-0023 reserve-until-consumed shape); `stimulus_structures` (ADR-0023); `mcq_responses` (NEVER,
ADR-0011/C1); the corpus file's own artifact registration and its lineage edge (rank 6/7 — the file's sha256 rides in
each record's `payload_jsonb` sidecar instead, which is the ONLY place the source file is named); the psychometrics (`percent_correct`, `incorrect_*`), `commentary`, the
`misconceptions` subsystem, `standard`/`grade_band`/`topic`/`year`. Every one of those EXCEPT the misconceptions
subsystem is nonetheless MIRRORED byte-for-byte in Layer 1: `_canonical_question_bytes` carries the WHOLE question
row and the WHOLE of each option row, so "not promoted" never means "not preserved". The three `misconceptions`
tables are a separate subsystem this module does not read, so they are genuinely NOT mirrored — stated plainly
rather than left to imply that Layer 1 covers them. No CLI (`neuro importer promote` stays a Stage-2 stub — ranks 5/6/7/8a all deferred it identically).
No cloud writes of any kind (D3). PRECONDITION: part of the admin-DSN importer flow.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from ..db.identity import canonical_bytes, content_hash
from ..db.repository import IdentityMismatchError
from .external_records import write_external_record
from .ingress import ImportBatchHandle, ImporterIngressError
from .promote import PromotedKind, PromotionMethod, promote_external_record

if TYPE_CHECKING:
    from ..db.repository import Repository

# The D2 `source_table` for this corpus (the corpus kind) and the `payload_jsonb` `kind` key. The kind key is
# LOAD-BEARING: `external_records_kind_idx ON external_records ((payload_jsonb ->> 'kind'))` (phase3-ddl.sql:623) is
# the ONE functional JSON index the bindings permit, and nothing has ever populated the key it indexes.
MCQ_SOURCE_TABLE = "mosart_questions"
MCQ_RECORD_KIND = "mcq_question"

# The manifest scheme, IN the preimage: a future non-MCQ mapper minting sets under a different rule cannot silently
# collide with these, and a reader can tell which rule to re-verify a hash under.
PROMPT_SET_SCHEME = "neuro.mcq.prompt_set.v1"
PROMPT_SET_KIND = (
    "imported"  # the `set_kind` vocabulary is un-CHECKed text; its values live only in a DDL comment
)

# The corpus's own option arity, verified: exactly 5 options for all 360 questions, letters a-e, 360 each.
_OPTION_LETTERS = ("a", "b", "c", "d", "e")

_QUESTION_COLUMNS = (
    "question_id",
    "document_id",
    "item_num",
    "stem",
    "correct_answer",
    "has_picture",
    "options_are_pictures",
    "phantom_suspected",
    *(f"opt_{letter}_text" for letter in _OPTION_LETTERS),
)
_OPTION_COLUMNS = ("question_id", "letter", "option_text", "is_correct")
_DOCUMENT_COLUMNS = ("document_id",)

_PROMPT_INLINE_CAP = 8192  # prompt_items' `prompt_inline_cap` CHECK (phase3-ddl.sql:503)

_SET_INSERT = text(
    "INSERT INTO neuro.prompt_sets (content_hash, set_kind) VALUES (:ch, :sk) "
    "ON CONFLICT (content_hash) DO NOTHING RETURNING prompt_set_id"
)
_SET_RESELECT = text("SELECT prompt_set_id FROM neuro.prompt_sets WHERE content_hash = :ch")

_ITEM_INSERT = text(
    "INSERT INTO neuro.prompt_items (prompt_set_id, item_ordinal, prompt_text) VALUES (:ps, :ord, :txt) "
    "ON CONFLICT (prompt_set_id, item_ordinal) DO NOTHING RETURNING prompt_item_id"
)
_ITEM_RESELECT = text(
    "SELECT prompt_item_id FROM neuro.prompt_items WHERE prompt_set_id = :ps AND item_ordinal = :ord"
)

_MCQ_INSERT = text(
    "INSERT INTO neuro.mcq_items (prompt_item_id, correct_letter, num_options) VALUES (:pi, :cl, :no) "
    "ON CONFLICT (prompt_item_id) DO NOTHING RETURNING mcq_item_id"
)
_MCQ_RESELECT = text(
    "SELECT mcq_item_id, correct_letter, num_options FROM neuro.mcq_items WHERE prompt_item_id = :pi"
)

_OPTION_INSERT = text(
    "INSERT INTO neuro.mcq_options (mcq_item_id, letter, option_text) VALUES (:mi, :l, :ot) "
    "ON CONFLICT (mcq_item_id, letter) DO NOTHING RETURNING mcq_option_id"
)
_OPTION_RESELECT = text("SELECT option_text FROM neuro.mcq_options WHERE mcq_item_id = :mi AND letter = :l")


@dataclass(frozen=True)
class McqItemRefusal:
    """One question this module declined to PROMOTE. It is still mirrored in Layer 1 (see the module docstring)."""

    question_id: str
    reason: str


@dataclass(frozen=True)
class McqMapResult:
    """The outcome of one corpus mapping.

    EVERY count is the number of rows THIS CALL INSERTED — never rows present, never echoed from the input. A
    re-run over an unchanged corpus therefore returns all-zero counts and the identical refusals. (Stated because
    a result dataclass reporting the caller's intent rather than the database's state is a defect this engagement
    has shipped before.)
    """

    prompt_sets: int
    prompt_items: int
    mcq_items: int
    mcq_options: int
    external_records: int
    promotions: int
    refusals: tuple[McqItemRefusal, ...]


def _require_columns(rows: list[dict[str, Any]], required: tuple[str, ...], what: str) -> None:
    """Fail closed on corpus shape BEFORE any write. A subset check: extra columns are tolerated (the corpus is a
    third party's and may grow), a missing one is refused."""
    if not rows:
        raise ImporterIngressError(
            f"{what} is empty — refusing (fail closed; the corpus must not be silently skipped)."
        )
    missing = [c for c in required if c not in rows[0]]
    if missing:
        raise ImporterIngressError(
            f"{what} is missing required column(s) {missing} — refusing (fail closed). Present: {sorted(rows[0])}."
        )


def _read_corpus(corpus_root: Path) -> tuple[list[dict], list[dict], list[dict], str]:
    """Read the three corpus tables and hash the stimulus file. pyarrow only — pandas is not a dependency and must
    not become one. Returns `(questions, option_rows, documents, corpus_sha256)`."""
    import pyarrow.parquet as pq  # local: keeps the module importable where pyarrow is absent

    if not isinstance(corpus_root, Path):
        raise ImporterIngressError(
            f"corpus_root must be a Path, got {corpus_root!r} — refusing (fail closed)."
        )
    tables = {}
    for name in ("questions", "question_options", "documents"):
        path = corpus_root / f"{name}.parquet"
        if not path.is_file():
            raise ImporterIngressError(
                f"corpus table {name}.parquet not found under {corpus_root} — refusing (fail closed). The MCQ "
                "corpus is three parquet tables; do not point this at a derived export or a test fixture."
            )
        tables[name] = pq.read_table(path).to_pylist()
    _require_columns(tables["questions"], _QUESTION_COLUMNS, "questions.parquet")
    _require_columns(tables["question_options"], _OPTION_COLUMNS, "question_options.parquet")
    _require_columns(tables["documents"], _DOCUMENT_COLUMNS, "documents.parquet")

    # THE CORPUS INVARIANTS THE DESIGN RESTS ON — verified at RUNTIME rather than trusted. `item_ordinal` is the
    # corpus's own `item_num` ONLY because `(document_id, item_num)` is unique (measured 360/360). A duplicate
    # would hash a manifest of TWO items while writing ONE prompt_item — the set's hash would then cover an item
    # the set does not contain, which is the collision §2.2 exists to prevent — and the second question would
    # silently take the first's prompt_item_id, firing the drift guard against the WRONG object with a false
    # diagnosis. The module docstring calls these invariants load-bearing and `_require_columns` anticipates a
    # third-party corpus that "may grow", so they are checked, not assumed.
    for label, key in (
        ("(document_id, item_num)", lambda q: (q["document_id"], q["item_num"])),
        ("question_id", lambda q: q["question_id"]),
    ):
        counts: dict = {}
        for q in tables["questions"]:
            counts[key(q)] = counts.get(key(q), 0) + 1
        dupes = sorted(str(k) for k, n in counts.items() if n > 1)
        if dupes:
            raise ImporterIngressError(
                f"questions.parquet has duplicate {label}: {dupes[:5]} — refusing (fail closed). The item_ordinal "
                "binding and the set manifest both require uniqueness; a duplicate would silently merge two "
                "questions onto one prompt_item."
            )

    corpus_sha256 = hashlib.sha256((corpus_root / "questions.parquet").read_bytes()).hexdigest()
    return tables["questions"], tables["question_options"], tables["documents"], corpus_sha256


def _resolve_answer_letter(
    question_id: str, correct_answer: str | None, options: dict[str, dict]
) -> str | None:
    """Reconcile the corpus's TWO answer-key sources (see the module docstring). Returns the letter, or None if the
    question carries no usable key (a refusal, not a raise). RAISES only on a genuine CONTRADICTION."""
    flagged = sorted(letter for letter, row in options.items() if row.get("is_correct") is True)
    if len(flagged) > 1:
        raise ImporterIngressError(
            f"question {question_id!r} flags {flagged} as correct in question_options.is_correct — a single-answer "
            "MCQ cannot have several right answers; refusing the whole mapping (fail loud, corpus contradiction)."
        )
    if correct_answer is None:
        # No scalar key. A lone is_correct flag is not adopted as the key: correct_answer is the authoritative
        # column, and inventing a key from a flag the corpus left unstated is exactly the invention D5 refuses.
        return None
    if flagged and flagged[0] != correct_answer:
        raise ImporterIngressError(
            f"question {question_id!r}: questions.correct_answer={correct_answer!r} CONTRADICTS "
            f"question_options.is_correct={flagged[0]!r} — refusing to pick a winner between two disagreeing "
            "statements of the same fact (fail loud, corpus contradiction)."
        )
    return correct_answer


def _classify(question: dict, options: dict[str, dict]) -> str | None:
    """Return a refusal reason, or None if the question is promotable. Every reason names a NOT NULL / CHECK the
    question would violate, or a corpus flag saying the stimulus is not honestly representable as text."""
    if question["stem"] is None:
        return "stem is NULL (prompt_items.prompt_text would violate prompt_has_payload)"
    if len(question["stem"].encode("utf-8")) > _PROMPT_INLINE_CAP:
        # A RULED LIMIT, stated with its reason — never a silent cap. v1 fails closed above 8192 bytes because
        # prompt_has_payload demands EXACTLY ONE of inline prompt_text or a spill_artifact_id, D3 forbids the
        # importer writing bytes to the cloud, and rank 6 registers an EXISTING FILE — which a stem inside a
        # parquet is not. Minting a file to satisfy a CHECK is the invention D5 refuses. On this corpus the
        # longest genuine stem is 738 bytes, so this is a corruption tripwire rather than a limitation.
        return f"stem exceeds {_PROMPT_INLINE_CAP} bytes (prompt_inline_cap; v1 has no honest spill path)"
    if question["has_picture"]:
        return "has_picture (the stimulus is not fully representable as text; importing the stem alone would lie)"
    if question["options_are_pictures"]:
        return "options_are_pictures (the options are not text)"
    if question["phantom_suspected"]:
        return "phantom_suspected (the corpus flags this row as possibly not real)"
    if set(options) != set(_OPTION_LETTERS):
        # `num_options` is written as len(_OPTION_LETTERS), so a question whose real arity differs would be
        # recorded with a FALSE option count, and an extra letter would be dropped from the mirror unnoticed.
        # Measured on the corpus of record: exactly 5 options for all 360. A corpus that grows an arity is a
        # ruling, not a row to quietly reshape.
        return f"option letters {sorted(options)} are not the written set {list(_OPTION_LETTERS)}"
    missing = sorted(
        letter for letter in _OPTION_LETTERS if options.get(letter, {}).get("option_text") is None
    )
    if missing:
        return f"option_text is NULL for {missing} (mcq_options.option_text is NOT NULL)"
    return None


def _canonical_question_bytes(question: dict, options: dict[str, dict], set_hash: bytes) -> bytes:
    """The Layer-1 mirror's source bytes, and therefore the D2 natural key's preimage (rank 5 mints
    `source_pk = sha256(...)` from exactly these bytes, and stores them verbatim as `payload_text`).

    ★ THE WHOLE ROW GOES IN — every column of the question and every column of each of its option rows, not a
    hand-picked subset. Layer 1 is a FAITHFUL mirror (ADR-0003 / phase0 Q11): the columns this module does not
    PROMOTE (the psychometrics `percent_correct`/`incorrect_*`, `commentary`, `standard`, `grade_band`, the
    parse-provenance flags, `pct_chosen`, …) must still SURVIVE here, because Layer 1 is what makes a total DB loss
    cost a re-run rather than data. A curated preimage would have made the module's own "not mapped, but preserved
    in Layer 1" claim FALSE — the defect the post-build vet caught, and the reason this takes the whole row.
    Widening the preimage re-mints every `source_pk`, so it is nearly free now (nothing has ever imported) and
    expensive later: that timing is exactly why it is done here.

    A parquet row has NO canonical byte form (columnar and compressed), so the mirrored "source bytes" are this
    deterministic serialization rather than the file's bytes. `set_content_hash` is present by owner ruling: it is
    what makes the record grain SET-SCOPED and the cascade re-runnable (see the module docstring). Control
    characters are escaped by json.dumps regardless of ensure_ascii, so a NUL in the corpus cannot reach the
    Layer-1 writer's NUL refusal. A future corpus column of a JSON-unserializable type (a timestamp, a Decimal)
    raises here — fail-closed and loud, which is the correct direction for a mirror that must be total.

    NOT included: the three `misconceptions` tables. They are a SEPARATE subsystem keyed by question, not columns
    of this row, and this module does not read them — so they are genuinely NOT mirrored, and the docstring says so
    rather than implying Layer 1 covers them.
    """
    return canonical_bytes(
        {
            "scheme": PROMPT_SET_SCHEME,
            "set_content_hash": set_hash.hex(),
            "question": question,
            "options": {letter: row for letter, row in options.items()},
        }
    )


def prompt_set_content_hash(items: list[tuple[int, str]]) -> bytes:
    """The ruled `prompt_sets.content_hash`: sha256 over a canonical manifest of the set's ordered items.

    `items` MUST be exactly the `(item_ordinal, prompt_text)` pairs ACTUALLY WRITTEN to the set — never the
    document's full upstream content. Hashing what a set does not contain lets two genuinely different sets collide
    on a UNIQUE column, which is an outage rather than a nuance.

    `document_id` is deliberately ABSENT from the preimage: including it would hash the set's IDENTITY rather than
    its CONTENT, which is the candidate the owner rejected as making `content_hash` a lie. The accepted consequence
    is that two documents with byte-identical ordered stems would be ONE set — correct under content-hash identity,
    and not the case in this corpus.

    Goes through `db.identity.content_hash`, this column's named producer (its docstring and the column's DDL
    comment cite the same phase0 Q1/Q3). Hand-rolling a second canonicalization here would fork the one hash
    function — the binding "one implementation per concept".
    """
    return content_hash(
        {"scheme": PROMPT_SET_SCHEME, "items": [[ordinal, txt] for ordinal, txt in sorted(items)]}
    )


def map_mcq_corpus(
    repo: Repository,
    handle: ImportBatchHandle,
    method: PromotionMethod,
    *,
    corpus_root: Path,
    derived_by_predecessor: bool = False,
) -> McqMapResult:
    """Map the MOSART MCQ corpus: mirror every question, build the native cascade, promote the promotable ones.

    `corpus_root` is the directory holding `questions.parquet` / `question_options.parquet` / `documents.parquet`
    — never hardcoded (a machine-absolute constant is the local-lake base_uri wedge this engagement already paid
    for once). `method` is registered ONCE PER BATCH by the CALLER and passed in (ruling C); this module never
    mints one. `derived_by_predecessor` defaults False and is stated rather than assumed: MOSART AUTHORED these
    stems, options and answer keys, so they are source data — the D1 marking means "the predecessor DERIVED this
    value" and belongs on things like predecessor-extracted answer letters, which this module does not import. It
    stays an explicit parameter so a future corpus can say otherwise; rank 5 keeps it REQUIRED, which is where the
    D1 boundary is.

    Raises `ImporterIngressError` on an invalid handle/method/corpus shape or a corpus contradiction;
    `IdentityMismatchError` on a divergent re-import of an item's answer key or option text.
    """
    if not isinstance(handle, ImportBatchHandle):
        raise ImporterIngressError(
            "handle must be an import-batch token from open_import_batch — a raw id is refused (fail closed). On "
            "importer paths the handle is the only thing forcing the once-per-batch ADR-0020 durability consult."
        )
    if not isinstance(method, PromotionMethod):
        raise ImporterIngressError(
            "method must be a promotion-method token from register_promotion_method — a raw method_version_id is "
            "refused (fail closed). Register once per BATCH, never per row (ruling C)."
        )
    if not isinstance(derived_by_predecessor, bool):
        raise ImporterIngressError(
            f"derived_by_predecessor must be a bool, got {derived_by_predecessor!r} — refusing (fail closed)."
        )

    questions, option_rows, documents, corpus_sha256 = _read_corpus(corpus_root)

    options_by_question: dict[str, dict[str, dict]] = {}
    for row in option_rows:
        options_by_question.setdefault(row["question_id"], {})[row["letter"]] = row
    by_document: dict[str, list[dict]] = {}
    for question in questions:
        by_document.setdefault(question["document_id"], []).append(question)

    counts = dict.fromkeys(
        ("prompt_sets", "prompt_items", "mcq_items", "mcq_options", "external_records", "promotions"), 0
    )
    refusals: list[McqItemRefusal] = []

    for document_id in sorted(by_document):
        items = sorted(by_document[document_id], key=lambda q: int(q["item_num"]))
        promotable: list[tuple[dict, str]] = []  # (question, answer_letter)
        for question in items:
            options = options_by_question.get(question["question_id"], {})
            reason = _classify(question, options)
            # The RESOLVED letter is what gets written — never the raw `correct_answer` column — so the resolver's
            # return is load-bearing rather than decorative. Bound only on the success path, so a refused question
            # cannot carry a letter forward (which also makes the type honest: no possibly-unbound name, and no
            # unreachable `if letter is not None` branch to satisfy a checker).
            # HONEST: this is a BELT, UNPINNABLE BY CONSTRUCTION and deliberately NOT counted in the mutation
            # matrix. `_resolve_answer_letter` returns `correct_answer` unmodified on every non-refusing path, so
            # writing the raw column instead is a semantic no-op today and NO probe can redden it (mutation-run and
            # confirmed GREEN). It is forward insurance: if the resolver ever normalizes, the writer inherits it
            # rather than silently bypassing it.
            letter = ""
            if reason is None:
                resolved = _resolve_answer_letter(
                    question["question_id"], question["correct_answer"], options
                )
                if resolved is None:
                    reason = "correct_answer is NULL (mcq_items.correct_letter is NOT NULL)"
                elif resolved not in _OPTION_LETTERS:
                    # Nothing in the schema ties correct_letter to a letter that exists: mcq_items.correct_letter
                    # and mcq_options.letter are bare text with no CHECK and no FK between them, so an answer key
                    # naming an absent option would read CLEAN forever. Code is the only mechanism.
                    # Checked against the letters this module actually WRITES, never against the corpus's own
                    # letter set: a corpus supplying options a-f with correct_answer='f' would pass a membership
                    # test on ITS letters while 'f' is never written — reopening the exact hole.
                    reason = f"correct_answer {resolved!r} is not one of the written option letters"
                else:
                    letter = resolved
            if reason is not None:
                refusals.append(McqItemRefusal(question["question_id"], reason))
            else:
                promotable.append((question, letter))

        # The manifest covers EXACTLY the items written to this set — computed BEFORE any write, because both the
        # set and every record of this document are addressed on it.
        set_hash = prompt_set_content_hash([(int(q["item_num"]), q["stem"]) for q, _ in promotable])

        prompt_item_ids: dict[str, int] = {}
        if promotable:
            # The set and its items commit together, so an interrupted document cannot leave an itemless set.
            with repo.engine.begin() as conn:
                new_set = conn.execute(
                    _SET_INSERT, {"ch": set_hash, "sk": PROMPT_SET_KIND}
                ).scalar_one_or_none()
                if new_set is None:
                    set_id = int(conn.execute(_SET_RESELECT, {"ch": set_hash}).scalar_one())
                else:
                    set_id, counts["prompt_sets"] = int(new_set), counts["prompt_sets"] + 1
                for question, _ in promotable:
                    params = {"ps": set_id, "ord": int(question["item_num"]), "txt": question["stem"]}
                    new_item = conn.execute(_ITEM_INSERT, params).scalar_one_or_none()
                    if new_item is None:
                        item_id = int(conn.execute(_ITEM_RESELECT, params).scalar_one())
                    else:
                        item_id, counts["prompt_items"] = int(new_item), counts["prompt_items"] + 1
                    prompt_item_ids[question["question_id"]] = item_id

        # LAYER 1 IS FAITHFUL: mirror EVERY question of this document, refused or not.
        record_ids: dict[str, int] = {}
        for question in items:
            options = options_by_question.get(question["question_id"], {})
            mirrored = write_external_record(
                repo,
                handle,
                source_table=MCQ_SOURCE_TABLE,
                source_bytes=_canonical_question_bytes(question, options, set_hash),
                derived_by_predecessor=derived_by_predecessor,
                payload_jsonb={
                    # `kind` is LOAD-BEARING: external_records_kind_idx indexes exactly this expression, and it is
                    # the one functional JSON index the bindings permit. `corpus_sha256` is the identity of the
                    # FILE these bytes came from — this module registers no artifact and writes no lineage edge
                    # (see the module docstring), so the sidecar is the ONLY place the source file is named.
                    # Sidecar-only by design: payload_jsonb is outside the source_pk preimage AND outside rank 5's
                    # drift check, so carrying it re-mints nothing — but an ALREADY-mirrored record keeps its old
                    # sidecar silently (rank 5 keep-firsts the whole row), so this is not a back-fill path.
                    "kind": MCQ_RECORD_KIND,
                    "question_id": question["question_id"],
                    "document_id": question["document_id"],
                    "item_num": int(question["item_num"]),
                    "corpus_sha256": corpus_sha256,
                },
            )
            record_ids[question["question_id"]] = mirrored.external_record_id
            counts["external_records"] += int(mirrored.inserted)

        for question, letter in promotable:
            options = options_by_question[question["question_id"]]
            item_id = prompt_item_ids[question["question_id"]]
            with repo.engine.begin() as conn:
                new_mcq = conn.execute(
                    _MCQ_INSERT, {"pi": item_id, "cl": letter, "no": len(_OPTION_LETTERS)}
                ).scalar_one_or_none()
                if new_mcq is None:
                    row = conn.execute(_MCQ_RESELECT, {"pi": item_id}).one()
                    # SINGLE-PURPOSE drift guard: only the columns that genuinely contradict.
                    if row.correct_letter != letter or row.num_options != len(_OPTION_LETTERS):
                        raise IdentityMismatchError(
                            f"mcq_items for prompt_item {item_id} already records correct_letter="
                            f"{row.correct_letter!r}/num_options={row.num_options}; the corpus now states "
                            f"{letter!r}/{len(_OPTION_LETTERS)} — refusing to silently keep-first a contradictory "
                            "answer key (fail loud). The stem is unchanged, so this is the same set; reconcile "
                            "deliberately, do not re-scan."
                        )
                    mcq_id = int(row.mcq_item_id)
                else:
                    mcq_id, counts["mcq_items"] = int(new_mcq), counts["mcq_items"] + 1
                for opt_letter in _OPTION_LETTERS:
                    option_text = options[opt_letter]["option_text"]
                    params = {"mi": mcq_id, "l": opt_letter, "ot": option_text}
                    new_option = conn.execute(_OPTION_INSERT, params).scalar_one_or_none()
                    if new_option is None:
                        stored = conn.execute(_OPTION_RESELECT, {"mi": mcq_id, "l": opt_letter}).scalar_one()
                        if stored != option_text:
                            raise IdentityMismatchError(
                                f"mcq_options ({mcq_id}, {opt_letter!r}) already records different option text — "
                                "refusing to silently keep-first a contradictory option (fail loud). The stem is "
                                "unchanged, so this is the same set; reconcile deliberately, do not re-scan."
                            )
                    else:
                        counts["mcq_options"] += 1

            # The native row precedes its promotion: rank 8a REFUSES a promoted_pk naming no live row. Every step
            # above is keep-first, so a crash between any two of them is a resume rather than a corruption.
            promoted = promote_external_record(
                repo,
                handle,
                method,
                external_record_id=record_ids[question["question_id"]],
                promoted_kind=PromotedKind.MCQ_STIMULUS,
                promoted_pk=mcq_id,
            )
            counts["promotions"] += int(promoted.promoted)

    return McqMapResult(**counts, refusals=tuple(refusals))
