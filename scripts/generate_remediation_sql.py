"""Regenerate the reviewable SQL snapshots under ``sql/remediation/`` from the builders.

The app never reads these files — it runs the builders directly — but they are kept
in the repo so the schema, seed and refresh SQL can be reviewed and run by hand. Run
this whenever the builders change so the snapshots do not drift:

    python scripts/generate_remediation_sql.py

Uses the default tenant (DEV_CORE_AAB, shared log in the same schema), matching the
checked-in files.
"""

from __future__ import annotations

from pathlib import Path

from dcc_console.remediation import RemediationObjects, ddl, snapshot

_OBJS = RemediationObjects("DEV_CORE_AAB")
_OUT = Path(__file__).resolve().parents[1] / "sql" / "remediation"


def _header(title: str, builder: str) -> str:
    return (
        "-- =====================================================================\n"
        f"-- {title}\n"
        f"-- GENERATED from src/dcc_console/remediation/{builder}.\n"
        "-- Do not hand-edit; change the builder and regenerate. Idempotent, safe to re-run.\n"
        '-- The app\'s "Initialise / verify my schema" button runs exactly these statements\n'
        "-- for the connected user's own schema (+ the shared fix-log schema).\n"
        "-- =====================================================================\n\n"
    )


def _write(name: str, header: str, body: str) -> None:
    (_OUT / name).write_text(header + body.rstrip() + "\n", encoding="utf-8")
    print(f"wrote {name}")


def main() -> None:
    schema = ";\n\n".join(ddl.build_initialise_statements(_OBJS)) + ";"
    _write(
        "001_schema.sql",
        _header(
            f"DCC Remediation schema for {_OBJS.schema_fqn} (shared log: {_OBJS.shared_fqn})",
            "ddl.py (build_initialise_statements())",
        ),
        schema,
    )
    _write(
        "002_seed_flag_reference.sql",
        _header(f"Seed {_OBJS.flag_reference_table}", "ddl.py (build_seed_merge())"),
        ddl.build_seed_merge(_OBJS) + ";",
    )
    _write(
        "003_refresh_snapshot.sql",
        _header(f"Refresh {_OBJS.snapshot_table}", "snapshot.py (build_refresh_merge())"),
        snapshot.build_refresh_merge(_OBJS) + ";",
    )


if __name__ == "__main__":
    main()
