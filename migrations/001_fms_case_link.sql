-- ============================================================================
-- Idempotent SQL Migration for FMS <-> Cases System Two-Way Integration
-- Run against BOTH:
--   1. OperationsDB (Production)
--   2. Test_OperationsDB (Testing)
-- ============================================================================

-- 1. Create FMS_Case_Link relationship table
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'FMS_Case_Link')
BEGIN
    CREATE TABLE FMS_Case_Link (
        LinkID INT IDENTITY(1,1) PRIMARY KEY,
        CaseID VARCHAR(50) NOT NULL,
        FMSAppID VARCHAR(50) NOT NULL,
        FMSTaskID VARCHAR(100) NOT NULL,
        FMSStageNo VARCHAR(50) NOT NULL DEFAULT '',
        FMSStageName NVARCHAR(255) NULL,
        FMSFormName NVARCHAR(255) NULL,
        FMSFormURL NVARCHAR(1000) NULL,
        DatabaseTarget VARCHAR(50) NOT NULL DEFAULT 'OperationsDB',
        LinkedBy VARCHAR(100) NULL,
        LinkedAt DATETIME2 NOT NULL DEFAULT GETDATE(),
        CONSTRAINT UQ_FMS_Case_Link UNIQUE (CaseID, FMSAppID, FMSTaskID, FMSStageNo, DatabaseTarget)
    );

    CREATE INDEX IX_FMS_Case_Link_Lookup 
        ON FMS_Case_Link (FMSAppID, FMSTaskID, FMSStageNo, DatabaseTarget);

    CREATE INDEX IX_FMS_Case_Link_CaseID 
        ON FMS_Case_Link (CaseID);

    PRINT 'Table FMS_Case_Link created successfully.';
END
ELSE
BEGIN
    PRINT 'Table FMS_Case_Link already exists.';
END
GO

-- 2. Ensure CaseSourceURL column exists on Case_Master
IF EXISTS (SELECT * FROM sys.tables WHERE name = 'Case_Master')
BEGIN
    IF NOT EXISTS (
        SELECT * FROM sys.columns 
        WHERE object_id = OBJECT_ID('Case_Master') AND name = 'CaseSourceURL'
    )
    BEGIN
        ALTER TABLE Case_Master ADD CaseSourceURL NVARCHAR(1000) NULL;
        PRINT 'Column CaseSourceURL added to Case_Master.';
    END
    ELSE
    BEGIN
        PRINT 'Column CaseSourceURL already exists on Case_Master.';
    END
END
GO

-- ============================================================================
-- Read-Only Verification Queries:
-- ============================================================================
-- Run these queries after migration to verify schema integrity:
--
-- 1. Verify FMS_Case_Link structure:
-- SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
-- FROM INFORMATION_SCHEMA.COLUMNS
-- WHERE TABLE_NAME = 'FMS_Case_Link';
--
-- 2. Verify CaseSourceURL on Case_Master:
-- SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
-- FROM INFORMATION_SCHEMA.COLUMNS
-- WHERE TABLE_NAME = 'Case_Master' AND COLUMN_NAME = 'CaseSourceURL';
--
-- 3. Check existing links (read-only):
-- SELECT TOP 10 * FROM FMS_Case_Link ORDER BY LinkID DESC;
-- ============================================================================

