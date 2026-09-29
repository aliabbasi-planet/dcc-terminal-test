-- =====================================================================
-- Seed DEV_CORE_AAB.DCC_REMEDIATION.FLAG_REFERENCE
-- GENERATED from src/dcc_console/remediation/ddl.py (build_seed_merge()).
-- Do not hand-edit; change the builder and regenerate. Idempotent, safe to re-run.
-- The app's "Initialise / verify my schema" button runs exactly these statements
-- for the connected user's own schema (+ the shared fix-log schema).
-- =====================================================================

MERGE INTO DEV_CORE_AAB.DCC_REMEDIATION.FLAG_REFERENCE tgt
USING (
    SELECT * FROM VALUES
        ('LOCATION_DCCENABLED_CHECK_C', 'Location DCC Enabled', 'CONFIG', NULL, NULL, NULL, NULL, FALSE, 'CONFIRM semantics with data owner; returned 0 broken in PROD.'),
        ('HANDLER_DCCENABLE_CHECK_O', 'Handler DCC Enable', 'HANDLER', 8, 'INSTANCE_IDENTIFIER', 'dccEnable', 'extra_config', TRUE, 'Bit 8 handler flag.'),
        ('HANDLER_DCCENABLECOMPLETION_CHECK_O', 'Handler DCC Completion', 'HANDLER', 8, 'INSTANCE_IDENTIFIER', 'dccEnableCompletion', 'extra_config', TRUE, 'Bit 8 handler flag.'),
        ('HANDLER_DCCENABLEAUTH_CHECK_O', 'Handler DCC Auth', 'HANDLER', 8, 'INSTANCE_IDENTIFIER', 'dccEnableAuth', 'extra_config', TRUE, 'Bit 8 handler flag.'),
        ('HANDLER_DCCENABLENFC_CHECK_O', 'Handler DCC NFC', 'HANDLER', 8, 'INSTANCE_IDENTIFIER', 'dccEnableNfc', 'extra_config', TRUE, 'Bit 8 handler flag.'),
        ('HANDLER_DCCENABLENFCSINGLETAP_CHECK_O', 'Handler DCC NFC Single Tap', 'HANDLER', 8, 'INSTANCE_IDENTIFIER', 'dccEnableNfcSingleTap', 'extra_config', TRUE, 'Bit 8 handler flag.'),
        ('DCCFLAGSENABLED_CHECK_C', 'DCC Flags Enabled', 'HANDLER', 8, 'INSTANCE_IDENTIFIER', 'dccFlagsEnabled', 'extra_config', FALSE, 'CONFIRM _CHECK_C semantics + whether dccFlagsEnabled is boolean or bitmask.'),
        ('DCCXPRESSCO_CHECK_O', 'DCC Xpress CO', 'DCC_XPRESS', 1, 'LOCATION_NO', 'DCCXpressCO', NULL, TRUE, 'Bit 1 location extra_function.'),
        ('DCCXPRESSCODT_CHECK_O', 'DCC Xpress CO DT', 'DCC_XPRESS', 1, 'LOCATION_NO', 'DCCXpressCODT', NULL, TRUE, 'Bit 1 location extra_function.'),
        ('DCCXPRESSCOFALLBACK_CHECK_O', 'DCC Xpress CO Fallback', 'DCC_XPRESS', 1, 'LOCATION_NO', 'DCCXpressCOFallback', NULL, TRUE, 'Bit 1 location extra_function.'),
        ('DCCMERCHANT_NO_CHECK_O', 'DCC Merchant Number', 'MERCHANT', NULL, NULL, NULL, NULL, FALSE, 'Not fixable by spApplyDCCEnablementConfiguration.'),
        ('FIRMWARE_VERSION_CHECK', 'Firmware Version', 'FIRMWARE', 4, 'TERMINAL_IDENTIFIER', NULL, NULL, FALSE, 'Different SP (spManageEMVTerminalFirmwarePackage); heavier rollout — flag only.'),
        ('PRINTOUTTYPETEMPLATEDCC_CHECK_C', 'DCC Receipt Template', 'TEMPLATE', 16, 'INSTANCE_IDENTIFIER', NULL, 'receipt_config', TRUE, 'Bit 16; template chosen per terminal (suggested from healthy peers, confirmed by the operator) — the row only holds the current, broken value.'),
        ('CONFIGDOWNLOAD_VERSION_CHECK_C', 'Config Download Version', 'CONFIG', 2, 'TERMINAL_IDENTIFIER', 'ECB DCC', NULL, TRUE, 'Bit 2; sets ECB DCC (every healthy terminal has it; broken ones have Standard).')
    AS v(CHECK_COLUMN, FLAG_NAME, FLAG_TYPE, FIX_BIT, TARGET_ID_KIND,
         FIX_VALUE, SP_COLUMN, FIXABLE, NOTES)
) src
ON tgt.CHECK_COLUMN = src.CHECK_COLUMN
WHEN MATCHED THEN UPDATE SET
    FLAG_NAME = src.FLAG_NAME, FLAG_TYPE = src.FLAG_TYPE, FIX_BIT = src.FIX_BIT,
    TARGET_ID_KIND = src.TARGET_ID_KIND, FIX_VALUE = src.FIX_VALUE,
    SP_COLUMN = src.SP_COLUMN, FIXABLE = src.FIXABLE, NOTES = src.NOTES
WHEN NOT MATCHED THEN INSERT
    (CHECK_COLUMN, FLAG_NAME, FLAG_TYPE, FIX_BIT, TARGET_ID_KIND, FIX_VALUE,
     SP_COLUMN, FIXABLE, NOTES)
    VALUES (src.CHECK_COLUMN, src.FLAG_NAME, src.FLAG_TYPE, src.FIX_BIT,
            src.TARGET_ID_KIND, src.FIX_VALUE, src.SP_COLUMN, src.FIXABLE, src.NOTES);
