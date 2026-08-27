-- ============================================================================
-- project-llm-wiki : 02_project_schema_template.sql
--
-- Reference copy of the DDL that PROJECTS.CREATE_PROJECT() applies
-- automatically. You normally never run this by hand — it exists so the
-- per-project table shape is reviewable/diffable outside the stored proc,
-- and as a fallback if you need to create a project's schema manually.
--
-- Replace {{DATA_SCHEMA}} (e.g. DATA_OCMS_MINUTES) before running.
-- ============================================================================

USE ROLE ADVANCEDANALYTICS;
USE WAREHOUSE MTMWH02;
USE DATABASE MEDSOCMS;

CREATE SCHEMA IF NOT EXISTS MEDSOCMS.{{DATA_SCHEMA}};
USE SCHEMA MEDSOCMS.{{DATA_SCHEMA}};

CREATE STAGE IF NOT EXISTS DOCS_STAGE
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

CREATE TABLE IF NOT EXISTS RAW_DOCUMENTS (
    DOC_ID              INT IDENTITY PRIMARY KEY,
    FILE_NAME             VARCHAR(500) NOT NULL,
    STAGE_PATH             VARCHAR(1000) NOT NULL,
    SOURCE_TYPE             VARCHAR(20) NOT NULL,      -- 'UPLOAD' | 'SHAREPOINT'
    SHAREPOINT_ITEM_ID       VARCHAR(200),               -- dedup key for SharePoint-sourced docs
    DOCUMENT_DATE               DATE,
    RAW_TEXT                     VARCHAR(16777216),
    SOURCE_HASH                   VARCHAR(64),
    SOURCE_URL                     VARCHAR(2000),           -- SharePoint webUrl, NULL for uploads
    PARSED_AT                      TIMESTAMP_NTZ,
    CREATED_AT                      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE TABLE IF NOT EXISTS DOCUMENT_INDEX (
    NODE_ID          INT IDENTITY PRIMARY KEY,
    DOC_ID            INT NOT NULL REFERENCES RAW_DOCUMENTS(DOC_ID),
    PARENT_NODE_ID     INT,
    NODE_LEVEL           VARCHAR(20) NOT NULL,          -- 'document' | 'section' | 'subsection'
    NODE_TITLE             VARCHAR(500),
    NODE_SUMMARY             VARCHAR(4000),
    NODE_TEXT_REF              VARCHAR(50),
    CREATED_AT                  TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Notes on SEGMENTATION_PROFILE (set per-project on PROJECTS row):
--   'GENERIC'  — document -> section tree, no assumptions about content type.
--                Used by python/ingestion/index_builder.py::PROMPTS['GENERIC'].
--   Add new profiles by adding a key to PROMPTS in index_builder.py and
--   setting PROJECTS.SEGMENTATION_PROFILE to match — no schema change needed.
