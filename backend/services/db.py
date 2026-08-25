"""Postgres 访问层（EU 合规改造新增，2026-08）。

用于审计日志（hash-chain）、LLM 调用留痕、血缘事件、DSAR 请求、留存策略。
采用 psycopg3 同步 API + psycopg-pool，与现有 qdrant/embedding 的阻塞 IO 模式一致：
async 调用方通过 asyncio.to_thread 包装调用本模块函数。

数据库不可用时：init_db() 记 critical 日志但不阻断应用启动（本地开发只有 qdrant），
此时所有 audit/llm_trace 写入会抛 DBUnavailableError，由调用方按策略处理
（审计写入失败 = 管理变更端点 500，可责性优先）。
"""
import os
import logging
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool
from dotenv import load_dotenv

from models.schemas import DEFAULT_RETENTION_DAYS

logger = logging.getLogger(__name__)

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://joytag:joytag@localhost:5432/joytag",
)

_pool: ConnectionPool | None = None
_db_available = False


class DBUnavailableError(RuntimeError):
    """Postgres 不可用（未初始化或连接失败）。"""


def is_db_available() -> bool:
    return _db_available


def get_pool() -> ConnectionPool:
    """惰性创建连接池（open=False 惰性连接，避免启动时阻塞）。"""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=4,
            open=False,
            kwargs={"connect_timeout": 3},
        )
    return _pool


def _check_available() -> None:
    if not _db_available:
        raise DBUnavailableError("Postgres 不可用（启动时初始化失败）")


def execute(sql: str, params: tuple | None = None) -> None:
    _check_available()
    with get_pool().connection() as conn:
        conn.execute(sql, params)


def execute_many(sql: str, params_seq) -> None:
    """在一个连接中批量执行写操作，供采集元数据批量 upsert 使用。"""
    _check_available()
    with get_pool().connection() as conn:
        with conn.transaction():
            # psycopg3 exposes executemany() on Cursor, not Connection.
            with conn.cursor() as cursor:
                cursor.executemany(sql, params_seq)


def fetchone(sql: str, params: tuple | None = None) -> tuple | None:
    _check_available()
    with get_pool().connection() as conn:
        return conn.execute(sql, params).fetchone()


def fetchall(sql: str, params: tuple | None = None) -> list[tuple]:
    _check_available()
    with get_pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


