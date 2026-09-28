"""DCC Remediation — Snowflake-backed identify/analyse/fix package.

This subpackage is self-contained and additive: it adds a third console tab that
discovers broken terminals from the daily maintenance snapshot in Snowflake,
lets an operator analyse and filter them, and (in a later phase) fixes them one
by one through the same stored procedure the single-test tab already drives.

Design rules mirrored from the rest of the console:
  * SQL identifiers are declared in code (see :mod:`.mapping`), never built from
    user input; filter *values* are always bound as parameters.
  * The pure query-builders (:mod:`.mapping`, :mod:`.snapshot`, :mod:`.worklist`,
    :mod:`.analytics`, :mod:`.fixlog`) import nothing heavy and are unit-tested.
  * Only :mod:`.sf_connection` and :mod:`.tab` touch Snowflake / Streamlit.
"""

from __future__ import annotations

SCHEMA = "DEV_CORE_AAB.DCC_REMEDIATION"
SNAPSHOT_TABLE = f"{SCHEMA}.HEALTH_DAILY_SNAPSHOT"
FIX_LOG_TABLE = f"{SCHEMA}.APP_FIX_LOG"
FLAG_REFERENCE_TABLE = f"{SCHEMA}.FLAG_REFERENCE"
V_CURRENT_BROKEN = f"{SCHEMA}.V_CURRENT_BROKEN"
V_FIX_HISTORY = f"{SCHEMA}.V_FIX_HISTORY"
V_REMEDIATION_KPIS = f"{SCHEMA}.V_REMEDIATION_KPIS"

MAINTENANCE_SOURCE = "PROD_PRESENTATION.CORTEX.CORTEX_TERMINAL_MAINTENANCE"
