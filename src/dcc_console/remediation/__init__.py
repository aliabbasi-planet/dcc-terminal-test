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


@dataclass(frozen=True)
class RemediationObjects:
    """Fully-qualified names for one tenant's remediation schema.

    Built from the user's chosen database (+ schema); every builder takes an
    instance so the same code serves any ``DEV_CORE_<x>``.
    """

    database: str
    schema: str = "DCC_REMEDIATION"

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", validate_identifier(self.database, "database"))
        object.__setattr__(self, "schema", validate_identifier(self.schema, "schema"))

    @property
    def schema_fqn(self) -> str:
        return f"{self.database}.{self.schema}"

    @property
    def snapshot_table(self) -> str:
        return f"{self.schema_fqn}.HEALTH_DAILY_SNAPSHOT"

    @property
    def fix_log_table(self) -> str:
        return f"{self.schema_fqn}.APP_FIX_LOG"

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