def ensure_schema() -> None:
    """幂等建表 + 种子数据。在单个事务内执行。"""
    global _db_available
    pool = get_pool()
    try:
        pool.open()
    except psycopg.Error as e:
        logger.critical(f"[db] 连接失败: {e}")
        return

    # 默认留存天数来自 models.schemas.DEFAULT_RETENTION_DAYS（唯一权威源，披露正文同源）
    ddl = f"""
    CREATE TABLE IF NOT EXISTS audit_chain_head (
        id        SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        head_hash TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id               BIGSERIAL PRIMARY KEY,
        ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
        actor_sub        TEXT NOT NULL,
        actor_username   TEXT NOT NULL,
        actor_roles      TEXT[] NOT NULL DEFAULT '{{}}',
        action           TEXT NOT NULL,
        resource_type    TEXT NOT NULL,
        resource_id      TEXT NOT NULL,
        resource_snapshot JSONB,
        detail           JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        request_id       TEXT NOT NULL DEFAULT '',
        ip               TEXT NOT NULL DEFAULT '',
        prev_hash        TEXT NOT NULL,
        hash             TEXT NOT NULL,
        expires_at       TIMESTAMPTZ NOT NULL DEFAULT now() + interval '{DEFAULT_RETENTION_DAYS["audit"]} days'
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts       ON audit_log (ts DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_actor    ON audit_log (actor_sub, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_log (resource_type, resource_id);
    CREATE INDEX IF NOT EXISTS idx_audit_expires  ON audit_log (expires_at);

    CREATE TABLE IF NOT EXISTS llm_trace (
        id          BIGSERIAL PRIMARY KEY,
        ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
        call_type   TEXT NOT NULL,
        provider    TEXT NOT NULL,
        model       TEXT NOT NULL,
        request_id  TEXT NOT NULL DEFAULT '',
        prompt_hash TEXT NOT NULL,
        prompt_pii  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        word_sha256 TEXT,
        response    TEXT,
        result      JSONB,
        latency_ms  INT,
        retry_count INT NOT NULL DEFAULT 0,
        error       TEXT,
        expires_at  TIMESTAMPTZ NOT NULL DEFAULT now() + interval '{DEFAULT_RETENTION_DAYS["llm_trace"]} days'
    );
    CREATE INDEX IF NOT EXISTS idx_llm_trace_expires ON llm_trace (expires_at);
    CREATE INDEX IF NOT EXISTS idx_llm_trace_hash    ON llm_trace (prompt_hash);
    CREATE INDEX IF NOT EXISTS idx_llm_trace_word    ON llm_trace (word_sha256);

    CREATE TABLE IF NOT EXISTS lineage_event (
        id            BIGSERIAL PRIMARY KEY,
        ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
        run_id        TEXT NOT NULL,
        job_name      TEXT NOT NULL,
        job_namespace TEXT NOT NULL DEFAULT 'joytag',
        event_type    TEXT NOT NULL,
        facets        JSONB NOT NULL,
        expires_at    TIMESTAMPTZ NOT NULL DEFAULT now() + interval '{DEFAULT_RETENTION_DAYS["lineage"]} days'
    );
    CREATE INDEX IF NOT EXISTS idx_lineage_run     ON lineage_event (run_id, ts);
    CREATE INDEX IF NOT EXISTS idx_lineage_expires ON lineage_event (expires_at);

    CREATE TABLE IF NOT EXISTS dsar_request (
        id            BIGSERIAL PRIMARY KEY,
        ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
        ticket_id     UUID NOT NULL UNIQUE,
        request_type  TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'received',
        contact       TEXT NOT NULL,
        subject_note  TEXT NOT NULL DEFAULT '',
        findings      JSONB,
        erasure_proof JSONB,
        completed_at  TIMESTAMPTZ,
        created_by    TEXT NOT NULL DEFAULT 'public'
    );
    CREATE INDEX IF NOT EXISTS idx_dsar_status ON dsar_request (status, ts DESC);

    CREATE TABLE IF NOT EXISTS retention_policy (
        key        TEXT PRIMARY KEY,
        days       INT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS collector_source_snapshot (
        source           TEXT NOT NULL,
        country          TEXT NOT NULL DEFAULT '',
        seed_hash        TEXT NOT NULL,
        response         JSONB NOT NULL DEFAULT '[]'::jsonb,
        response_hash    TEXT NOT NULL DEFAULT '',
        last_fetched_at  TIMESTAMPTZ,
        last_changed_at  TIMESTAMPTZ,
        next_fetch_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        unchanged_streak INT NOT NULL DEFAULT 0,
        error_streak     INT NOT NULL DEFAULT 0,
        etag             TEXT NOT NULL DEFAULT '',
        last_modified    TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (source, country, seed_hash)
    );
    CREATE INDEX IF NOT EXISTS idx_collector_snapshot_due
        ON collector_source_snapshot (source, country, next_fetch_at);

    CREATE TABLE IF NOT EXISTS collector_candidate_observation (
        pipeline              TEXT NOT NULL,
        country               TEXT NOT NULL DEFAULT '',
        normalized_word       TEXT NOT NULL,
        display_word          TEXT NOT NULL,
        source_set             JSONB NOT NULL DEFAULT '[]'::jsonb,
        category              TEXT,
        first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        seen_count            INT NOT NULL DEFAULT 1,
        last_processed_at     TIMESTAMPTZ,
        next_eligible_at      TIMESTAMPTZ,
        decision_status       TEXT,
        source_heat_score     DOUBLE PRECISION NOT NULL DEFAULT 0,
        last_collection_run_id TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (pipeline, country, normalized_word)
    );
    CREATE INDEX IF NOT EXISTS idx_collector_observation_due
        ON collector_candidate_observation (pipeline, country, next_eligible_at);
    CREATE INDEX IF NOT EXISTS idx_collector_observation_seen
        ON collector_candidate_observation (pipeline, country, last_seen_at DESC);

    CREATE TABLE IF NOT EXISTS collector_seed_frontier (
        pipeline          TEXT NOT NULL DEFAULT 'cn',
        normalized_seed   TEXT NOT NULL,
        seed_word         TEXT NOT NULL,
        seed_kind         TEXT NOT NULL,
        category          TEXT,
        parent_seed       TEXT,
        seed_depth        INT NOT NULL DEFAULT 0,
        source_heat_score DOUBLE PRECISION NOT NULL DEFAULT 0,
        first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_queried_at   TIMESTAMPTZ,
        next_query_at     TIMESTAMPTZ,
        query_count       INT NOT NULL DEFAULT 0,
        active            BOOLEAN NOT NULL DEFAULT true,
        PRIMARY KEY (pipeline, normalized_seed)
    );
    CREATE INDEX IF NOT EXISTS idx_collector_seed_due
        ON collector_seed_frontier (pipeline, active, next_query_at, source_heat_score DESC);
    """

    seed_policy = f"""
    INSERT INTO retention_policy (key, days) VALUES
        ('llm_trace', {DEFAULT_RETENTION_DAYS["llm_trace"]}),
        ('lineage', {DEFAULT_RETENTION_DAYS["lineage"]}),
        ('audit', {DEFAULT_RETENTION_DAYS["audit"]})
    ON CONFLICT (key) DO NOTHING;
    """

    try:
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute(ddl)
                conn.execute(seed_policy)
                # 审计链头种子（初始随机值，Python 侧生成避免依赖 pgcrypto）
                import hashlib
                import secrets
                initial_head = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                conn.execute(
                    "INSERT INTO audit_chain_head (id, head_hash) "
                    "SELECT 1, %s WHERE NOT EXISTS (SELECT 1 FROM audit_chain_head)",
                    (initial_head,),
                )
        _db_available = True
        logger.info("[db] schema ready")
    except psycopg.Error as e:
        logger.critical(f"[db] schema 初始化失败: {e}")


def init_db() -> None:
    """应用启动时调用：建表 + 种子。失败不阻断启动（见模块 docstring）。"""
    ensure_schema()
