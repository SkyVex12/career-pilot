-- 0004_document_ingesting: a document between `POST /documents/{id}/ingest` and the end of
-- its `memory.extract` job is `ingesting`, so the desktop app can show it as processing.
-- Source: ARCHITECTURE.md, "Schema DDL" (documents) and "REST API" (build step 4b).
--
-- Expand only: a wider check. Code from step 4a never writes the new value.

set local role app_owner;

alter table app.documents drop constraint documents_status_check;
alter table app.documents
  add constraint documents_status_check
    check (status in ('uploaded','ingesting','ingested','failed'));
