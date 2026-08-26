-- ============================================================
--  TaskMonitor Schema (PostgreSQL 16+)
--  混合设计模式：关系核心列 + JSONB 灵活列
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ------------------------------------------------------------
-- tasks：任务主表
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    task_id             VARCHAR(64)     PRIMARY KEY,
    description         TEXT            NOT NULL,
    overall_status      VARCHAR(32)     NOT NULL DEFAULT 'in_progress',
    current_subtask     VARCHAR(64),
    priority            INTEGER         DEFAULT 0,
    task_type           VARCHAR(64),
    created_at          DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
    updated_at          DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
    metadata            JSONB           NOT NULL DEFAULT '{}'::JSONB,
    global_parameters   JSONB           NOT NULL DEFAULT '{}'::JSONB,
    anomaly_state       JSONB           NOT NULL DEFAULT '{}'::JSONB,
    latest_anomaly_context JSONB        DEFAULT '{}'::JSONB,
    latest_anomaly_advice JSONB         DEFAULT '{}'::JSONB,
    notifications       JSONB           NOT NULL DEFAULT '[]'::JSONB,
    pending_intervention JSONB          DEFAULT '{}'::JSONB
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(overall_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_type          ON tasks(task_type);
CREATE INDEX IF NOT EXISTS idx_tasks_current_sub   ON tasks(current_subtask);
CREATE INDEX IF NOT EXISTS idx_tasks_metadata_gin  ON tasks USING GIN (metadata jsonb_path_ops);


-- ------------------------------------------------------------
-- subtasks：子任务表（关系列+JSONB）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subtasks (
    id                  BIGSERIAL       PRIMARY KEY,
    task_id             VARCHAR(64)     NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    subtask_id          VARCHAR(64)     NOT NULL,
    name                VARCHAR(255),
    status              VARCHAR(32)     NOT NULL DEFAULT 'pending',
    retry_count         INTEGER         NOT NULL DEFAULT 0,
    criteria_ref        VARCHAR(128),
    evidence_summary    TEXT,
    latest_state        JSONB           NOT NULL DEFAULT '{}'::JSONB,
    completion_criteria JSONB           NOT NULL DEFAULT '{}'::JSONB,
    user_overrides      JSONB           NOT NULL DEFAULT '{}'::JSONB,
    parameters          JSONB           NOT NULL DEFAULT '{}'::JSONB,
    anomalies_config    JSONB           NOT NULL DEFAULT '{}'::JSONB,
    created_at          DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
    updated_at          DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
    UNIQUE (task_id, subtask_id)
);

CREATE INDEX IF NOT EXISTS idx_subtasks_task_status ON subtasks(task_id, status);
CREATE INDEX IF NOT EXISTS idx_subtasks_criteria    ON subtasks(criteria_ref);
CREATE INDEX IF NOT EXISTS idx_subtasks_latest_gin  ON subtasks USING GIN (latest_state jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_subtasks_overrides   ON subtasks USING GIN (user_overrides);

-- 高频查询路径：距离误差、标志位等数值表达式索引
CREATE INDEX IF NOT EXISTS idx_subtasks_dist_err ON subtasks (((latest_state->>'distance_error_m')::numeric));
CREATE INDEX IF NOT EXISTS idx_subtasks_slot_err ON subtasks (((latest_state->>'slot_pose_delta_m')::numeric));


-- ------------------------------------------------------------
-- audit_logs：审计日志（所有 write/control 动作 100% 留痕）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id                  BIGSERIAL       PRIMARY KEY,
    task_id             VARCHAR(64),
    subtask_id          VARCHAR(64),
    user_id             VARCHAR(128)    DEFAULT 'anonymous',
    action              VARCHAR(64)     NOT NULL,
    action_category     VARCHAR(32)     NOT NULL,   -- query / control / write
    severity            VARCHAR(16)     NOT NULL DEFAULT 'info',   -- info/warn/critical
    input_message       TEXT,
    action_payload      JSONB,
    before_state        JSONB,
    after_state         JSONB,
    decision_path       VARCHAR(255),               -- "rule|slm|llm_gateway"
    llm_confidence      REAL,
    result_ok           BOOLEAN,
    error_message       TEXT,
    created_at          DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())
);

CREATE INDEX IF NOT EXISTS idx_audit_task_time   ON audit_logs(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_cat  ON audit_logs(action_category, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_severity    ON audit_logs(severity, created_at DESC);


-- ------------------------------------------------------------
-- cache_metadata：缓存元数据（命中率统计，用于网关观察）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cache_meta (
    cache_layer         VARCHAR(16) PRIMARY KEY,    -- exact / semantic / prefix
    hits                BIGINT NOT NULL DEFAULT 0,
    misses              BIGINT NOT NULL DEFAULT 0,
    updated_at          DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())
);

INSERT INTO cache_meta(cache_layer, hits, misses) VALUES
    ('exact', 0, 0),
    ('semantic', 0, 0)
ON CONFLICT (cache_layer) DO NOTHING;
