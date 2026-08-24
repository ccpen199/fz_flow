-- Project database dependencies for standard dictionary resolution.
-- This file creates views only. It does not create business tables,
-- add business columns, or import business data.
--
-- Prerequisites in the target database:
--   1. dict_brand with Type, Code, Name, NameEn, Alias columns.
--   2. dict_info with Id, ParentId, DictType, DictValue, DictName columns.
--   3. clothing_info.BrandCode contains the brand code used by dict_brand.Code.
--   4. clothing_fiber_info.Code contains the fiber code used by dict_info.DictValue.
--
-- The backend also creates these views during startup. Run this file when
-- preparing a customer database before starting the backend, or use it as a
-- documented manual migration.

USE `dataservice_test_local`;

-- Optional preflight checks. These return rows only when the prerequisite
-- object exists.
SELECT
  TABLE_NAME,
  TABLE_TYPE
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN (
    'dict_brand',
    'dict_info',
    'clothing_info',
    'clothing_fiber_info'
  )
ORDER BY TABLE_NAME;

-- Standard brand view used for brand recognition and exact filtering.
-- Type = 0 means brand records; other dict_brand types are excluded.
CREATE OR REPLACE VIEW `dict_brand_info` AS
SELECT
  `Id`,
  `Type`,
  `Code`,
  `Name`,
  `NameEn`,
  `Alias`,
  `Location`,
  `Source`,
  `SourceName`,
  `SourceUrl`
FROM `dict_brand`
WHERE `Type` = 0;

-- Standard fiber view. The parent ID identifies the fiber dictionary subtree.
-- Keep this value aligned with DictionaryViewBootstrap in the backend.
CREATE OR REPLACE VIEW `dict_fiber_info` AS
SELECT
  `Id`,
  `ParentId`,
  `DictType`,
  `DictValue` AS `Code`,
  `DictName` AS `Name`
FROM `dict_info`
WHERE `ParentId` = '46641D1E-D348-4503-8C60-1664213D4D19';

-- Verification queries.
SELECT
  'dict_brand_info' AS view_name,
  COUNT(*) AS row_count
FROM `dict_brand_info`
UNION ALL
SELECT
  'dict_fiber_info' AS view_name,
  COUNT(*) AS row_count
FROM `dict_fiber_info`;

-- Example relationship checks:
-- SELECT ci.BrandCode, db.Name
-- FROM clothing_info ci
-- LEFT JOIN dict_brand_info db ON db.Code = ci.BrandCode
-- WHERE ci.BrandCode IS NOT NULL
-- LIMIT 20;
--
-- SELECT cfi.Code, dfi.Name
-- FROM clothing_fiber_info cfi
-- LEFT JOIN dict_fiber_info dfi ON dfi.Code = cfi.Code
-- LIMIT 20;
