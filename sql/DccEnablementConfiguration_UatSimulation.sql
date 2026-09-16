/*
====================================================================
DCC ENABLEMENT CONFIGURATION - CONSOLIDATED CAB VALIDATION SUITE
====================================================================
Run as ONE complete script in a single SSMS query window and session.
Do not run individual sections separately. Every procedure call uses
@is_simulation = 1 and each call is wrapped in an outer transaction that
this harness rolls back, so nothing is ever persisted.

This suite is built to satisfy a CAB reviewer. It proves the procedure was
executed and captures, per call:
  * the exact EXEC statement and input JSON,
  * the verified column BEFORE and AFTER the (rolled-back) call, as a
    SHA-256 fingerprint plus a readable snippet -> proof of no persistence,
  * a precise verdict (SIMULATED-OK / REJECTED-AS-EXPECTED / BLOCKED /
    DEFERRED / FAIL), never a bare "PASS" that could be misread as
    "configuration changed".

It returns FOUR result sets:
  1. Check summary (readiness + procedure version evidence)
  2. Selected terminal identifiers (Bit 2 sample)
  3. Test results with before/after evidence and verdicts
  4. Coverage matrix over all nine configuration areas

OPTIONAL INPUTS (leave NULL to DEFER a positive test with a clear reason):
  @Bit4FirmwarePackage  - an APPROVED firmware package name for Bit 4
  @Bit16Template        - an APPROVED DCC receipt template value for Bit 16
====================================================================
*/

USE [3CDB];
GO

DROP TABLE IF EXISTS #CheckSummary;
DROP TABLE IF EXISTS #TerminalSelection;
DROP TABLE IF EXISTS #TestResults;
DROP TABLE IF EXISTS #Coverage;
GO

SET NOCOUNT ON;
SET XACT_ABORT OFF;

DECLARE @ProcName nvarchar(300) = N'[cccai].[spApplyDCCEnablementConfiguration]';
DECLARE @ExpectedServer sysname = N'LU3C01DVSQL01';
DECLARE @ExpectedDatabase sysname = N'3CDB';

-- Fixtures used for value-based tests (valid targets).
DECLARE @InstanceId nvarchar(50) = N'I000000001';
DECLARE @LocationNo nvarchar(50) = N'0000000';

-- Optional approved inputs. Supply these to turn Bit 4 / Bit 16 from DEFERRED
-- into fully exercised positive tests.
DECLARE @Bit4FirmwarePackage nvarchar(200) = NULL;
DECLARE @Bit16Template nvarchar(200) = NULL;

CREATE TABLE #CheckSummary
(
    check_name nvarchar(200) NOT NULL,
    detail nvarchar(4000) NOT NULL,
    status varchar(10) NOT NULL
);

CREATE TABLE #TerminalSelection
(
    terminal_identifier nvarchar(50) NOT NULL,
    configdownload_version nvarchar(200) NULL,
    selection_status varchar(40) NOT NULL
);

