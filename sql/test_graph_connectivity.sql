-- ============================================================================
-- project-llm-wiki : test_graph_connectivity.sql
-- Unchanged from ocms-llm-wiki — this infra is tenant-level, not per-project.
-- Run once after setup to confirm the External Access Integration works.
-- ============================================================================

USE ROLE ADVANCEDANALYTICS;
USE WAREHOUSE MTMWH02;
USE DATABASE MEDSOCMS;

-- Expect these to already exist (created outside this repo, shared across
-- all projects since the Azure AD app registration is tenant-scoped):
--   SECRET             MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET
--   NETWORK RULE        MEDSOCMS.APP_CATALOG.GRAPH_API_NETWORK_RULE
--   EXTERNAL ACCESS      MEDSOCMS.APP_CATALOG.GRAPH_API_ACCESS_INTEGRATION

SHOW SECRETS LIKE 'GRAPH_API_SECRET' IN SCHEMA MEDSOCMS.APP_CATALOG;
SHOW NETWORK RULES LIKE 'GRAPH_API_NETWORK_RULE' IN SCHEMA MEDSOCMS.APP_CATALOG;
SHOW EXTERNAL ACCESS INTEGRATIONS LIKE 'GRAPH_API_ACCESS_INTEGRATION';

-- If any of the above return no rows, Graph API ingestion (SharePoint) will
-- fail for every project until an admin creates them once, tenant-wide:
--
-- CREATE SECRET IF NOT EXISTS MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET
--   TYPE = GENERIC_STRING
--   SECRET_STRING = '<client_secret>';
--
-- CREATE NETWORK RULE IF NOT EXISTS MEDSOCMS.APP_CATALOG.GRAPH_API_NETWORK_RULE
--   MODE = EGRESS
--   TYPE = HOST_PORT
--   VALUE_LIST = ('login.microsoftonline.com', 'graph.microsoft.com');
--
-- CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS GRAPH_API_ACCESS_INTEGRATION
--   ALLOWED_NETWORK_RULES = (MEDSOCMS.APP_CATALOG.GRAPH_API_NETWORK_RULE)
--   ALLOWED_AUTHENTICATION_SECRETS = (MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET)
--   ENABLED = TRUE;
