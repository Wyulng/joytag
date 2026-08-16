#!/bin/bash
# Postgres 首次启动初始化（docker-entrypoint-initdb.d）：
# 建 keycloak 库 + 应用角色，密码来自容器环境变量（不落 git）。
# 注意：密码中的单引号会被转义，避免 SQL 注入/语法错误。
set -e

JOYTAG_PW_ESC="${JOYTAG_DB_PASSWORD//\'/\'\'}"
KEYCLOAK_PW_ESC="${KEYCLOAK_DB_PASSWORD//\'/\'\'}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE joytag LOGIN PASSWORD '${JOYTAG_PW_ESC}';
    CREATE ROLE keycloak LOGIN PASSWORD '${KEYCLOAK_PW_ESC}';
    CREATE DATABASE keycloak;
    GRANT ALL PRIVILEGES ON DATABASE joytag TO joytag;
    GRANT ALL PRIVILEGES ON DATABASE keycloak TO keycloak;
EOSQL

# PG15+：public schema 默认权限收紧，需显式授予（joytag 库 = 审计/trace/lineage/DSAR 表）
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname joytag <<-EOSQL
    GRANT ALL ON SCHEMA public TO joytag;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO joytag;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO joytag;
EOSQL

# keycloak 库同样处理（Keycloak 以 keycloak 角色建表）
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname keycloak <<-EOSQL
    GRANT ALL ON SCHEMA public TO keycloak;
EOSQL
