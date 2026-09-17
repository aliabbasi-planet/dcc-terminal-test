"""User-configurable table and schema names.

Clone this repo, edit the two schema prefixes below to match your Snowflake
environment, and every table reference in the pipeline and dashboard will
follow automatically.  No other file needs editing.

Example:
    CORE_SCHEMA        = "PROD_CORE.DCC"
    PRESENTATION_SCHEMA = "PROD_PRESENTATION.DCC"
"""

from __future__ import annotations

import os

# ── Schema prefixes (set via env vars or edit directly) ───────────────
CORE_SCHEMA = os.getenv("DCC_CORE_SCHEMA", "DEV_CORE_AAB.PUBLIC")
PRESENTATION_SCHEMA = os.getenv("DCC_PRESENTATION_SCHEMA", "DEV_CORE_AAB.PUBLIC")

# ── Input tables (read-only — must already exist) ─────────────────────
SNAPSHOT_TABLE = f"{CORE_SCHEMA}.DCC_HEALTH_DAILY_SNAPSHOT"
FLAG_REF_TABLE = f"{CORE_SCHEMA}.DCC_FLAG_REFERENCE"

# ── Output tables (written by the pipeline, read by the dashboard) ────
TRANSITION_LOG_TABLE = f"{PRESENTATION_SCHEMA}.DCC_TRANSITION_LOG"
FIX_EPISODE_TABLE = f"{PRESENTATION_SCHEMA}.DCC_FIX_EPISODE"
BREAK_EPISODE_TABLE = f"{PRESENTATION_SCHEMA}.DCC_BREAK_EPISODE"
EPISODE_PROFIT_TABLE = f"{PRESENTATION_SCHEMA}.DCC_EPISODE_PROFIT"
DAILY_TRACKER_TABLE = f"{PRESENTATION_SCHEMA}.DCC_DAILY_TRACKER"
SUMMARY_TABLE = f"{PRESENTATION_SCHEMA}.DCC_HEALTH_SUMMARY"

# ── Flag columns that participate in transition detection ─────────────
# These match the CHECK columns in the snapshot table.  The UNPIVOT in
# the pipeline iterates exactly this list.
FLAG_COLUMNS: tuple[str, ...] = (
    "LOCATION_DCCENABLED_CHECK_C",
    "HANDLER_DCCENABLE_CHECK_O",
    "HANDLER_DCCENABLECOMPLETION_CHECK_O",
    "HANDLER_DCCENABLEAUTH_CHECK_O",
    "HANDLER_DCCENABLENFC_CHECK_O",
    "HANDLER_DCCENABLENFCSINGLETAP_CHECK_O",
    "DCCXPRESSCO_CHECK_O",
    "DCCXPRESSCODT_CHECK_O",
    "DCCMERCHANT_NO_CHECK_O",
)
