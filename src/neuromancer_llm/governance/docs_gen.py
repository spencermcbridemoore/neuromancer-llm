"""Generated docs — schema / CLI / environment / governance + the split ADRs.

Docs CANNOT rot (phase0 Q14): every artifact here is REGENERATED from the live models / CLI / source,
and `neuro docs build --check` fails CI if the committed copy drifts by a single byte. Output is fully
deterministic (sorted, no timestamps) and written/compared as bytes with LF newlines, so the gate is
identical on Windows and Linux.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, PrimaryKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "docs"
ADR_SOURCE = DOCS / "adr" / "_source" / "phase3-adrs.md"

_PG = postgresql.dialect()


# --- schema.md (from the ORM metadata) -------------------------------------------------------------
def _render_schema() -> str:
    from ..db.orm import Base

    md = Base.metadata
    lines = [
        "# Schema reference (generated)",
        "",
        "Generated from `neuromancer_llm.db.orm` by `neuro docs build`. The schema is owned by Alembic",
        "(migrations-from-zero against real Postgres 18); this file mirrors the ORM. Do not edit by hand.",
        "",
    ]

    enums: dict[str, tuple[str, ...]] = {}
    for table in md.sorted_tables:
        for col in table.columns:
            t = col.type
            if isinstance(t, postgresql.ENUM) and t.name is not None:
                enums[t.name] = tuple(t.enums)
    lines += ["## Enum types", ""]
    for name in sorted(enums):
        lines.append(f"- `{name}`: {', '.join(enums[name])}")
    lines += ["", f"_{len(md.sorted_tables)} tables, {len(enums)} enum types._", "", "## Tables", ""]

    for table in sorted(md.sorted_tables, key=lambda t: t.name):
        lines.append(f"### `{table.name}`")
        lines.append("")
        lines.append("| column | type | null | default |")
        lines.append("|---|---|---|---|")
        for col in table.columns:
            coltype = col.type.compile(dialect=_PG)
            default = ""
            if col.identity is not None:
                default = "IDENTITY (always)"
            elif col.server_default is not None:
                arg = getattr(
                    col.server_default, "arg", None
                )  # DefaultClause has .arg; FetchedValue does not
                text_attr = getattr(arg, "text", None)
                default = text_attr if text_attr is not None else (str(arg) if arg is not None else "")
            null = "" if col.nullable else "NOT NULL"
            lines.append(f"| `{col.name}` | `{coltype}` | {null} | {default} |")
        lines.append("")

        constraints = []
        for c in table.constraints:
            if isinstance(c, PrimaryKeyConstraint) and c.columns:
                constraints.append(("PRIMARY KEY", c.name or "", ", ".join(col.name for col in c.columns)))
            elif isinstance(c, UniqueConstraint) and c.columns:
                nnd = (
                    " NULLS NOT DISTINCT" if c.dialect_options["postgresql"].get("nulls_not_distinct") else ""
                )
                constraints.append((f"UNIQUE{nnd}", c.name or "", ", ".join(col.name for col in c.columns)))
            elif isinstance(c, CheckConstraint):
                constraints.append(("CHECK", c.name or "", str(c.sqltext)))
            elif isinstance(c, ForeignKeyConstraint):
                target = ", ".join(e.target_fullname for e in c.elements)
                constraints.append(
                    ("FOREIGN KEY", c.name or "", f"({', '.join(col.name for col in c.columns)}) -> {target}")
                )
        for kind, name, detail in sorted(constraints):
            label = f" `{name}`" if name else ""
            lines.append(f"- {kind}{label}: {detail}")

        idx_lines = []
        for idx in table.indexes:
            cols = ", ".join(c.name for c in idx.columns) if idx.columns else "(expression)"
            unique = "UNIQUE " if idx.unique else ""
            where = idx.dialect_options["postgresql"].get("where")
            nnd = " NULLS NOT DISTINCT" if idx.dialect_options["postgresql"].get("nulls_not_distinct") else ""
            partial = f" WHERE {where}" if where is not None else ""
            idx_lines.append(f"- {unique}INDEX `{idx.name}` ({cols}){nnd}{partial}")
        for line in sorted(idx_lines):
            lines.append(line)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# --- cli.md (from the typer app via click) ---------------------------------------------------------
def _render_cli() -> str:
    import typer

    from ..cli.app import app

    root = typer.main.get_command(app)
    lines = [
        "# CLI reference (generated)",
        "",
        "The one `neuro` CLI (ADR-0038). Generated from `neuromancer_llm.cli.app` by `neuro docs build`.",
        "",
    ]

    def first_line(cmd) -> str:
        text = (cmd.help or getattr(cmd, "short_help", "") or "").strip()
        return text.splitlines()[0] if text else ""

    def walk(cmd, prefix: str) -> None:
        names = sorted(getattr(cmd, "commands", {}))
        for name in names:
            sub = cmd.commands[name]
            full = f"{prefix} {name}".strip()
            lines.append(f"- `neuro {full}` — {first_line(sub)}")
            if hasattr(sub, "commands"):
                walk(sub, full)

    walk(root, "")
    return "\n".join(lines).rstrip("\n") + "\n"


# --- environment.md (the infrastructure-only env surface) ------------------------------------------
_PRODUCT_ENV = [
    (
        "NEURO_DATABASE_URL",
        "The canonical write DSN (`postgresql+psycopg://...`). The primary product env var; "
        "behavior never lives in env (NEVER-AGAIN: SQ_*-style shell flags) — these are infrastructure DSNs.",
    ),
    (
        "NEURO_MIGRATION_EXPECTED_LANE",
        "The lane a migration must positively match once the DB is provisioned "
        "(ADR-0006). Set by provisioning/CI, never baked into code.",
    ),
    (
        "NEURO_READER_DATABASE_URL",
        "Optional SELECT-only (neuro_reader) DSN for the read/export surface (`neuro capture show`); "
        "falls back to NEURO_DATABASE_URL. The SELECT-only boundary is the role (phase0 Q12).",
    ),
    (
        "NEURO_EXPECTED_LANE",
        "The lane `neuro capture logprob`/`replay` positively verify before any write (default "
        "`canonical`); for `capture show` it is an optional positive lane confirm (default: none). "
        "Infra config for the fail-closed lane guard, never a behavior switch.",
    ),
    (
        "NEURO_VLLM_BASE_URL",
        "Base URL of the vLLM OpenAI-compatible server the capture lane talks to "
        "(default http://127.0.0.1:8000).",
    ),
    (
        "NEURO_NTFY_TOPIC",
        "The alert-channel topic env var (ADR-0019). Only the VARIABLE NAME is documented — the topic "
        "value IS the credential (a secret; never in git or generated docs). Consumed by "
        "governance.notify() (the stdlib ntfy.sh push, go-remote Stage A A2-12); a missing topic fails "
        "loud (ConfigurationError, no default-topic fallback).",
    ),
    (
        "NEURO_NTFY_BASE_URL",
        "Optional ntfy endpoint override for governance.notify() (default https://ntfy.sh); set to a "
        "self-hosted ntfy server. Infrastructure config (which endpoint), never a behavior switch.",
    ),
    (
        "NEURO_BACKUP_DEST",
        "The off-cloud backup-mirror destination PATH (A2-16; e.g. D:/neuro-backups on the desktop NVMe) "
        "consumed by `neuro probe run --key backup_freshness` as a typer envvar option — the driver itself "
        "is parameter-only (env-free, L13-scanned). The path must pass the ADR-0014 off-cloud guard "
        "(assert_offcloud_destination: never an Azure shape); the SSH host/user/key travel as an ssh_config "
        "Host alias, never in this variable and never on argv.",
    ),
    (
        "AZURE_STORAGE_CONNECTION_STRING",
        "The production cloud credential (account-key connection string — the accepted Stage-A ADR-0013 "
        "deviation; user-delegation SAS via storage/sas.py is an unbuilt Stage-B item). Consumed by "
        "registry.backends.make_backend ALONE, which also cross-checks the credential's account endpoint "
        "host against the registered base_uri; `AzureBlobBackend` itself reads NO env and has NO fallback "
        "(C5, GO-D-cost) — an azure_blob resolve with this unset and no explicit string FAILS CLOSED, "
        "never an Azurite fallback write.",
    ),
]
_SECRET_ENV = [
    (
        "ntfy topic",
        "The probe alert channel topic (ADR-0019) — treated as a SECRET; never in git or generated docs.",
    ),
    ("user-delegation SAS", "Storage SAS (ADR-0013) — minted with a TTL; never committed."),
]
_TEST_ENV = [
    (
        "NEURO_TEST_DATABASE_URL",
        "Test DB DSN; if set, the suite reuses it (CI service container) instead of testcontainers.",
    ),
    (
        "NEURO_TEST_AZURITE",
        "Azurite connection string for the W1-W8 seam tests — threaded EXPLICITLY by the test fixtures "
        "(the backend class reads no env, C5); absent, fixtures use the well-known Azurite default string.",
    ),
    (
        "NEURO_TEST_GPU / NEURO_TEST_API / NEURO_TEST_NETWORK",
        "Capability flags; absent -> the matching marker skips visibly.",
    ),
    (
        "NEURO_VLLM_CONTROL_BASE_URL",
        "Optional second vLLM server (batch-invariance OFF) for the E6 control arm; unset -> the "
        "control-arm assertions are omitted from the E6 test (the main arm still runs).",
    ),
    (
        "NEURO_VLLM_TOKENIZER_FILE",
        "Path to the served model's tokenizer.json for the live capture tests (sha256'd into the "
        "durable tokenizer identity).",
    ),
    ("UV_SYSTEM_CERTS", "Make uv trust the OS certificate store (corporate TLS interception)."),
]


def _render_environment() -> str:
    lines = [
        "# Environment reference (generated)",
        "",
        "The env surface is infrastructure-only (ADR-0006). Generated by `neuro docs build`.",
        "",
        "## Product",
        "",
    ]
    for name, desc in _PRODUCT_ENV:
        lines.append(f"- `{name}` — {desc}")
    lines += ["", "## Infrastructure secrets (never committed)", ""]
    for name, desc in _SECRET_ENV:
        lines.append(f"- {name} — {desc}")
    lines += ["", "## Test / CI", ""]
    for name, desc in _TEST_ENV:
        lines.append(f"- `{name}` — {desc}")
    return "\n".join(lines) + "\n"


# --- governance.md (roles, markers, CI lanes, ADR status inventory) --------------------------------
def _render_governance() -> str:
    from ..db.provision import ROLES

    statuses = _adr_statuses()
    status_counts: dict[str, int] = {}
    for _, _, status in statuses:
        status_counts[status] = status_counts.get(status, 0) + 1

    lines = [
        "# Governance inventory (generated)",
        "",
        "Generated by `neuro docs build`. Roles, markers, CI lanes, and ADR status counts.",
        "",
        "## Postgres roles (ADR-0007)",
        "",
    ]
    for role in ROLES:
        lines.append(f"- `{role}`")
    lines += [
        "",
        "Boundary (C3): GPU/remote workers connect as `neuro_writer` (operational INSERT + lifecycle UPDATE, "
        "no identity INSERT, no DELETE); registry/identity INSERT is `neuro_registrar` (VM-local). "
        "`lineage_edges` + `spend_entries` are shared writer/registrar (B2). Reader is SELECT-only.",
        "",
        "## Test markers (detection hooks, skip visibly)",
        "",
        "- `pg` — requires real Postgres 18 (testcontainers / CI service container)",
        "- `seam` — requires real Azurite AND Postgres (W1-W8 kill-tests)",
        "- `gpu` / `api` / `network` — require a local capability; absent -> skipped visibly",
        "",
        "## CI lanes",
        "",
        "- **fast** (every push): ruff + pyright + unit/`pg` tests on a session-scoped `postgres:18` (no `seam`); "
        "`neuro docs build --check` (byte-gate) + offline lychee link check.",
        "- **full** (PR + main): migrations-from-zero + DDL parity + golden + W1-W8 seam on `postgres:18` + Azurite; "
        "wheel build/install/`neuro --version` smoke; `neuro docs build --check`; Docker build + compose boot smoke.",
        "",
        "## ADR status inventory",
        "",
        f"_{len(statuses)} ADRs._",
        "",
    ]
    for status in sorted(status_counts):
        lines.append(f"- {status}: {status_counts[status]}")
    return "\n".join(lines) + "\n"


# --- ADR split (from the carried source) -----------------------------------------------------------
_ADR_HEADER = re.compile(r"^### (ADR-(\d{4})) — (.+)$")
# The Status value ends at the ` · **Source:**` separator, at an inline `. **Decision.**` (the one-line
# Group-3 ADRs put Status and Decision on a single line — without this stop the whole Decision paragraph
# was captured as the Status, corrupting the index and splintering the governance status inventory), or
# at end of line.
_STATUS = re.compile(r"^\*\*Status:\*\*\s*(.+?)(?:\s*·|\.\s+\*\*Decision|\s*$)", re.MULTILINE)


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s


def _split_adrs() -> list[tuple[str, str, str, str]]:
    """Return sorted (number, adr_id, title, body) tuples from the carried ADR source."""
    text = ADR_SOURCE.read_text(encoding="utf-8")
    lines = text.splitlines()
    sections: list[tuple[str, str, str, list[str]]] = []
    current = None
    for line in lines:
        m = _ADR_HEADER.match(line)
        if m:
            if current:
                sections.append(current)
            current = (m.group(2), m.group(1), m.group(3).strip(), [line])
        elif current:
            # stop a section at the next top-level group header
            if line.startswith("## ") and not line.startswith("### "):
                sections.append(current)
                current = None
            else:
                current[3].append(line)
    if current:
        sections.append(current)
    out = []
    for number, adr_id, title, body in sections:
        out.append((number, adr_id, title, "\n".join(body).rstrip("\n") + "\n"))
    return sorted(out, key=lambda s: s[0])


def _adr_statuses() -> list[tuple[str, str, str]]:
    out = []
    for number, _adr_id, title, body in _split_adrs():
        m = _STATUS.search(body)
        status = m.group(1).strip() if m else "Unknown"
        out.append((number, title, status))
    return out


def _render_targets() -> dict[Path, bytes]:
    targets: dict[Path, bytes] = {
        DOCS / "reference" / "schema.md": _render_schema().encode("utf-8"),
        DOCS / "reference" / "cli.md": _render_cli().encode("utf-8"),
        DOCS / "reference" / "environment.md": _render_environment().encode("utf-8"),
        DOCS / "reference" / "governance.md": _render_governance().encode("utf-8"),
    }
    adrs = _split_adrs()
    index = [
        "# Architecture Decision Records (generated index)",
        "",
        "Split from `docs/adr/_source/phase3-adrs.md` by `neuro docs build`. ADR numbers are permanent.",
        "",
        "| ADR | Title | Status |",
        "|---|---|---|",
    ]
    for number, adr_id, title, body in adrs:
        fname = f"{number}-{_slug(title)}.md"
        targets[DOCS / "adr" / fname] = body.encode("utf-8")
        m = _STATUS.search(body)
        status = m.group(1).strip() if m else "Unknown"
        index.append(f"| [{adr_id}]({fname}) | {title} | {status} |")
    targets[DOCS / "adr" / "index.md"] = ("\n".join(index) + "\n").encode("utf-8")
    return targets


def build_docs(check: bool = False) -> int:
    """Regenerate all docs. With check=True, write nothing and return 1 if anything drifts (CI gate)."""
    targets = _render_targets()
    expected_paths = set(targets)

    # orphan detection: generated dirs must contain EXACTLY the generated files (excluding _source/).
    on_disk: set[Path] = set()
    for sub in (DOCS / "reference", DOCS / "adr"):
        if sub.exists():
            on_disk |= {p for p in sub.glob("*.md")}

    if check:
        drift = []
        for path in sorted(expected_paths):
            actual = path.read_bytes() if path.exists() else None
            if actual != targets[path]:
                drift.append(f"changed/missing: {path.relative_to(REPO_ROOT)}")
        for path in sorted(on_disk - expected_paths):
            drift.append(f"orphan (regenerate to remove): {path.relative_to(REPO_ROOT)}")
        if drift:
            print("docs OUT OF DATE — run `neuro docs build`:")
            for d in drift:
                print(f"  {d}")
            return 1
        print(f"docs up to date ({len(targets)} files)")
        return 0

    for path in sorted(on_disk - expected_paths):
        path.unlink()  # remove orphans so the tree matches exactly
    for path, content in sorted(targets.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print(f"generated {len(targets)} docs under {DOCS.relative_to(REPO_ROOT)}/")
    return 0
