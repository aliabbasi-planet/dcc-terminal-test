/*
====================================================================
DCC ENABLEMENT CONFIGURATION - CONSOLIDATED UAT SIMULATION TEST SUITE
====================================================================
Run as one complete script in a single SSMS query window and session.
Do not run individual sections separately. All procedure calls use @is_simulation = 1.
The script returns three consolidated result sets at the end. The procedure
may also return its own simulation and rollback-preview grids during execution:
  1. Check summary
  2. Selected terminal identifiers
  3. Test results, including rollback status
====================================================================
*/

USE [3CDB];
GO

DROP TABLE IF EXISTS #CheckSummary;
DROP TABLE IF EXISTS #TerminalSelection;
DROP TABLE IF EXISTS #TestResults;
GO

SET NOCOUNT ON;
SET XACT_ABORT OFF;

DECLARE @ProcName nvarchar(300) = N'[cccai].[spApplyDCCEnablementConfiguration]';
DECLARE @ExpectedServer sysname = N'LU3C01DVSQL01';
DECLARE @ExpectedDatabase sysname = N'3CDB';

CREATE TABLE #CheckSummary
(
    check_name nvarchar(200) NOT NULL,
    detail nvarchar(1000) NOT NULL,
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
    target_type nvarchar(50) NOT NULL,
    target_identifier nvarchar(100) NOT NULL,
    expected_result nvarchar(600) NOT NULL,
    execution_detail nvarchar(1000) NOT NULL,
    status varchar(10) NOT NULL,
    change_persisted bit NULL,
    outer_transaction_status nvarchar(200) NOT NULL,
    rollback_preview_status nvarchar(200) NOT NULL,
    executed_at datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME()
);

INSERT INTO #CheckSummary (check_name, detail, status)
VALUES
    (N'Connected server', @@SERVERNAME, CASE WHEN @@SERVERNAME = @ExpectedServer OR @@SERVERNAME LIKE @ExpectedServer + N'%' THEN 'PASS' ELSE 'FAIL' END),
    (N'Selected database', DB_NAME(), CASE WHEN DB_NAME() = @ExpectedDatabase THEN 'PASS' ELSE 'FAIL' END),
    (N'Login', CONVERT(nvarchar(200), ORIGINAL_LOGIN()), 'INFO'),
    (N'Simulation safeguard', N'All procedure calls are hard-coded with @is_simulation = 1.', 'PASS'),
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
    (N'Firmware procedure',
        CASE WHEN OBJECT_ID(N'[cccintegrang].[spManageEMVTerminalFirmwarePackage]', N'P') IS NULL
            THEN N'NOT FOUND (firmware is out of scope)' ELSE N'Found' END,
        CASE WHEN OBJECT_ID(N'[cccintegrang].[spManageEMVTerminalFirmwarePackage]', N'P') IS NULL THEN 'WARN' ELSE 'PASS' END);

INSERT INTO #CheckSummary (check_name, detail, status)
SELECT N'Fixture instance I000000001',
       CASE WHEN EXISTS (SELECT 1 FROM [cccintegrang].[instance] WHERE instance_identifier = N'I000000001') THEN N'Found' ELSE N'NOT FOUND' END,
       CASE WHEN EXISTS (SELECT 1 FROM [cccintegrang].[instance] WHERE instance_identifier = N'I000000001') THEN 'PASS' ELSE 'FAIL' END
UNION ALL
SELECT N'Fixture location 0000000',
       CASE WHEN EXISTS (SELECT 1 FROM [ccc].[location] WHERE location_no = N'0000000') THEN N'Found' ELSE N'NOT FOUND' END,
       CASE WHEN EXISTS (SELECT 1 FROM [ccc].[location] WHERE location_no = N'0000000') THEN 'PASS' ELSE 'FAIL' END;

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

INSERT INTO #CheckSummary (check_name, detail, status)
SELECT
    N'Bit 2 representative coverage',
    CONCAT(
        N'Already Standard=', SUM(CASE WHEN selection_status = 'SELECTED - ALREADY STANDARD' THEN 1 ELSE 0 END),
        N'; Different version=', SUM(CASE WHEN selection_status = 'SELECTED - DIFFERENT VERSION' THEN 1 ELSE 0 END)
    ),
    CASE WHEN SUM(CASE WHEN selection_status = 'SELECTED - ALREADY STANDARD' THEN 1 ELSE 0 END) >= 1
           AND SUM(CASE WHEN selection_status = 'SELECTED - DIFFERENT VERSION' THEN 1 ELSE 0 END) >= 1
         THEN 'PASS' ELSE 'WARN' END
FROM #TerminalSelection;