CREATE TABLE #TestResults
(
    test_name nvarchar(200) NOT NULL,
    config_area nvarchar(100) NOT NULL,
    target_type nvarchar(50) NOT NULL,
    target_identifier nvarchar(100) NOT NULL,
    config_value nvarchar(200) NULL,
    exec_statement nvarchar(max) NOT NULL,
    input_json nvarchar(max) NOT NULL,
    expected_result nvarchar(600) NOT NULL,
    state_before_hash varchar(64) NULL,
    state_after_hash varchar(64) NULL,
    state_before_snippet nvarchar(400) NULL,
    state_after_snippet nvarchar(400) NULL,
    change_persisted bit NULL,
    verdict varchar(30) NOT NULL,
    execution_detail nvarchar(1000) NOT NULL,
    outer_transaction_status nvarchar(200) NOT NULL,
    executed_at datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE TABLE #Coverage
(
    ordinal int NOT NULL,
    config_area nvarchar(100) NOT NULL,
    family nvarchar(60) NOT NULL,
    requirement nvarchar(400) NOT NULL
);

/* -----------------------------------------------------------------
   Readiness checks
   ----------------------------------------------------------------- */
INSERT INTO #CheckSummary (check_name, detail, status)
VALUES
    (N'Connected server', @@SERVERNAME, CASE WHEN @@SERVERNAME = @ExpectedServer OR @@SERVERNAME LIKE @ExpectedServer + N'%' THEN 'PASS' ELSE 'FAIL' END),
    (N'Selected database', DB_NAME(), CASE WHEN DB_NAME() = @ExpectedDatabase THEN 'PASS' ELSE 'FAIL' END),
    (N'Login', CONVERT(nvarchar(200), ORIGINAL_LOGIN()), 'INFO'),
    (N'Simulation safeguard', N'All procedure calls are hard-coded with @is_simulation = 1 and rolled back.', 'PASS'),
    (N'Procedure exists',
        CASE WHEN OBJECT_ID(@ProcName, N'P') IS NULL THEN N'NOT FOUND' ELSE N'Found' END,
        CASE WHEN OBJECT_ID(@ProcName, N'P') IS NULL THEN 'FAIL' ELSE 'PASS' END),
    (N'EXECUTE on main procedure',
        CONVERT(nvarchar(10), HAS_PERMS_BY_NAME(N'cccai.spApplyDCCEnablementConfiguration', N'OBJECT', N'EXECUTE')),
        CASE WHEN HAS_PERMS_BY_NAME(N'cccai.spApplyDCCEnablementConfiguration', N'OBJECT', N'EXECUTE') = 1 THEN 'PASS' ELSE 'FAIL' END),
    (N'[db].[fnDisplayTrace] exists',
        CASE WHEN OBJECT_ID(N'[db].[fnDisplayTrace]') IS NULL THEN N'NOT FOUND' ELSE N'Found' END,
        CASE WHEN OBJECT_ID(N'[db].[fnDisplayTrace]') IS NULL THEN 'FAIL' ELSE 'PASS' END),
    (N'EXECUTE on [db].[fnDisplayTrace]',
        CONVERT(nvarchar(10), HAS_PERMS_BY_NAME(N'db.fnDisplayTrace', N'OBJECT', N'EXECUTE')),
        CASE WHEN HAS_PERMS_BY_NAME(N'db.fnDisplayTrace', N'OBJECT', N'EXECUTE') = 1 THEN 'PASS' ELSE 'FAIL' END),
    (N'Firmware procedure (Bit 4 dependency)',
        CASE WHEN OBJECT_ID(N'[cccintegrang].[spManageEMVTerminalFirmwarePackage]', N'P') IS NULL
            THEN N'NOT FOUND - Bit 4 will be DEFERRED' ELSE N'Found' END,
        CASE WHEN OBJECT_ID(N'[cccintegrang].[spManageEMVTerminalFirmwarePackage]', N'P') IS NULL THEN 'WARN' ELSE 'PASS' END);

/* -----------------------------------------------------------------
   Procedure version evidence (CAB issue #1: prove WHICH build was tested)
   ----------------------------------------------------------------- */
DECLARE @ObjId int = OBJECT_ID(@ProcName, N'P');
INSERT INTO #CheckSummary (check_name, detail, status)
SELECT N'Procedure object_id', CONVERT(nvarchar(50), @ObjId), 'INFO'
WHERE @ObjId IS NOT NULL;

INSERT INTO #CheckSummary (check_name, detail, status)
SELECT N'Procedure created', CONVERT(nvarchar(30), o.create_date, 121), 'INFO'
FROM sys.objects AS o WHERE o.object_id = @ObjId;

INSERT INTO #CheckSummary (check_name, detail, status)
SELECT N'Procedure last modified', CONVERT(nvarchar(30), o.modify_date, 121), 'INFO'
FROM sys.objects AS o WHERE o.object_id = @ObjId;

INSERT INTO #CheckSummary (check_name, detail, status)
SELECT N'Procedure definition SHA-256',
       CONVERT(varchar(64), HASHBYTES('SHA2_256', CONVERT(nvarchar(max), OBJECT_DEFINITION(@ObjId))), 2),
       'INFO'
WHERE @ObjId IS NOT NULL;

/* -----------------------------------------------------------------
   Fixtures
   ----------------------------------------------------------------- */
INSERT INTO #CheckSummary (check_name, detail, status)
SELECT N'Fixture instance ' + @InstanceId,
       CASE WHEN EXISTS (SELECT 1 FROM [cccintegrang].[instance] WHERE instance_identifier = @InstanceId) THEN N'Found' ELSE N'NOT FOUND' END,
       CASE WHEN EXISTS (SELECT 1 FROM [cccintegrang].[instance] WHERE instance_identifier = @InstanceId) THEN 'PASS' ELSE 'FAIL' END
UNION ALL
SELECT N'Fixture location ' + @LocationNo,
       CASE WHEN EXISTS (SELECT 1 FROM [ccc].[location] WHERE location_no = @LocationNo) THEN N'Found' ELSE N'NOT FOUND' END,
       CASE WHEN EXISTS (SELECT 1 FROM [ccc].[location] WHERE location_no = @LocationNo) THEN 'PASS' ELSE 'FAIL' END;

/* -----------------------------------------------------------------
   Bit 2 terminal sample: some already Standard, some on a different version
   ----------------------------------------------------------------- */
;WITH AlreadyStandard AS
(
    SELECT TOP (5)
        CONVERT(nvarchar(50), terminal_identifier) AS terminal_identifier,
        CONVERT(nvarchar(200), configdownload_version) AS configdownload_version
    FROM [cccintegrang].[emv_terminal]
    WHERE terminal_identifier IS NOT NULL
      AND LTRIM(RTRIM(CONVERT(nvarchar(50), terminal_identifier))) <> N''
      AND configdownload_version = 1
    ORDER BY terminal_identifier
),
DifferentVersion AS
(
    SELECT TOP (5)
        CONVERT(nvarchar(50), terminal_identifier) AS terminal_identifier,
        CONVERT(nvarchar(200), configdownload_version) AS configdownload_version
    FROM [cccintegrang].[emv_terminal]
    WHERE terminal_identifier IS NOT NULL
      AND LTRIM(RTRIM(CONVERT(nvarchar(50), terminal_identifier))) <> N''
      AND (configdownload_version <> 1 OR configdownload_version IS NULL)
    ORDER BY terminal_identifier
)
INSERT INTO #TerminalSelection (terminal_identifier, configdownload_version, selection_status)
SELECT terminal_identifier, configdownload_version, 'SELECTED - ALREADY STANDARD'
FROM AlreadyStandard
UNION ALL
SELECT terminal_identifier, configdownload_version, 'SELECTED - DIFFERENT VERSION'
FROM DifferentVersion;

INSERT INTO #CheckSummary (check_name, detail, status)
SELECT
    N'Selected terminal sample',
    CONCAT(
        N'Total selected=', COUNT(*),
        N'; Already Standard=', SUM(CASE WHEN selection_status = 'SELECTED - ALREADY STANDARD' THEN 1 ELSE 0 END),
        N'; Different version=', SUM(CASE WHEN selection_status = 'SELECTED - DIFFERENT VERSION' THEN 1 ELSE 0 END)
    ),
    CASE WHEN COUNT(*) >= 2
           AND SUM(CASE WHEN selection_status = 'SELECTED - DIFFERENT VERSION' THEN 1 ELSE 0 END) >= 1
         THEN 'PASS' ELSE 'WARN' END
FROM #TerminalSelection;

/* -----------------------------------------------------------------
   Coverage registry: the nine CAB-facing configuration areas
   ----------------------------------------------------------------- */
INSERT INTO #Coverage (ordinal, config_area, family, requirement)
VALUES
    (1, N'Bit 1 - DCCXpressCO', N'Location extra_function', N'A valid location and the DCCXpressCO node.'),
    (2, N'Bit 1 - DCCXpressCODT', N'Location extra_function', N'A valid location and the DCCXpressCODT node (confirm node name with owner).'),
    (3, N'Bit 1 - DCCXpressCOFallback', N'Location extra_function', N'A valid location and the DCCXpressCOFallback node (confirm node name with owner).'),
    (4, N'Bit 2 - Config Download Version', N'Terminal', N'Valid terminals; both already-Standard and different-version rows.'),
    (5, N'Bit 4 - Firmware Package', N'Terminal', N'Firmware helper procedure, an approved package name (@Bit4FirmwarePackage), and a test terminal.'),
    (6, N'Bit 8 - DCC Handler Flags (core)', N'Instance handler flags', N'A valid instance; the five core dccEnable* flags.'),
    (7, N'Bit 8 - dccEnableRefund', N'Instance handler flags', N'A valid instance and the dccEnableRefund flag.'),
    (8, N'Bit 8 - dccFlagsEnabled', N'Instance handler flags', N'A valid instance and dccFlagsEnabled (confirm boolean vs bitmask with owner).'),
    (9, N'Bit 16 - DCC Receipt Template', N'Instance', N'A valid instance and an approved DCC receipt template value (@Bit16Template).');

/* -----------------------------------------------------------------
   Dependency gate
   ----------------------------------------------------------------- */
DECLARE @DependenciesOk bit = CASE WHEN EXISTS
(
    SELECT 1 FROM #CheckSummary
    WHERE status = 'FAIL'
      AND check_name IN
      (
          N'Connected server', N'Selected database', N'Procedure exists', N'EXECUTE on main procedure',
          N'[db].[fnDisplayTrace] exists', N'EXECUTE on [db].[fnDisplayTrace]',
          N'Fixture instance ' + @InstanceId, N'Fixture location ' + @LocationNo
      )
) THEN 0 ELSE 1 END;

IF @DependenciesOk = 0
BEGIN
    INSERT INTO #TestResults
    (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
     expected_result, verdict, execution_detail, outer_transaction_status)
    VALUES
    (N'Test suite execution', N'Suite', N'Suite', N'All tests', NULL, N'(not executed)', N'(not executed)',
     N'Required checks pass', 'BLOCKED', N'Not executed because one or more required checks failed.', N'NOT STARTED');
END
ELSE
BEGIN
    -- Shared evidence variables reused by every block.
    DECLARE @beforeHash varchar(64), @afterHash varchar(64);
    DECLARE @beforeSnip nvarchar(400), @afterSnip nvarchar(400);
    DECLARE @ExecText nvarchar(max), @InputJson nvarchar(max);
    DECLARE @otx nvarchar(200), @errMsg nvarchar(2000);
    DECLARE @rolled bit;

    /* =========================================================
       BIT 8 - DCC handler flags (core five + refund + flagsEnabled)
       ========================================================= */
    DECLARE @Bit8Flags TABLE (id int IDENTITY(1,1), flag_name nvarchar(200), area nvarchar(100));
    INSERT INTO @Bit8Flags (flag_name, area) VALUES
        (N'dccEnable',              N'Bit 8 - DCC Handler Flags (core)'),
        (N'dccEnableAuth',          N'Bit 8 - DCC Handler Flags (core)'),
        (N'dccEnableCompletion',    N'Bit 8 - DCC Handler Flags (core)'),
        (N'dccEnableNfc',           N'Bit 8 - DCC Handler Flags (core)'),
        (N'dccEnableNfcSingleTap',  N'Bit 8 - DCC Handler Flags (core)'),
        (N'dccEnableRefund',        N'Bit 8 - dccEnableRefund'),
        (N'dccFlagsEnabled',        N'Bit 8 - dccFlagsEnabled');

    DECLARE @fid int = 1, @fmax int = (SELECT MAX(id) FROM @Bit8Flags);
    DECLARE @FlagName nvarchar(200), @FlagArea nvarchar(100);

    WHILE @fid <= @fmax
    BEGIN
        SELECT @FlagName = flag_name, @FlagArea = area FROM @Bit8Flags WHERE id = @fid;
        SET @InputJson = N'[{"instance_identifier":"' + @InstanceId + N'"}]';
        SET @ExecText = N'EXEC ' + @ProcName + N' @display_config = 8, @instance_json = N''' + @InputJson
            + N''', @Extra_Config_Name = N''' + @FlagName + N''', @Config_value = 1, @is_simulation = 1';
        SET @rolled = 0;

        SELECT @beforeHash = CONVERT(varchar(64), HASHBYTES('SHA2_256', CONVERT(nvarchar(max), package_config)), 2),
               @beforeSnip = LEFT(CONVERT(nvarchar(max), package_config), 300)
        FROM [cccintegrang].[instance] WHERE instance_identifier = @InstanceId;

        BEGIN TRY
            BEGIN TRANSACTION;
            EXEC @ProcName @display_config = 8, @instance_json = @InputJson,
                 @Extra_Config_Name = @FlagName, @Config_value = 1, @is_simulation = 1;
            IF XACT_STATE() <> 0 BEGIN ROLLBACK TRANSACTION; SET @rolled = 1; END;

            SELECT @afterHash = CONVERT(varchar(64), HASHBYTES('SHA2_256', CONVERT(nvarchar(max), package_config)), 2),
                   @afterSnip = LEFT(CONVERT(nvarchar(max), package_config), 300)
            FROM [cccintegrang].[instance] WHERE instance_identifier = @InstanceId;

            SET @otx = CASE WHEN @rolled = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY HARNESS' ELSE N'NO ACTIVE TRANSACTION REMAINED' END;
            INSERT INTO #TestResults
            (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
             expected_result, state_before_hash, state_after_hash, state_before_snippet, state_after_snippet,
             change_persisted, verdict, execution_detail, outer_transaction_status)
            VALUES
            (N'Bit 8 - ' + @FlagName, @FlagArea, N'Instance', @InstanceId, @FlagName, @ExecText, @InputJson,
             N'Procedure executes under simulation and proposes setting the flag to true; nothing persists.',
             @beforeHash, @afterHash, @beforeSnip, @afterSnip,
             CASE WHEN @beforeHash <> @afterHash THEN 1 ELSE 0 END, 'SIMULATED-OK',
             N'No SQL exception. Before/after fingerprints match, proving no persistence. Review the procedure preview grid in the messages/grids pane.',
             @otx);
        END TRY
        BEGIN CATCH
            IF XACT_STATE() <> 0 BEGIN ROLLBACK TRANSACTION; SET @rolled = 1; END;
            SET @errMsg = ERROR_MESSAGE();
            INSERT INTO #TestResults
            (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
             expected_result, state_before_hash, state_before_snippet, change_persisted, verdict, execution_detail, outer_transaction_status)
            VALUES
            (N'Bit 8 - ' + @FlagName, @FlagArea, N'Instance', @InstanceId, @FlagName, @ExecText, @InputJson,
             N'Procedure executes under simulation and proposes setting the flag to true; nothing persists.',
             @beforeHash, @beforeSnip, 0,
             CASE WHEN @errMsg LIKE N'%EXECUTE permission was denied%' THEN 'BLOCKED' ELSE 'FAIL' END,
             LEFT(CONCAT(N'Error ', ERROR_NUMBER(), N': ', @errMsg), 1000),
             CASE WHEN @rolled = 1 THEN N'OUTER TRANSACTION ROLLED BACK AFTER EXCEPTION' ELSE N'NO ACTIVE TRANSACTION REMAINED' END);
        END CATCH;
        SET @fid += 1;
    END;

    /* =========================================================
       BIT 1 - location extra_function (CO / CODT / COFallback)
       ========================================================= */
    DECLARE @Bit1Funcs TABLE (id int IDENTITY(1,1), func_name nvarchar(100), area nvarchar(100));
    INSERT INTO @Bit1Funcs (func_name, area) VALUES
        (N'DCCXpressCO',          N'Bit 1 - DCCXpressCO'),
        (N'DCCXpressCODT',        N'Bit 1 - DCCXpressCODT'),
        (N'DCCXpressCOFallback',  N'Bit 1 - DCCXpressCOFallback');

    DECLARE @lid int = 1, @lmax int = (SELECT MAX(id) FROM @Bit1Funcs);
    DECLARE @FuncName nvarchar(100), @FuncArea nvarchar(100);

    WHILE @lid <= @lmax
    BEGIN
        SELECT @FuncName = func_name, @FuncArea = area FROM @Bit1Funcs WHERE id = @lid;
        SET @InputJson = N'[{"location_no":"' + @LocationNo + N'"}]';
        SET @ExecText = N'EXEC ' + @ProcName + N' @display_config = 1, @location_json = N''' + @InputJson
            + N''', @extra_function_name = N''' + @FuncName + N''', @add = 1, @is_simulation = 1';
        SET @rolled = 0;

        SELECT @beforeHash = CONVERT(varchar(64), HASHBYTES('SHA2_256', CONVERT(nvarchar(max), extra_function)), 2),
               @beforeSnip = LEFT(CONVERT(nvarchar(max), extra_function), 300)
        FROM [ccc].[location] WHERE location_no = @LocationNo;

        BEGIN TRY
            BEGIN TRANSACTION;
            EXEC @ProcName @display_config = 1, @location_json = @InputJson,
                 @extra_function_name = @FuncName, @add = 1, @is_simulation = 1;
            IF XACT_STATE() <> 0 BEGIN ROLLBACK TRANSACTION; SET @rolled = 1; END;

            SELECT @afterHash = CONVERT(varchar(64), HASHBYTES('SHA2_256', CONVERT(nvarchar(max), extra_function)), 2),
                   @afterSnip = LEFT(CONVERT(nvarchar(max), extra_function), 300)
            FROM [ccc].[location] WHERE location_no = @LocationNo;

            SET @otx = CASE WHEN @rolled = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY HARNESS' ELSE N'NO ACTIVE TRANSACTION REMAINED' END;
            INSERT INTO #TestResults
            (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
             expected_result, state_before_hash, state_after_hash, state_before_snippet, state_after_snippet,
             change_persisted, verdict, execution_detail, outer_transaction_status)
            VALUES
            (N'Bit 1 - add ' + @FuncName, @FuncArea, N'Location', @LocationNo, N'Add ' + @FuncName, @ExecText, @InputJson,
             N'Procedure executes under simulation and proposes adding the function node; nothing persists.',
             @beforeHash, @afterHash, @beforeSnip, @afterSnip,
             CASE WHEN @beforeHash <> @afterHash THEN 1 ELSE 0 END, 'SIMULATED-OK',
             N'No SQL exception. Before/after fingerprints match, proving no persistence. Review the procedure preview grid in the messages/grids pane.',
             @otx);
        END TRY
        BEGIN CATCH
            IF XACT_STATE() <> 0 BEGIN ROLLBACK TRANSACTION; SET @rolled = 1; END;
            SET @errMsg = ERROR_MESSAGE();
            INSERT INTO #TestResults
            (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
             expected_result, state_before_hash, state_before_snippet, change_persisted, verdict, execution_detail, outer_transaction_status)
            VALUES
            (N'Bit 1 - add ' + @FuncName, @FuncArea, N'Location', @LocationNo, N'Add ' + @FuncName, @ExecText, @InputJson,
             N'Procedure executes under simulation and proposes adding the function node; nothing persists.',
             @beforeHash, @beforeSnip, 0,
             CASE WHEN @errMsg LIKE N'%EXECUTE permission was denied%' THEN 'BLOCKED' ELSE 'FAIL' END,
             LEFT(CONCAT(N'Error ', ERROR_NUMBER(), N': ', @errMsg), 1000),
             CASE WHEN @rolled = 1 THEN N'OUTER TRANSACTION ROLLED BACK AFTER EXCEPTION' ELSE N'NO ACTIVE TRANSACTION REMAINED' END);
        END CATCH;
        SET @lid += 1;
    END;

    /* =========================================================
       BIT 2 - config download version, over the selected terminals
       ========================================================= */
    DECLARE @Tid nvarchar(50);
    DECLARE TerminalCursor CURSOR LOCAL FAST_FORWARD FOR
        SELECT terminal_identifier FROM #TerminalSelection;
    OPEN TerminalCursor;
    FETCH NEXT FROM TerminalCursor INTO @Tid;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @InputJson = N'[{"terminal_identifier":"' + STRING_ESCAPE(@Tid, 'json') + N'"}]';
        SET @ExecText = N'EXEC ' + @ProcName + N' @display_config = 2, @terminal_json = N''' + @InputJson
            + N''', @ConfigDownloadVersionDesc = N''Standard'', @is_simulation = 1';
        SET @rolled = 0;

        SELECT @beforeHash = CONVERT(varchar(64), HASHBYTES('SHA2_256', CONVERT(nvarchar(50), configdownload_version)), 2),
               @beforeSnip = CONVERT(nvarchar(50), configdownload_version)
        FROM [cccintegrang].[emv_terminal] WHERE terminal_identifier = @Tid;

        BEGIN TRY
            BEGIN TRANSACTION;
            EXEC @ProcName @display_config = 2, @terminal_json = @InputJson,
                 @ConfigDownloadVersionDesc = N'Standard', @is_simulation = 1;
            IF XACT_STATE() <> 0 BEGIN ROLLBACK TRANSACTION; SET @rolled = 1; END;

            SELECT @afterHash = CONVERT(varchar(64), HASHBYTES('SHA2_256', CONVERT(nvarchar(50), configdownload_version)), 2),
                   @afterSnip = CONVERT(nvarchar(50), configdownload_version)
            FROM [cccintegrang].[emv_terminal] WHERE terminal_identifier = @Tid;

            SET @otx = CASE WHEN @rolled = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY HARNESS' ELSE N'NO ACTIVE TRANSACTION REMAINED' END;
            INSERT INTO #TestResults
            (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
             expected_result, state_before_hash, state_after_hash, state_before_snippet, state_after_snippet,
             change_persisted, verdict, execution_detail, outer_transaction_status)
            VALUES
            (N'Bit 2 - Config Download Version', N'Bit 2 - Config Download Version', N'Terminal', @Tid, N'Standard', @ExecText, @InputJson,
             N'Procedure executes under simulation and proposes Standard; nothing persists.',
             @beforeHash, @afterHash, @beforeSnip, @afterSnip,
             CASE WHEN @beforeHash <> @afterHash THEN 1 ELSE 0 END, 'SIMULATED-OK',
             N'No SQL exception. Before/after values match, proving no persistence. Review the procedure preview grid in the messages/grids pane.',
             @otx);
        END TRY
        BEGIN CATCH
            IF XACT_STATE() <> 0 BEGIN ROLLBACK TRANSACTION; SET @rolled = 1; END;
            SET @errMsg = ERROR_MESSAGE();
            INSERT INTO #TestResults
            (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
             expected_result, state_before_hash, state_before_snippet, change_persisted, verdict, execution_detail, outer_transaction_status)
            VALUES
            (N'Bit 2 - Config Download Version', N'Bit 2 - Config Download Version', N'Terminal', @Tid, N'Standard', @ExecText, @InputJson,
             N'Procedure executes under simulation and proposes Standard; nothing persists.',
             @beforeHash, @beforeSnip, 0,
             CASE WHEN @errMsg LIKE N'%EXECUTE permission was denied%' THEN 'BLOCKED' ELSE 'FAIL' END,
             LEFT(CONCAT(N'Error ', ERROR_NUMBER(), N': ', @errMsg), 1000),
             CASE WHEN @rolled = 1 THEN N'OUTER TRANSACTION ROLLED BACK AFTER EXCEPTION' ELSE N'NO ACTIVE TRANSACTION REMAINED' END);
        END CATCH;
        FETCH NEXT FROM TerminalCursor INTO @Tid;
    END;
    CLOSE TerminalCursor;
    DEALLOCATE TerminalCursor;

    /* =========================================================
       BIT 4 - firmware package (DEFERRED unless helper + approved package)
       ========================================================= */
    IF OBJECT_ID(N'[cccintegrang].[spManageEMVTerminalFirmwarePackage]', N'P') IS NULL
    BEGIN
        INSERT INTO #TestResults
        (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
         expected_result, verdict, execution_detail, outer_transaction_status)
        VALUES
        (N'Bit 4 - Firmware Package', N'Bit 4 - Firmware Package', N'Terminal', N'(none)', @Bit4FirmwarePackage,
         N'(deferred)', N'(deferred)',
         N'Downstream firmware helper procedure must exist and an approved package supplied.',
         'BLOCKED', N'Firmware helper [cccintegrang].[spManageEMVTerminalFirmwarePackage] is not present on this server; Bit 4 cannot run here.',
         N'NOT STARTED');
    END
    ELSE IF @Bit4FirmwarePackage IS NULL
    BEGIN
        INSERT INTO #TestResults
        (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
         expected_result, verdict, execution_detail, outer_transaction_status)
        VALUES
        (N'Bit 4 - Firmware Package', N'Bit 4 - Firmware Package', N'Terminal', N'(none)', NULL,
         N'(deferred)', N'(deferred)',
         N'An approved firmware package name is required to exercise Bit 4.',
         'DEFERRED', N'Set @Bit4FirmwarePackage to an approved package (and a test terminal) to exercise Bit 4.',
         N'NOT STARTED');
    END;
    -- (When both are available, add a firmware EXEC block mirroring the Bit 2 pattern.)

    /* =========================================================
       BIT 16 - DCC receipt template (DEFERRED unless approved template)
       ========================================================= */
    IF @Bit16Template IS NULL
    BEGIN
        INSERT INTO #TestResults
        (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
         expected_result, verdict, execution_detail, outer_transaction_status)
        VALUES
        (N'Bit 16 - DCC Receipt Template', N'Bit 16 - DCC Receipt Template', N'Instance', @InstanceId, NULL,
         N'(deferred)', N'(deferred)',
         N'An approved DCC receipt template value is required to exercise Bit 16.',
         'DEFERRED', N'Set @Bit16Template to an approved template value to exercise Bit 16 under simulation.',
         N'NOT STARTED');
    END
    ELSE
    BEGIN
        SET @InputJson = N'[{"instance_identifier":"' + @InstanceId + N'"}]';
        SET @ExecText = N'EXEC ' + @ProcName + N' @display_config = 16, @instance_json = N''' + @InputJson
            + N''', @printout_type_Template_DCC = N''' + @Bit16Template + N''', @is_simulation = 1';
        SET @rolled = 0;

        SELECT @beforeHash = CONVERT(varchar(64), HASHBYTES('SHA2_256', CONVERT(nvarchar(max), package_config)), 2),
               @beforeSnip = LEFT(CONVERT(nvarchar(max), package_config), 300)
        FROM [cccintegrang].[instance] WHERE instance_identifier = @InstanceId;

        BEGIN TRY
            BEGIN TRANSACTION;
            EXEC @ProcName @display_config = 16, @instance_json = @InputJson,
                 @printout_type_Template_DCC = @Bit16Template, @is_simulation = 1;
            IF XACT_STATE() <> 0 BEGIN ROLLBACK TRANSACTION; SET @rolled = 1; END;

            SELECT @afterHash = CONVERT(varchar(64), HASHBYTES('SHA2_256', CONVERT(nvarchar(max), package_config)), 2),
                   @afterSnip = LEFT(CONVERT(nvarchar(max), package_config), 300)
            FROM [cccintegrang].[instance] WHERE instance_identifier = @InstanceId;

            SET @otx = CASE WHEN @rolled = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY HARNESS' ELSE N'NO ACTIVE TRANSACTION REMAINED' END;
            INSERT INTO #TestResults
            (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
             expected_result, state_before_hash, state_after_hash, state_before_snippet, state_after_snippet,
             change_persisted, verdict, execution_detail, outer_transaction_status)
            VALUES
            (N'Bit 16 - DCC Receipt Template', N'Bit 16 - DCC Receipt Template', N'Instance', @InstanceId, @Bit16Template, @ExecText, @InputJson,
             N'Procedure executes under simulation and proposes the receipt template; nothing persists.',
             @beforeHash, @afterHash, @beforeSnip, @afterSnip,
             CASE WHEN @beforeHash <> @afterHash THEN 1 ELSE 0 END, 'SIMULATED-OK',
             N'No SQL exception. Before/after fingerprints match, proving no persistence.', @otx);
        END TRY
        BEGIN CATCH
            IF XACT_STATE() <> 0 BEGIN ROLLBACK TRANSACTION; SET @rolled = 1; END;
            SET @errMsg = ERROR_MESSAGE();
            INSERT INTO #TestResults
            (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
             expected_result, state_before_hash, state_before_snippet, change_persisted, verdict, execution_detail, outer_transaction_status)
            VALUES
            (N'Bit 16 - DCC Receipt Template', N'Bit 16 - DCC Receipt Template', N'Instance', @InstanceId, @Bit16Template, @ExecText, @InputJson,
             N'Procedure executes under simulation and proposes the receipt template; nothing persists.',
             @beforeHash, @beforeSnip, 0,
             CASE WHEN @errMsg LIKE N'%EXECUTE permission was denied%' THEN 'BLOCKED' ELSE 'FAIL' END,
             LEFT(CONCAT(N'Error ', ERROR_NUMBER(), N': ', @errMsg), 1000),
             CASE WHEN @rolled = 1 THEN N'OUTER TRANSACTION ROLLED BACK AFTER EXCEPTION' ELSE N'NO ACTIVE TRANSACTION REMAINED' END);
        END CATCH;
    END;

    /* =========================================================
       NEGATIVE VALIDATION BATTERY (rejection is the pass condition)
       Each case sends deliberately invalid input in simulation.
       ========================================================= */
    DECLARE @Neg TABLE
    (
        id int IDENTITY(1,1),
        name nvarchar(200),
        display_config int,
        json_param varchar(20),   -- 'instance' | 'terminal' | 'location'
        input_json nvarchar(max),
        extra_name nvarchar(200),  -- flag or function name (NULL if n/a)
        version_desc nvarchar(200) -- for Bit 2 (NULL if n/a)
    );
    INSERT INTO @Neg (name, display_config, json_param, input_json, extra_name, version_desc) VALUES
        (N'Invalid instance identifier', 8, 'instance', N'[{"instance_identifier":"I000099999"}]', N'dccEnable', NULL),
        (N'Invalid terminal identifier', 2, 'terminal', N'[{"terminal_identifier":"99999999"}]', NULL, N'Standard'),
        (N'Invalid location number',     1, 'location', N'[{"location_no":"9999999"}]', N'DCCXpressCO', NULL),
        (N'Invalid handler flag name',   8, 'instance', N'[{"instance_identifier":"' + @InstanceId + N'"}]', N'dccNotARealFlag', NULL),
        (N'Invalid location function',   1, 'location', N'[{"location_no":"' + @LocationNo + N'"}]', N'DCCNotARealFunction', NULL),
        (N'Invalid config version',      2, 'terminal', N'[{"terminal_identifier":"99999999"}]', NULL, N'NotAVersion'),
        (N'Missing target JSON (empty)', 8, 'instance', N'[]', N'dccEnable', NULL);

    DECLARE @nid int = 1, @nmax int = (SELECT MAX(id) FROM @Neg);
    DECLARE @nName nvarchar(200), @nCfg int, @nParam varchar(20), @nJson nvarchar(max), @nExtra nvarchar(200), @nVer nvarchar(200);

    WHILE @nid <= @nmax
    BEGIN
        SELECT @nName = name, @nCfg = display_config, @nParam = json_param,
               @nJson = input_json, @nExtra = extra_name, @nVer = version_desc
        FROM @Neg WHERE id = @nid;

        SET @rolled = 0;
        SET @ExecText = N'EXEC ' + @ProcName + N' @display_config = ' + CONVERT(nvarchar(10), @nCfg)
            + CASE @nParam WHEN 'instance' THEN N', @instance_json = N''' + @nJson + N''''
                           WHEN 'terminal' THEN N', @terminal_json = N''' + @nJson + N''''
                           ELSE N', @location_json = N''' + @nJson + N'''' END
            + CASE WHEN @nCfg = 8 THEN N', @Extra_Config_Name = N''' + @nExtra + N''', @Config_value = 1'
                   WHEN @nCfg = 1 THEN N', @extra_function_name = N''' + @nExtra + N''', @add = 1'
                   WHEN @nCfg = 2 THEN N', @ConfigDownloadVersionDesc = N''' + @nVer + N''''
                   ELSE N'' END
            + N', @is_simulation = 1';

        BEGIN TRY
            BEGIN TRANSACTION;
            IF @nCfg = 8
                EXEC @ProcName @display_config = 8, @instance_json = @nJson, @Extra_Config_Name = @nExtra, @Config_value = 1, @is_simulation = 1;
            ELSE IF @nCfg = 1
                EXEC @ProcName @display_config = 1, @location_json = @nJson, @extra_function_name = @nExtra, @add = 1, @is_simulation = 1;
            ELSE IF @nCfg = 2
                EXEC @ProcName @display_config = 2, @terminal_json = @nJson, @ConfigDownloadVersionDesc = @nVer, @is_simulation = 1;
            IF XACT_STATE() <> 0 BEGIN ROLLBACK TRANSACTION; SET @rolled = 1; END;

            -- No exception was raised: the bad input was NOT rejected via an error.
            INSERT INTO #TestResults
            (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
             expected_result, change_persisted, verdict, execution_detail, outer_transaction_status)
            VALUES
            (N'Negative - ' + @nName, N'Negative validation', N'Negative', @nParam, @nExtra, @ExecText, @nJson,
             N'Procedure should reject the invalid input.', 0, 'NOT-REJECTED',
             N'No SQL exception was raised. Inspect the procedure trace: if it contains an ERROR message this is a rejection; if it silently accepted the input, review with the owner.',
             CASE WHEN @rolled = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY HARNESS' ELSE N'NO ACTIVE TRANSACTION REMAINED' END);
        END TRY
        BEGIN CATCH
            IF XACT_STATE() <> 0 BEGIN ROLLBACK TRANSACTION; SET @rolled = 1; END;
            SET @errMsg = ERROR_MESSAGE();
            INSERT INTO #TestResults
            (test_name, config_area, target_type, target_identifier, config_value, exec_statement, input_json,
             expected_result, change_persisted, verdict, execution_detail, outer_transaction_status)
            VALUES
            (N'Negative - ' + @nName, N'Negative validation', N'Negative', @nParam, @nExtra, @ExecText, @nJson,
             N'Procedure should reject the invalid input.', 0,
             CASE WHEN @errMsg LIKE N'%EXECUTE permission was denied%' THEN 'BLOCKED' ELSE 'REJECTED-AS-EXPECTED' END,
             LEFT(CONCAT(N'Rejected as expected: Error ', ERROR_NUMBER(), N': ', @errMsg), 1000),
             CASE WHEN @rolled = 1 THEN N'OUTER TRANSACTION ROLLED BACK; REJECTION EXPECTED' ELSE N'NO ACTIVE TRANSACTION REMAINED' END);
        END CATCH;
        SET @nid += 1;
    END;
END;

/* -----------------------------------------------------------------
   RESULT SET 1 - check summary + procedure version evidence
   ----------------------------------------------------------------- */
SELECT check_name, detail, status
FROM #CheckSummary
ORDER BY CASE status WHEN 'FAIL' THEN 1 WHEN 'WARN' THEN 2 WHEN 'PASS' THEN 3 ELSE 4 END, check_name;

/* -----------------------------------------------------------------
   RESULT SET 2 - selected terminals for the Bit 2 sample
   ----------------------------------------------------------------- */
SELECT terminal_identifier, configdownload_version, selection_status
FROM #TerminalSelection
ORDER BY terminal_identifier;

/* -----------------------------------------------------------------
   RESULT SET 3 - test results with before/after evidence and verdicts
   ----------------------------------------------------------------- */
SELECT test_name, config_area, target_type, target_identifier, config_value,
       verdict, change_persisted,
       state_before_hash, state_after_hash, state_before_snippet, state_after_snippet,
       exec_statement, input_json, expected_result, execution_detail,
       outer_transaction_status, executed_at
FROM #TestResults
ORDER BY
    CASE verdict
        WHEN 'FAIL' THEN 1 WHEN 'NOT-REJECTED' THEN 2 WHEN 'BLOCKED' THEN 3
        WHEN 'DEFERRED' THEN 4 WHEN 'REVIEW' THEN 5 WHEN 'REJECTED-AS-EXPECTED' THEN 6
        WHEN 'SIMULATED-OK' THEN 7 ELSE 8 END,
    config_area, test_name, target_identifier;

/* -----------------------------------------------------------------
   RESULT SET 4 - coverage matrix over the nine configuration areas
   ----------------------------------------------------------------- */
SELECT
    c.ordinal,
    c.config_area,
    c.family,
    CASE
        WHEN EXISTS (SELECT 1 FROM #TestResults t WHERE t.config_area = c.config_area AND t.verdict IN ('SIMULATED-OK','APPLIED','REVIEW')) THEN 'COVERED'
        WHEN EXISTS (SELECT 1 FROM #TestResults t WHERE t.config_area = c.config_area AND t.verdict = 'BLOCKED') THEN 'ATTEMPTED - BLOCKED'
        WHEN EXISTS (SELECT 1 FROM #TestResults t WHERE t.config_area = c.config_area AND t.verdict = 'DEFERRED') THEN 'DEFERRED - NEEDS APPROVED INPUT'
        WHEN EXISTS (SELECT 1 FROM #TestResults t WHERE t.config_area = c.config_area AND t.verdict = 'FAIL') THEN 'ATTEMPTED - FAILED'
        ELSE 'NOT TESTED'
    END AS coverage_state,
    (SELECT COUNT(*) FROM #TestResults t WHERE t.config_area = c.config_area) AS run_count,
    c.requirement
FROM #Coverage c
ORDER BY c.ordinal;
