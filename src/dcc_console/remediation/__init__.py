"""DCC Remediation — Snowflake-backed identify/analyse/fix package.

This subpackage is self-contained and additive: it adds a third console tab that
discovers broken terminals from the daily maintenance snapshot in Snowflake,
lets an operator analyse and filter them, and (in a later phase) fixes them one
by one through the same stored procedure the single-test tab already drives.

Multi-tenant by design: the target schema is not hardcoded. Each user points the
tab at their own ``DEV_CORE_<x>`` via :class:`RemediationObjects`, which is built
from the connection at runtime and threaded into every query builder. The
database/schema names are validated as SQL identifiers before they are ever
placed in SQL text (they are interpolated, not bound), so a caller cannot inject
through them.

Design rules mirrored from the rest of the console:
  * SQL identifiers are declared in code (see :mod:`.mapping`) or validated here,
    never taken raw from user input; filter *values* are always bound as params.
  * The pure query-builders (:mod:`.mapping`, :mod:`.snapshot`, :mod:`.worklist`,
    :mod:`.analytics`, :mod:`.fixlog`, :mod:`.ddl`) import nothing heavy and are
    unit-tested.
  * Only :mod:`.sf_connection` and :mod:`.tab` touch Snowflake / Streamlit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Shared, read-only discovery source — the same for every user.
MAINTENANCE_SOURCE = "PROD_PRESENTATION.CORTEX.CORTEX_TERMINAL_MAINTENANCE"

# The account's DCC maintenance Cortex Agent (Cortex Analyst over the maintenance
# semantic view). Queried on demand from the tab so an operator can ask about a
# terminal before changing it. Read-only; called with the operator's own SSO session.
DCC_AGENT_FQN = "PROD_PRESENTATION.CORTEX.DCC_TERMINAL_MAINTENANCE_AGENT"

# Unquoted Snowflake identifier: letter/underscore start, then letters/digits/_/$.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def validate_identifier(value: str, kind: str) -> str:
    """Return the trimmed identifier or raise if it is not a safe bare identifier.

    Used for the per-user database/schema names, which are interpolated into DDL
    and fully-qualified object names rather than bound as parameters.
    """
    trimmed = (value or "").strip()
    if not _IDENTIFIER.match(trimmed):
        raise ValueError(f"Invalid {kind} identifier: {value!r}")
    return trimmed


def parse_schema_fqn(value: str) -> tuple[str, str]:
    """Split and validate ``DATABASE.SCHEMA`` (e.g. the shared fix-log location)."""
    parts = (value or "").strip().split(".")
    if len(parts) != 2:
        raise ValueError(f"Expected DATABASE.SCHEMA, got {value!r}")
    return (
        validate_identifier(parts[0], "shared database"),
        validate_identifier(parts[1], "shared schema"),
    )


@dataclass(frozen=True)
class RemediationObjects:
    """Fully-qualified names for one tenant's remediation schema.

    Built from the user's chosen database (+ schema); every builder takes an
    instance so the same code serves any ``DEV_CORE_<x>``.

    The fix log and the live-fix operator allowlist can live in a *shared* schema
    (``shared_database``/``shared_schema``) so a team has one audit trail and one
    re-fix guard, while each user keeps their own snapshot and views. When no
    shared schema is given they live in the user's own schema.
    """

    database: str
    schema: str = "DCC_REMEDIATION"
    shared_database: str | None = None
    shared_schema: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", validate_identifier(self.database, "database"))
        object.__setattr__(self, "schema", validate_identifier(self.schema, "schema"))
        object.__setattr__(
            self,
            "shared_database",
            validate_identifier(self.shared_database or self.database, "shared database"),
        )
        object.__setattr__(
            self,
            "shared_schema",
            validate_identifier(self.shared_schema or self.schema, "shared schema"),
        )

    @property
    def schema_fqn(self) -> str:
        return f"{self.database}.{self.schema}"

    @property
    def shared_fqn(self) -> str:
        return f"{self.shared_database}.{self.shared_schema}"

    @property
    def uses_shared_log(self) -> bool:
        """True when the fix log lives outside the user's own schema."""
        return self.shared_fqn.upper() != self.schema_fqn.upper()

    @property
    def snapshot_table(self) -> str:
        return f"{self.schema_fqn}.HEALTH_DAILY_SNAPSHOT"

    @property
    def fix_log_table(self) -> str:
        return f"{self.shared_fqn}.APP_FIX_LOG"

    @property
    def operators_table(self) -> str:
        return f"{self.shared_fqn}.FIX_OPERATORS"

    @property
    def registry_table(self) -> str:
        """Durable registry of verified PROD fixes (survives the daily refresh).

        One row per (ENVIRONMENT, TERMINAL_IDENTIFIER, CHECK_COLUMN) currently fixed;
        inserted when a live fix verifies, deleted when a rollback is confirmed. The
        worklist view reads its PROD rows so a fix is not re-flagged after a refresh.
        """
        return f"{self.shared_fqn}.DCC_FIX_REGISTRY"

    @property
    def confirmed_table(self) -> str:
        """Profit-tracking handoff: fixes the Cortex source has confirmed as healthy.

        One row per confirmed fix event ``(ENVIRONMENT, TERMINAL_IDENTIFIER, CHECK_COLUMN,
        FIXED_AT_UTC)``, carrying the fix metadata plus the terminal's dimensions. The separate
        financial app reads this; the operational app only writes it (see :mod:`.reconcile`).
        """
        return f"{self.shared_fqn}.DCC_CONFIRMED_FIXES"

    @property
    def reconcile_task(self) -> str:
        """Daily task that confirms fixes against Cortex and fills the handoff (owner-run)."""
        return f"{self.shared_fqn}.DCC_RECONCILE_CONFIRMED_FIXES_TASK"

    @property
    def flag_reference_table(self) -> str:
        return f"{self.schema_fqn}.FLAG_REFERENCE"

    @property
    def v_current_broken(self) -> str:
        return f"{self.schema_fqn}.V_CURRENT_BROKEN"

    @property
    def v_fix_history(self) -> str:
        return f"{self.schema_fqn}.V_FIX_HISTORY"

    @property
    def v_remediation_kpis(self) -> str:
        return f"{self.schema_fqn}.V_REMEDIATION_KPIS"


# Convenience default (the schema created during initial development). The tab
# always constructs objects from the live connection instead of relying on this.
DEFAULT_OBJECTS = RemediationObjects("DEV_CORE_AAB")

# Team-wide fix log + operator allowlist (decision 2026-09-29: named group, shared
# log). Owned by role DATA_SCIENTIST; teammates on another role need the grants in
# docs/plan_dcc_remediation.md §15.
DEFAULT_SHARED_SCHEMA = "DEV_CORE_AAB.DCC_REMEDIATION"
