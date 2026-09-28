-- =====================================================================
-- Seed DEV_CORE_AAB.DCC_REMEDIATION.FLAG_REFERENCE
-- Maps each maintenance-table CHECK column to the fix bit/procedure.
--
-- REVIEW BEFORE RUNNING. Idempotent MERGE (safe to re-run).
-- FIXABLE=FALSE rows are deliberately conservative: they need the data
-- owner (David) to confirm CHECK semantics / SP mapping before the UI
-- offers a one-click fix. Flip to TRUE only after confirmation.
-- =====================================================================

MERGE INTO DEV_CORE_AAB.DCC_REMEDIATION.FLAG_REFERENCE tgt
USING (
    SELECT * FROM VALUES
        -- CHECK_COLUMN, FLAG_NAME, FLAG_TYPE, FIX_BIT, TARGET_ID_KIND, FIX_VALUE, SP_COLUMN, FIXABLE, NOTES
        ('HANDLER_DCCENABLE_CHECK_O',             'Handler DCC Enable',          'HANDLER',    8,    'INSTANCE_IDENTIFIER', 'dccEnable',             'extra_config',   TRUE,  'Bit 8 handler flag'),
        ('HANDLER_DCCENABLEAUTH_CHECK_O',         'Handler DCC Auth',            'HANDLER',    8,    'INSTANCE_IDENTIFIER', 'dccEnableAuth',         'extra_config',   TRUE,  'Bit 8 handler flag'),
        ('HANDLER_DCCENABLECOMPLETION_CHECK_O',   'Handler DCC Completion',      'HANDLER',    8,    'INSTANCE_IDENTIFIER', 'dccEnableCompletion',   'extra_config',   TRUE,  'Bit 8 handler flag'),
        ('HANDLER_DCCENABLENFC_CHECK_O',          'Handler DCC NFC',             'HANDLER',    8,    'INSTANCE_IDENTIFIER', 'dccEnableNfc',          'extra_config',   TRUE,  'Bit 8 handler flag'),
        ('HANDLER_DCCENABLENFCSINGLETAP_CHECK_O', 'Handler DCC NFC Single Tap',  'HANDLER',    8,    'INSTANCE_IDENTIFIER', 'dccEnableNfcSingleTap', 'extra_config',   TRUE,  'Bit 8 handler flag'),
        ('DCCXPRESSCO_CHECK_O',                   'DCC Xpress CO',               'DCC_XPRESS', 1,    'LOCATION_NO',         'DCCXpressCO',           NULL,             TRUE,  'Bit 1 location extra_function'),
        ('DCCXPRESSCODT_CHECK_O',                 'DCC Xpress CO DT',            'DCC_XPRESS', 1,    'LOCATION_NO',         'DCCXpressCODT',         NULL,             TRUE,  'Bit 1 location extra_function'),
        ('DCCXPRESSCOFALLBACK_CHECK_O',           'DCC Xpress CO Fallback',      'DCC_XPRESS', 1,    'LOCATION_NO',         'DCCXpressCOFallBack',   NULL,             TRUE,  'Bit 1 location extra_function'),
        ('PRINTOUTTYPETEMPLATEDCC_CHECK_C',       'DCC Receipt Template',        'TEMPLATE',   16,   'INSTANCE_IDENTIFIER', NULL,                    'receipt_config', TRUE,  'Bit 16; value is the template, taken from the row'),
        ('CONFIGDOWNLOAD_VERSION_CHECK_C',        'Config Download Version',     'CONFIG',     2,    'TERMINAL_IDENTIFIER', NULL,                    NULL,             TRUE,  'Bit 2; value is the version, taken from the row'),
        ('DCCFLAGSENABLED_CHECK_C',               'DCC Flags Enabled',           'HANDLER',    8,    'INSTANCE_IDENTIFIER', 'dccFlagsEnabled',       'extra_config',   FALSE, 'CONFIRM: _CHECK_C semantics + whether dccFlagsEnabled is boolean or bitmask'),
        ('FIRMWARE_VERSION_CHECK',                'Firmware Version',            'FIRMWARE',   4,    'TERMINAL_IDENTIFIER', NULL,                    NULL,             FALSE, 'Different SP (spManageEMVTerminalFirmwarePackage); heavier rollout — flag only'),
        ('DCCMERCHANT_NO_CHECK_O',                'DCC Merchant Number',         'MERCHANT',   NULL, NULL,                  NULL,                    NULL,             FALSE, 'Not fixable by spApplyDCCEnablementConfiguration'),
        ('LOCATION_DCCENABLED_CHECK_C',           'Location DCC Enabled',        'CONFIG',     NULL, NULL,                  NULL,                    NULL,             FALSE, 'CONFIRM semantics: returned 0 broken of 52,196 in PROD')
    AS v(CHECK_COLUMN, FLAG_NAME, FLAG_TYPE, FIX_BIT, TARGET_ID_KIND, FIX_VALUE, SP_COLUMN, FIXABLE, NOTES)
) src
ON tgt.CHECK_COLUMN = src.CHECK_COLUMN
WHEN MATCHED THEN UPDATE SET
    FLAG_NAME = src.FLAG_NAME, FLAG_TYPE = src.FLAG_TYPE, FIX_BIT = src.FIX_BIT,
    TARGET_ID_KIND = src.TARGET_ID_KIND, FIX_VALUE = src.FIX_VALUE,
    SP_COLUMN = src.SP_COLUMN, FIXABLE = src.FIXABLE, NOTES = src.NOTES
WHEN NOT MATCHED THEN INSERT
    (CHECK_COLUMN, FLAG_NAME, FLAG_TYPE, FIX_BIT, TARGET_ID_KIND, FIX_VALUE, SP_COLUMN, FIXABLE, NOTES)
    VALUES (src.CHECK_COLUMN, src.FLAG_NAME, src.FLAG_TYPE, src.FIX_BIT, src.TARGET_ID_KIND,
            src.FIX_VALUE, src.SP_COLUMN, src.FIXABLE, src.NOTES);
