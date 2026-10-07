-- 0005_embedding_model: every memory node vector names the embedding model that made it, so
-- vectors from different models are never compared (Cohere on Bedrock and OpenAI both give
-- 1024 dimensions, but their spaces differ). Search filters on the current model's id.
-- Source: ARCHITECTURE.md, "Schema DDL" (memory_nodes) and "Hybrid memory search".
--
-- Expand only: a new nullable column and a check. No code writes node vectors yet, so no
-- vector loses anything; a vector without a model could never be compared safely, so any
-- such row has its vector cleared and is embedded again when node embedding arrives.

set local role app_owner;

-- `provider:model@dimensions`, as CoreSettings.embedding_model_id gives it, for example
-- `openai:text-embedding-3-large@1024` or `bedrock:cohere.embed-v4:0@1024`.
alter table app.memory_nodes add column embedding_model text;

-- The update must see every user's rows: lift forced row-level security for this
-- transaction only, as 0003 does.
alter table app.memory_nodes no force row level security;
update app.memory_nodes set embedding = null where embedding is not null;
alter table app.memory_nodes force row level security;

alter table app.memory_nodes
  add constraint memory_nodes_embedding_model_check
    check ((embedding is null) = (embedding_model is null));
