-- Схема прототипа (SQLite). Целевая схема PostgreSQL — итерация I4
-- (docs/03-architecture/data-model.md). Время: секунды UTC; деньги: копейки;
-- доля: базисные пункты. Никаких float.

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES tenants(tenant_id),
    name         TEXT NOT NULL,
    forge        TEXT NOT NULL DEFAULT 'github',
    repo         TEXT,
    timezone     TEXT NOT NULL DEFAULT 'Europe/Moscow',
    default_branch TEXT NOT NULL DEFAULT 'main'
);

CREATE TABLE IF NOT EXISTS participants (
    participant_id TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL REFERENCES tenants(tenant_id),
    display_name   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','paused','good_leaver','bad_leaver'))
);

CREATE TABLE IF NOT EXISTS identities (
    identity_id     TEXT PRIMARY KEY,
    participant_id  TEXT NOT NULL REFERENCES participants(participant_id),
    forge           TEXT NOT NULL,
    forge_login     TEXT NOT NULL,
    UNIQUE (forge, forge_login)          -- ADR-0014: один аккаунт на платформу
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id      TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    policy_version INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_versions (
    policy_id      TEXT NOT NULL REFERENCES policies(policy_id),
    effective_from TEXT NOT NULL,
    value_cents    INTEGER NOT NULL CHECK (value_cents > 0),
    reason         TEXT NOT NULL,
    approved_by    TEXT NOT NULL,
    PRIMARY KEY (policy_id, effective_from)   -- V-02/V-04 проверяются в коде
);

CREATE TABLE IF NOT EXISTS valuation_versions (
    policy_id      TEXT NOT NULL REFERENCES policies(policy_id),
    effective_from TEXT NOT NULL,
    value_rub      INTEGER NOT NULL CHECK (value_rub > 0),
    reason         TEXT NOT NULL,
    approved_by    TEXT NOT NULL,
    adr_ref        TEXT NOT NULL,            -- V-05
    PRIMARY KEY (policy_id, effective_from)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    task_ref       TEXT,                     -- NULL => no_task_link (не оплачивается)
    branch         TEXT,
    agent_attached INTEGER NOT NULL DEFAULT 1,
    started_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeats (
    event_id     TEXT PRIMARY KEY,           -- UUID: идемпотентность, защита от replay
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    seq          INTEGER NOT NULL,
    received_at  TEXT NOT NULL,
    signals      TEXT,                       -- JSON: input_events, focus, tool_calls...
    UNIQUE (session_id, seq)                 -- монотонный seq устройства
);

CREATE TABLE IF NOT EXISTS intervals (
    interval_id TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    cls         TEXT NOT NULL CHECK (cls IN
                ('active_human','supervised_agent','autonomous_agent','idle')),
    start_utc   TEXT NOT NULL,
    end_utc     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS day_records (
    day_id            TEXT PRIMARY KEY,     -- participant_id|date
    participant_id    TEXT NOT NULL REFERENCES participants(participant_id),
    date              TEXT NOT NULL,        -- в поясе проекта
    tz                TEXT NOT NULL,
    paid_seconds      INTEGER NOT NULL,
    amount_cents      INTEGER NOT NULL,
    cash_cents        INTEGER NOT NULL DEFAULT 0,
    equity_cents      INTEGER NOT NULL DEFAULT 0,
    share_bp          TEXT NOT NULL,        -- Decimal как TEXT (никаких float)
    rate_version      INTEGER NOT NULL,
    valuation_version INTEGER NOT NULL,
    policy_version    INTEGER NOT NULL,
    sha256_input      TEXT NOT NULL,
    closed            INTEGER NOT NULL DEFAULT 1 CHECK (closed IN (0,1))
);

CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_id TEXT PRIMARY KEY,
    day_id        TEXT NOT NULL REFERENCES day_records(day_id),
    delta_cents   INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    approved_by   TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
-- day_records не обновляются: правка закрытого дня — только adjustments (append-only)

CREATE TABLE IF NOT EXISTS flags (
    day_id   TEXT NOT NULL REFERENCES day_records(day_id),
    code     TEXT NOT NULL,
    detail   TEXT,
    PRIMARY KEY (day_id, code)
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    month       TEXT NOT NULL,               -- YYYY-MM
    sha256      TEXT NOT NULL,               -- SHA-256 входных дневных записей
    git_ref     TEXT,                        -- якорь в git (commit/tag)
    created_at  TEXT NOT NULL
);
