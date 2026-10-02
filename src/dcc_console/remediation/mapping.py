"""Canonical mapping from a maintenance CHECK column to the fix that repairs it.

Single source of truth the app uses to turn a broken flag into a stored-procedure
call. It mirrors ``DEV_CORE_AAB.DCC_REMEDIATION.FLAG_REFERENCE`` (seeded by
``sql/remediation/002_seed_flag_reference.sql``) and links every *fixable* flag to
its :mod:`dcc_console.catalog` entry, so the live fixer reuses the exact same
parameterised EXEC + rollback path as the single-test tab.

Every column / identifier name here is a declared constant, never built from user
input; that is what keeps the generated snapshot and worklist SQL injection-proof.
"""

from __future__ import annotations

from dataclasses import dataclass

# Identifier kinds the stored procedure can bind a fix to.
INSTANCE = "INSTANCE_IDENTIFIER"
TERMINAL = "TERMINAL_IDENTIFIER"
LOCATION = "LOCATION_NO"


@dataclass(frozen=True)
class FlagFix:
    """One maintenance CHECK column and how (if at all) the console fixes it."""

    check_column: str          # maintenance-table CHECK column == snapshot column name
    base_column: str           # maintenance-table BASE column that scopes the check
    flag_name: str             # human label
    flag_type: str             # HANDLER | DCC_XPRESS | TEMPLATE | CONFIG | FIRMWARE | MERCHANT
    fix_bit: int | None        # @display_config for the SP (None = not fixable here)
    target_id_kind: str | None  # INSTANCE / TERMINAL / LOCATION
    # What the fix sets: Bit 8 flag name, Bit 1 function name (the call itself sends
    # "Add"), Bit 2 version description. None = chosen per terminal (Bit 16 template).
    fix_value: str | None
    sp_column: str | None      # handler column the SP edits (mirror of catalog.sp_column)
    fixable: bool
    catalog_key: str | None = None  # TEST_CATALOG key the live fixer reuses
    notes: str = ""

    @property
    def is_handler_object(self) -> bool:
        """True for handler/object-level checks (…_CHECK_O)."""
        return self.check_column.endswith("_CHECK_O")


