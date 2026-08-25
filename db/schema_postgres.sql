-- SK-pilot — целевая схема PostgreSQL.
-- Единственный источник правды по структуре базы.
--
-- Собрана объединением db/schema.py и CREATE TABLE внутри auto_migrate():
-- таблицы acceptance_control и ks2_check существовали только в app.py.
--
-- Отличия от SQLite-версии:
--   INTEGER PRIMARY KEY AUTOINCREMENT → GENERATED ALWAYS AS IDENTITY
--   DEFAULT (datetime('now'))         → DEFAULT now()
--   даты и время остаются TEXT: приложение хранит их строками ISO,
--   перевод в DATE/TIMESTAMPTZ — отдельная задача после переезда.
--
-- Скрипт идемпотентен: повторный запуск ничего не ломает.

-- ─────────────────────────────────────────────
-- ОРГАНИЗАЦИИ, ПРОЕКТЫ, ПАРТНЁРЫ
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS organizations (
    id                 INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name               TEXT NOT NULL,
    subscription_until TEXT,                    -- NULL = без ограничения
    is_active          INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    tj_project_id   TEXT,
    organization_id INTEGER,
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS partners (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            TEXT NOT NULL,
    type            TEXT,
    address         TEXT,
    contact_name    TEXT,
    contact_role    TEXT,
    inn             TEXT,
    phone           TEXT,
    email           TEXT,
    notes           TEXT,
    work_type       TEXT,
    project_id      INTEGER,                    -- совместимость: первый проект
    organization_id INTEGER,
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

-- Связь партнёров с проектами (многие ко многим). Без жёстких FK — связь логическая.
CREATE TABLE IF NOT EXISTS partner_projects (
    id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    partner_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    UNIQUE (partner_id, project_id)
);

-- ─────────────────────────────────────────────
-- ОБЪЕКТЫ, УЧАСТКИ, ПОДРЯДЧИКИ
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS objects (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tj_object_id    TEXT,
    name            TEXT NOT NULL,
    address         TEXT,
    client_name     TEXT,
    contract_number TEXT,
    project_id      INTEGER,
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS sections (
    id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id INTEGER NOT NULL,
    name      TEXT NOT NULL,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS contractors (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id       INTEGER NOT NULL,
    name            TEXT NOT NULL,
    work_type       TEXT,
    partner_id      INTEGER,                    -- ссылка на partners.id, без жёсткого FK
    hidden_manually INTEGER NOT NULL DEFAULT 0, -- 1 = скрыт администратором вручную
    is_active       INTEGER DEFAULT 1
);

-- ─────────────────────────────────────────────
-- ПОЛЬЗОВАТЕЛИ
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tj_user_id      TEXT,
    full_name       TEXT NOT NULL,
    email           TEXT UNIQUE NOT NULL,
    role            TEXT DEFAULT 'engineer',    -- platform / root / admin / senior / engineer
    password_hash   TEXT,
    organization_id INTEGER,
    is_active       INTEGER DEFAULT 1,
    can_view_all    INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS object_users (
    id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    date_from TEXT,
    date_to   TEXT,
    UNIQUE (object_id, user_id)
);

-- Личные участки инженера (не пересекаются с sections)
CREATE TABLE IF NOT EXISTS user_sections (
    id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id  INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    is_active  INTEGER DEFAULT 1,
    created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

-- ─────────────────────────────────────────────
-- ЕЖЕДНЕВНАЯ СВОДКА
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS daily_reports (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id    INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    report_date  TEXT NOT NULL,                 -- YYYY-MM-DD
    status       TEXT DEFAULT 'draft',          -- draft / submitted
    created_at   TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    submitted_at TEXT,
    UNIQUE (object_id, user_id, report_date)
);

-- Численность персонала. Без FK на contractors: подрядчик может быть скрыт.
CREATE TABLE IF NOT EXISTS personnel_entries (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id        INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    contractor_id    INTEGER NOT NULL,
    section_id       INTEGER,                   -- допускает личные участки
    headcount        INTEGER DEFAULT 0,
    work_description TEXT
);

CREATE TABLE IF NOT EXISTS input_control (
    id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id      INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    material_name  TEXT,
    quantity       TEXT,
    document_name  TEXT,
    deviation_note TEXT,
    status         TEXT DEFAULT '',
    section_id     INTEGER,
    contractor_id  INTEGER,
    engineer_id    INTEGER
);

CREATE TABLE IF NOT EXISTS operational_control (
    id                    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id             INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    section_id            INTEGER,
    work_stage            TEXT,
    controlled_operations TEXT,
    control_method        TEXT,
    status                TEXT DEFAULT '',
    deviation_note        TEXT DEFAULT '',
    contractor_id         INTEGER,
    engineer_id           INTEGER
);

CREATE TABLE IF NOT EXISTS acceptance_control (
    id                    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id             INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    section_id            INTEGER,
    work_stage            TEXT,
    controlled_operations TEXT,
    control_method        TEXT,
    status                TEXT DEFAULT '',
    deviation_note        TEXT DEFAULT '',
    contractor_id         INTEGER,
    engineer_id           INTEGER
);

CREATE TABLE IF NOT EXISTS ks2_check (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id       INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    contractor_id   INTEGER,
    contractor_name TEXT,
    object_work     TEXT,
    ks2_number      TEXT,
    ks3_number      TEXT,
    has_ks6a        INTEGER DEFAULT 0,
    has_ks3         INTEGER DEFAULT 0,
    has_id          INTEGER DEFAULT 0,
    engineer_id     INTEGER
);

CREATE TABLE IF NOT EXISTS verbal_remarks (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id   INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    section_id  INTEGER,
    description TEXT NOT NULL,
    deadline    TEXT,
    status      TEXT DEFAULT 'open',            -- open / closed
    issued_by   INTEGER,
    closed_at   TEXT,
    closed_note TEXT
);

CREATE TABLE IF NOT EXISTS prescriptions_log (
    id                 INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id          INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    tj_prescription_id TEXT,
    number             TEXT,
    issue_date         TEXT,
    section_id         INTEGER,
    deadline           TEXT,
    status             TEXT
);

CREATE TABLE IF NOT EXISTS meetings (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id     INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    location      TEXT,
    time          TEXT,
    participants  TEXT,
    agenda        TEXT,
    protocol_path TEXT,
    protocol_name TEXT,
    engineer_id   INTEGER
);

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id   INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    file_path   TEXT NOT NULL,
    caption     TEXT,
    sort_order  INTEGER DEFAULT 0,
    remark_id   INTEGER,
    uploaded_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

-- ─────────────────────────────────────────────
-- ИНДЕКСЫ
-- ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_reports_object_date ON daily_reports (object_id, report_date);
CREATE INDEX IF NOT EXISTS idx_reports_user        ON daily_reports (user_id);
CREATE INDEX IF NOT EXISTS idx_personnel_report    ON personnel_entries (report_id);
CREATE INDEX IF NOT EXISTS idx_photos_report       ON photos (report_id);
CREATE INDEX IF NOT EXISTS idx_remarks_status      ON verbal_remarks (status);
CREATE INDEX IF NOT EXISTS idx_objects_project     ON objects (project_id);
CREATE INDEX IF NOT EXISTS idx_contractors_object  ON contractors (object_id);
CREATE INDEX IF NOT EXISTS idx_contractors_partner ON contractors (partner_id);
CREATE INDEX IF NOT EXISTS idx_object_users_user   ON object_users (user_id);
CREATE INDEX IF NOT EXISTS idx_user_sections_owner ON user_sections (object_id, user_id);
