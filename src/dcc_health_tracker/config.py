"""User-configurable table and schema names.

This project is self-contained and writes all output to DEV_CORE_AAB.
It relies on only TWO external source tables in PROD:

  1. PROD_PRESENTATION.CORTEX.CORTEX_TERMINAL_MAINTENANCE
     - Terminal health/config data (daily snapshot source)

  2. PROD_CORE.TRANSFORMATION.TRN_DWH_DCC_REVENUE
     - DCC profit/revenue data (for episode profit allocation)

Edit the OUTPUT_SCHEMA below if deploying elsewhere.
"""

from __future__ import annotations

import os

# ═══════════════════════════════════════════════════════════════════════
# EXTERNAL SOURCE TABLES (read-only — owned by other teams)
# ═══════════════════════════════════════════════════════════════════════
SOURCE_TERMINAL_TABLE = "PROD_PRESENTATION.CORTEX.CORTEX_TERMINAL_MAINTENANCE"
SOURCE_REVENUE_TABLE = "PROD_CORE.TRANSFORMATION.TRN_DWH_DCC_REVENUE"

# ═══════════════════════════════════════════════════════════════════════
# OUTPUT SCHEMA (all tables created/owned by this project)
# ═══════════════════════════════════════════════════════════════════════
OUTPUT_SCHEMA = os.getenv("DCC_OUTPUT_SCHEMA", "DEV_CORE_AAB.PUBLIC")

# ── Snapshot table (daily MERGE from SOURCE_TERMINAL_TABLE) ───────────
SNAPSHOT_TABLE = f"{OUTPUT_SCHEMA}.DCC_V3_HEALTH_DAILY_SNAPSHOT"

# ── Reference table (flag metadata) ───────────────────────────────────
FLAG_REF_TABLE = f"{OUTPUT_SCHEMA}.DCC_V3_FLAG_REFERENCE"

# ── Output tables (written by the pipeline, read by the dashboard) ────
FIX_EPISODE_TABLE = f"{OUTPUT_SCHEMA}.DCC_V3_FIX_EPISODES"
FIX_EPISODE_PROFIT_TABLE = f"{OUTPUT_SCHEMA}.DCC_V3_FIX_EPISODE_PROFIT"
DAILY_TRACKER_TABLE = f"{OUTPUT_SCHEMA}.DCC_V3_FIX_DAILY_TRACKER"
SUMMARY_TABLE = f"{OUTPUT_SCHEMA}.DCC_V3_FIX_PROFIT_SUMMARY"

# ═══════════════════════════════════════════════════════════════════════
# FLAG COLUMNS — Boolean (0/1) health check columns
# ═══════════════════════════════════════════════════════════════════════
# These are INTEGER columns where 1 = broken, 0 = healthy.
# The pipeline detects transitions: 1→0 = FIXED, 0→1 = BROKEN.
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

# ═══════════════════════════════════════════════════════════════════════
# FIRMWARE VERSION — String column (separate detection logic)
# ═══════════════════════════════════════════════════════════════════════
# Firmware version changes are detected as: prev_version != curr_version.
# Any change is recorded as a "firmware fix" event.
FIRMWARE_COLUMN = "FIRMWARE_VERSION"