DECLARE @DependenciesOk bit = CASE WHEN EXISTS
(
    SELECT 1 FROM #CheckSummary
    WHERE status = 'FAIL'
      AND check_name IN
      (
          N'Connected server', N'Selected database', N'Procedure exists', N'EXECUTE on main procedure',
          N'[db].[fnDisplayTrace] exists', N'EXECUTE on [db].[fnDisplayTrace]',
          N'Fixture instance I000000001', N'Fixture location 0000000'
      )
) THEN 0 ELSE 1 END;

IF @DependenciesOk = 0
BEGIN
    INSERT INTO #TestResults
    (test_name, target_type, target_identifier, expected_result, execution_detail, status, change_persisted, outer_transaction_status, rollback_preview_status)
    VALUES
    (N'Test suite execution', N'Suite', N'All tests', N'Required checks pass',
        N'Not executed because one or more required checks failed.', 'BLOCKED', NULL, N'NOT STARTED', N'NOT APPLICABLE');
END
ELSE
BEGIN
    DECLARE @InstanceJson nvarchar(max) = N'[{"instance_identifier":"I000000001"}]';
    DECLARE @Bit8Flags TABLE (id int IDENTITY(1,1), flag_name nvarchar(200) NOT NULL);
    INSERT INTO @Bit8Flags (flag_name)
    VALUES (N'dccEnable'), (N'dccEnableAuth'), (N'dccEnableCompletion'), (N'dccEnableNfc'), (N'dccEnableNfcSingleTap');

    DECLARE @FlagId int = 1;
    DECLARE @FlagMax int = (SELECT MAX(id) FROM @Bit8Flags);
    DECLARE @FlagName nvarchar(200);
    DECLARE @OuterRollbackExecuted bit;

    WHILE @FlagId <= @FlagMax
    BEGIN
        SELECT @FlagName = flag_name FROM @Bit8Flags WHERE id = @FlagId;
        SET @OuterRollbackExecuted = 0;
        BEGIN TRY
            BEGIN TRANSACTION;
            EXEC [cccai].[spApplyDCCEnablementConfiguration]
                @display_config = 8, @instance_json = @InstanceJson,
                @Extra_Config_Name = @FlagName, @Config_value = 1, @is_simulation = 1;
            IF XACT_STATE() <> 0
            BEGIN
                ROLLBACK TRANSACTION;
                SET @OuterRollbackExecuted = 1;
            END;
            INSERT INTO #TestResults
            (test_name, target_type, target_identifier, expected_result, execution_detail, status, change_persisted, outer_transaction_status, rollback_preview_status)
            VALUES
            (N'Bit 8 - ' + @FlagName, N'Instance', N'I000000001', N'Simulation considers the valid instance and proposes setting the exact flag to true.',
             N'No SQL exception was raised. Review the procedure trace, proposed XML, and persistent-state evidence.', 'REVIEW', NULL,
             CASE WHEN @OuterRollbackExecuted = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY TEST HARNESS' ELSE N'NO ACTIVE TRANSACTION REMAINED AFTER PROCEDURE' END,
             N'MANUAL REVIEW REQUIRED - inspect procedure backup/rollback result grid');
        END TRY
        BEGIN CATCH
            IF XACT_STATE() <> 0
            BEGIN
                ROLLBACK TRANSACTION;
                SET @OuterRollbackExecuted = 1;
            END;
            INSERT INTO #TestResults
            (test_name, target_type, target_identifier, expected_result, execution_detail, status, change_persisted, outer_transaction_status, rollback_preview_status)
            VALUES
            (N'Bit 8 - ' + @FlagName, N'Instance', N'I000000001', N'Simulation considers the valid instance and proposes setting the exact flag to true.',
             LEFT(CONCAT(N'Error ', ERROR_NUMBER(), N': ', ERROR_MESSAGE()), 1000), 'FAIL', NULL,
             CASE WHEN @OuterRollbackExecuted = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY TEST HARNESS AFTER SQL EXCEPTION' ELSE N'NO ACTIVE TRANSACTION REMAINED AFTER PROCEDURE' END,
             N'NOT AVAILABLE - SQL EXCEPTION');
        END CATCH;
        SET @FlagId += 1;
    END;

    DECLARE @TerminalIdentifier nvarchar(50);
    DECLARE TerminalCursor CURSOR LOCAL FAST_FORWARD FOR
        SELECT terminal_identifier FROM #TerminalSelection;
    OPEN TerminalCursor;
    FETCH NEXT FROM TerminalCursor INTO @TerminalIdentifier;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @OuterRollbackExecuted = 0;
        DECLARE @TerminalJson nvarchar(max) = N'[{"terminal_identifier":"' + STRING_ESCAPE(@TerminalIdentifier, 'json') + N'"}]';
        BEGIN TRY
            BEGIN TRANSACTION;
            EXEC [cccai].[spApplyDCCEnablementConfiguration]
                @display_config = 2, @terminal_json = @TerminalJson,
                @ConfigDownloadVersionDesc = N'Standard', @is_simulation = 1;
            IF XACT_STATE() <> 0
            BEGIN
                ROLLBACK TRANSACTION;
                SET @OuterRollbackExecuted = 1;
            END;
            INSERT INTO #TestResults
            (test_name, target_type, target_identifier, expected_result, execution_detail, status, change_persisted, outer_transaction_status, rollback_preview_status)
            VALUES
            (N'Bit 2 - Config Download Version', N'Terminal', @TerminalIdentifier, N'Simulation considers the valid terminal and proposes Standard.',
             N'No SQL exception was raised. Review the procedure trace, proposed value, and persistent-state evidence.', 'REVIEW', NULL,
             CASE WHEN @OuterRollbackExecuted = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY TEST HARNESS' ELSE N'NO ACTIVE TRANSACTION REMAINED AFTER PROCEDURE' END,
             N'MANUAL REVIEW REQUIRED - inspect procedure backup/rollback result grid');
        END TRY
        BEGIN CATCH
            IF XACT_STATE() <> 0
            BEGIN
                ROLLBACK TRANSACTION;
                SET @OuterRollbackExecuted = 1;
            END;
            INSERT INTO #TestResults
            (test_name, target_type, target_identifier, expected_result, execution_detail, status, change_persisted, outer_transaction_status, rollback_preview_status)
            VALUES
            (N'Bit 2 - Config Download Version', N'Terminal', @TerminalIdentifier, N'Simulation considers the valid terminal and proposes Standard.',
             LEFT(CONCAT(N'Error ', ERROR_NUMBER(), N': ', ERROR_MESSAGE()), 1000), 'FAIL', NULL,
             CASE WHEN @OuterRollbackExecuted = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY TEST HARNESS AFTER SQL EXCEPTION' ELSE N'NO ACTIVE TRANSACTION REMAINED AFTER PROCEDURE' END,
             N'NOT AVAILABLE - SQL EXCEPTION');
        END CATCH;
        FETCH NEXT FROM TerminalCursor INTO @TerminalIdentifier;
    END;
    CLOSE TerminalCursor;
    DEALLOCATE TerminalCursor;

    DECLARE @LocationJson nvarchar(max) = N'[{"location_no":"0000000"}]';
    SET @OuterRollbackExecuted = 0;
    BEGIN TRY
        BEGIN TRANSACTION;
        EXEC [cccai].[spApplyDCCEnablementConfiguration]
            @display_config = 1, @location_json = @LocationJson,
            @extra_function_name = N'DCCXpressCO', @add = 1, @is_simulation = 1;
        IF XACT_STATE() <> 0
        BEGIN
            ROLLBACK TRANSACTION;
            SET @OuterRollbackExecuted = 1;
        END;
        INSERT INTO #TestResults
        (test_name, target_type, target_identifier, expected_result, execution_detail, status, change_persisted, outer_transaction_status, rollback_preview_status)
        VALUES
        (N'Bit 1 - DCC Xpress CO', N'Location', N'0000000', N'Simulation considers the valid location and proposes adding DCCXpressCO.',
         N'No SQL exception was raised. Review the procedure trace, proposed XML, and persistent-state evidence.', 'REVIEW', NULL,
         CASE WHEN @OuterRollbackExecuted = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY TEST HARNESS' ELSE N'NO ACTIVE TRANSACTION REMAINED AFTER PROCEDURE' END,
         N'MANUAL REVIEW REQUIRED - inspect procedure backup/rollback result grid');
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0
        BEGIN
            ROLLBACK TRANSACTION;
            SET @OuterRollbackExecuted = 1;
        END;
        INSERT INTO #TestResults
        (test_name, target_type, target_identifier, expected_result, execution_detail, status, change_persisted, outer_transaction_status, rollback_preview_status)
        VALUES
        (N'Bit 1 - DCC Xpress CO', N'Location', N'0000000', N'Simulation considers the valid location and proposes adding DCCXpressCO.',
         LEFT(CONCAT(N'Error ', ERROR_NUMBER(), N': ', ERROR_MESSAGE()), 1000), 'FAIL', NULL,
         CASE WHEN @OuterRollbackExecuted = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY TEST HARNESS AFTER SQL EXCEPTION' ELSE N'NO ACTIVE TRANSACTION REMAINED AFTER PROCEDURE' END,
         N'NOT AVAILABLE - SQL EXCEPTION');
    END CATCH;

    SET @OuterRollbackExecuted = 0;
    BEGIN TRY
        BEGIN TRANSACTION;
        EXEC [cccai].[spApplyDCCEnablementConfiguration]
            @display_config = 8, @instance_json = N'[{"instance_identifier":"I000012345"}]',
            @Extra_Config_Name = N'dccEnable', @Config_value = 1, @is_simulation = 1;
        IF XACT_STATE() <> 0
        BEGIN
            ROLLBACK TRANSACTION;
            SET @OuterRollbackExecuted = 1;
        END;
        INSERT INTO #TestResults
        (test_name, target_type, target_identifier, expected_result, execution_detail, status, change_persisted, outer_transaction_status, rollback_preview_status)
        VALUES
        (N'Negative - nonexistent instance', N'Instance', N'I000012345', N'Procedure reports or raises an error for a nonexistent target.',
         N'No SQL exception was raised. Confirm that the procedure trace contains the expected ERROR message.', 'REVIEW', NULL,
         CASE WHEN @OuterRollbackExecuted = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY TEST HARNESS' ELSE N'NO ACTIVE TRANSACTION REMAINED AFTER PROCEDURE' END,
         N'MANUAL REVIEW REQUIRED - inspect procedure error trace');
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0
        BEGIN
            ROLLBACK TRANSACTION;
            SET @OuterRollbackExecuted = 1;
        END;
        INSERT INTO #TestResults
        (test_name, target_type, target_identifier, expected_result, execution_detail, status, change_persisted, outer_transaction_status, rollback_preview_status)
        VALUES
        (N'Negative - nonexistent instance', N'Instance', N'I000012345', N'Procedure reports or raises an error for a nonexistent target.',
            LEFT(CONCAT(N'Expected rejection: Error ', ERROR_NUMBER(), N': ', ERROR_MESSAGE()), 1000), 'PASS', NULL,
            CASE WHEN @OuterRollbackExecuted = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY TEST HARNESS; REJECTION EXPECTED' ELSE N'NO ACTIVE TRANSACTION REMAINED AFTER PROCEDURE' END,
            N'NOT APPLICABLE - SQL EXCEPTION CONFIRMED REJECTION');
    END CATCH;

    SET @OuterRollbackExecuted = 0;
    BEGIN TRY
        BEGIN TRANSACTION;
        EXEC [cccai].[spApplyDCCEnablementConfiguration]
            @display_config = 2, @terminal_json = N'[{"terminal_identifier":"34567891"}]',
            @ConfigDownloadVersionDesc = N'Standard', @is_simulation = 1;
        IF XACT_STATE() <> 0
        BEGIN
            ROLLBACK TRANSACTION;
            SET @OuterRollbackExecuted = 1;
        END;
        INSERT INTO #TestResults
        (test_name, target_type, target_identifier, expected_result, execution_detail, status, change_persisted, outer_transaction_status, rollback_preview_status)
        VALUES
        (N'Negative - nonexistent terminal', N'Terminal', N'34567891', N'Procedure reports or raises an error for a nonexistent target.',
         N'No SQL exception was raised. Confirm that the procedure trace contains the expected ERROR message.', 'REVIEW', NULL,
         CASE WHEN @OuterRollbackExecuted = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY TEST HARNESS' ELSE N'NO ACTIVE TRANSACTION REMAINED AFTER PROCEDURE' END,
         N'MANUAL REVIEW REQUIRED - inspect procedure error trace');
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0
        BEGIN
            ROLLBACK TRANSACTION;
            SET @OuterRollbackExecuted = 1;
        END;
        INSERT INTO #TestResults
        (test_name, target_type, target_identifier, expected_result, execution_detail, status, change_persisted, outer_transaction_status, rollback_preview_status)
        VALUES
        (N'Negative - nonexistent terminal', N'Terminal', N'34567891', N'Procedure reports or raises an error for a nonexistent target.',
         LEFT(CONCAT(N'Expected rejection: Error ', ERROR_NUMBER(), N': ', ERROR_MESSAGE()), 1000), 'PASS', NULL,
         CASE WHEN @OuterRollbackExecuted = 1 THEN N'OUTER TRANSACTION ROLLED BACK BY TEST HARNESS; REJECTION EXPECTED' ELSE N'NO ACTIVE TRANSACTION REMAINED AFTER PROCEDURE' END,
         N'NOT APPLICABLE - SQL EXCEPTION CONFIRMED REJECTION');
    END CATCH;
END;

SELECT check_name, detail, status
FROM #CheckSummary
ORDER BY CASE status WHEN 'FAIL' THEN 1 WHEN 'WARN' THEN 2 WHEN 'PASS' THEN 3 ELSE 4 END, check_name;

SELECT terminal_identifier, configdownload_version, selection_status
FROM #TerminalSelection
ORDER BY terminal_identifier;

SELECT test_name, target_type, target_identifier, expected_result, execution_detail, status,
       change_persisted, outer_transaction_status, rollback_preview_status, executed_at
FROM #TestResults
ORDER BY CASE status WHEN 'FAIL' THEN 1 WHEN 'BLOCKED' THEN 2 WHEN 'REVIEW' THEN 3 WHEN 'PASS' THEN 4 ELSE 5 END,
         test_name,
         target_identifier;