# Order here is the canonical column order for the snapshot fact table.
FLAG_FIXES: tuple[FlagFix, ...] = (
    FlagFix(
        "LOCATION_DCCENABLED_CHECK_C", "LOCATION_DCCENABLED_BASE",
        "Location DCC Enabled", "CONFIG", None, None, None, None, False,
        notes="CONFIRM semantics with data owner; returned 0 broken in PROD.",
    ),
    FlagFix(
        "HANDLER_DCCENABLE_CHECK_O", "HANDLER_DCCENABLE_BASE",
        "Handler DCC Enable", "HANDLER", 8, INSTANCE, "dccEnable", "extra_config", True,
        catalog_key="Bit 8 — DCC Handler Flags (instance)", notes="Bit 8 handler flag.",
    ),
    FlagFix(
        "HANDLER_DCCENABLECOMPLETION_CHECK_O", "HANDLER_DCCENABLECOMPLETION_BASE",
        "Handler DCC Completion", "HANDLER", 8, INSTANCE, "dccEnableCompletion", "extra_config",
        True, catalog_key="Bit 8 — DCC Handler Flags (instance)", notes="Bit 8 handler flag.",
    ),
    FlagFix(
        "HANDLER_DCCENABLEAUTH_CHECK_O", "HANDLER_DCCENABLEAUTH_BASE",
        "Handler DCC Auth", "HANDLER", 8, INSTANCE, "dccEnableAuth", "extra_config", True,
        catalog_key="Bit 8 — DCC Handler Flags (instance)", notes="Bit 8 handler flag.",
    ),
    FlagFix(
        "HANDLER_DCCENABLENFC_CHECK_O", "HANDLER_DCCENABLENFC_BASE",
        "Handler DCC NFC", "HANDLER", 8, INSTANCE, "dccEnableNfc", "extra_config", True,
        catalog_key="Bit 8 — DCC Handler Flags (instance)", notes="Bit 8 handler flag.",
    ),
    FlagFix(
        "HANDLER_DCCENABLENFCSINGLETAP_CHECK_O", "HANDLER_DCCENABLENFCSINGLETAP_BASE",
        "Handler DCC NFC Single Tap", "HANDLER", 8, INSTANCE, "dccEnableNfcSingleTap",
        "extra_config", True, catalog_key="Bit 8 — DCC Handler Flags (instance)",
        notes="Bit 8 handler flag.",
    ),
    FlagFix(
        "DCCFLAGSENABLED_CHECK_C", "DCCFLAGSENABLED_BASE",
        "DCC Flags Enabled", "HANDLER", 8, INSTANCE, "dccFlagsEnabled", "extra_config", False,
        catalog_key="Bit 8 — DCC Handler Flags (instance)",
        notes="CONFIRM _CHECK_C semantics + whether dccFlagsEnabled is boolean or bitmask.",
    ),
    FlagFix(
        "DCCXPRESSCO_CHECK_O", "DCCXPRESSCO_BASE",
        "DCC Xpress CO", "DCC_XPRESS", 1, LOCATION, "DCCXpressCO", None, True,
        catalog_key="Bit 1 — DCC Xpress CO (location extra_function)",
        notes="Bit 1 location extra_function.",
    ),
    FlagFix(
        "DCCXPRESSCODT_CHECK_O", "DCCXPRESSCODT_BASE",
        "DCC Xpress CO DT", "DCC_XPRESS", 1, LOCATION, "DCCXpressCODT", None, True,
        catalog_key="Bit 1 — DCC Xpress CO Delayed Terminal (location extra_function)",
        notes="Bit 1 location extra_function.",
    ),
    FlagFix(
        "DCCXPRESSCOFALLBACK_CHECK_O", "DCCXPRESSCOFALLBACK_BASE",
        "DCC Xpress CO Fallback", "DCC_XPRESS", 1, LOCATION, "DCCXpressCOFallback", None, True,
        catalog_key="Bit 1 — DCC Xpress CO Fallback (location extra_function)",
        notes="Bit 1 location extra_function.",
    ),
    FlagFix(
        "DCCMERCHANT_NO_CHECK_O", "DCCMERCHANT_NO_BASE",
        "DCC Merchant Number", "MERCHANT", None, None, None, None, False,
        notes="Not fixable by spApplyDCCEnablementConfiguration.",
    ),
    FlagFix(
        "FIRMWARE_VERSION_CHECK", "FIRMWARE_VERSION_BASE",
        "Firmware Version", "FIRMWARE", 4, TERMINAL, None, None, False,
        catalog_key="Bit 4 — Firmware Package (terminal)",
        notes="Different SP (spManageEMVTerminalFirmwarePackage); heavier rollout — flag only.",
    ),
    FlagFix(
        "PRINTOUTTYPETEMPLATEDCC_CHECK_C", "PRINTOUTTYPETEMPLATEDCC_BASE",
        "DCC Receipt Template", "TEMPLATE", 16, INSTANCE, None, "receipt_config", True,
        catalog_key="Bit 16 — DCC Receipt Template (instance)",
        notes="Bit 16; template chosen per terminal (suggested from healthy peers, confirmed "
        "by the operator) — the row only holds the current, broken value.",
    ),
    FlagFix(
        "CONFIGDOWNLOAD_VERSION_CHECK_C", "CONFIGDOWNLOAD_VERSION_CHECK_BASE",
        "Config Download Version", "CONFIG", 2, TERMINAL, "ECB DCC", None, True,
        catalog_key="Bit 2 — Config Download Version (terminal)",
        notes="Bit 2; sets ECB DCC (every healthy terminal has it; broken ones have Standard).",
    ),
)

BY_CHECK: dict[str, FlagFix] = {f.check_column: f for f in FLAG_FIXES}

# Column tuples used by the snapshot / worklist / analytics builders.
TRACKED_CHECK_COLUMNS: tuple[str, ...] = tuple(f.check_column for f in FLAG_FIXES)
FIXABLE_CHECK_COLUMNS: tuple[str, ...] = tuple(f.check_column for f in FLAG_FIXES if f.fixable)
HANDLER_CHECK_COLUMNS: tuple[str, ...] = tuple(
    f.check_column for f in FLAG_FIXES if f.is_handler_object
)
NON_HANDLER_CHECK_COLUMNS: tuple[str, ...] = tuple(
    f.check_column for f in FLAG_FIXES if not f.is_handler_object
)

# Dimension columns exposed as worklist filters / analytics group-bys. Declared
# here so the query builders can validate a requested column against an allowlist.
DIMENSION_COLUMNS: tuple[str, ...] = (
    "COUNTRY_NAME",
    "REGION",
    "INDUSTRY_NAME",
    "ACQUIRER_NAME",
    "TERMINAL_BRAND_NAME",
    "TERMINAL_MODEL_NAME",
    "MERCHANT_NAME",
    "CUSTOMER_NAME",
    "BANK_MERCHANT_ID",
)

IDENTIFIER_COLUMNS: tuple[str, ...] = (
    "TERMINAL_IDENTIFIER",
    "INSTANCE_IDENTIFIER",
    "LOCATION_NO",
)


def resolve(check_column: str) -> FlagFix:
    """Return the :class:`FlagFix` for a check column or raise ``KeyError``."""
    return BY_CHECK[check_column]


def is_tracked(check_column: str) -> bool:
    return check_column in BY_CHECK
