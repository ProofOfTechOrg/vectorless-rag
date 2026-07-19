#!/bin/sh
set -eu
psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
  CREATE ROLE rag_owner LOGIN PASSWORD '${RAG_OWNER_PASSWORD}';
  CREATE ROLE rag_app LOGIN PASSWORD '${RAG_APP_PASSWORD}';
  CREATE ROLE rag_reader LOGIN PASSWORD '${RAG_READER_PASSWORD}';
  CREATE ROLE langfuse LOGIN PASSWORD '${LANGFUSE_DB_PASSWORD}';
  CREATE DATABASE rag OWNER rag_owner;
  CREATE DATABASE langfuse OWNER langfuse;
SQL
psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname rag <<-SQL
  GRANT CONNECT ON DATABASE rag TO rag_app, rag_reader;
  GRANT CREATE ON SCHEMA public TO rag_app;
SQL
