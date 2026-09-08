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
    -- Реквизиты для ежемесячного отчёта (шаг 2-1)
    report_name               TEXT,   -- наименование объекта для заказчика
    object_type               TEXT,
    contract_date             TEXT,
    contract_amendment        TEXT,
    client_partner_id         INTEGER,  -- ссылка на partners.id, без жёсткого FK
    client_signatory          TEXT,
    client_signatory_role     TEXT,
    contractor_signatory      TEXT,
    contractor_signatory_role TEXT,
    created_at      TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS sections (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id    INTEGER NOT NULL,
    name         TEXT NOT NULL,
    section_type TEXT,          -- корпус / зона / общая площадка (шаг 2-1)
    is_active    INTEGER DEFAULT 1
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
    status             TEXT,
    issued_by_name     TEXT          -- кем выдано (шаг 2-1)
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

-- ─────────────────────────────────────────────
-- ГЕНЕРАТОР ЕЖЕМЕСЯЧНОГО ОТЧЁТА (шаг 2-1)
-- Начальное наполнение — в db/schema_2_1_report_generator.sql,
-- здесь только структура.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS report_groups (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         TEXT NOT NULL,              -- внутреннее имя группы
    client_name  TEXT,                       -- заказчик
    report_title TEXT,                       -- наименование для заказчика
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    CONSTRAINT report_groups_name_key UNIQUE (name)
);

-- Уникальности по (группа, объект) намеренно НЕТ: объект может входить
-- в группу несколько раз с разными периодами.
CREATE TABLE IF NOT EXISTS report_group_objects (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_group_id INTEGER NOT NULL,
    object_id       INTEGER NOT NULL,
    date_from       TEXT,                    -- YYYY-MM-DD, NULL = с начала
    date_to         TEXT                     -- YYYY-MM-DD, NULL = бессрочно
);

-- ─────────────────────────────────────────────────────────────
-- 2.2. ШАБЛОНЫ
-- Набор блоков фиксирован, различается полнота, а не состав.
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS report_templates (
    id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name      TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT report_templates_name_key UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS report_template_blocks (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    template_id   INTEGER NOT NULL,
    block_key     TEXT NOT NULL,             -- фиксированный перечень, см. ниже
    is_enabled    INTEGER NOT NULL DEFAULT 1,
    fill_mode     TEXT NOT NULL DEFAULT 'auto',   -- auto / manual / external
    sort_order    INTEGER NOT NULL DEFAULT 0,
    settings_json TEXT,
    CONSTRAINT report_template_blocks_key UNIQUE (template_id, block_key)
);

-- ─────────────────────────────────────────────────────────────
-- 2.3. ОТЧЁТ
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS monthly_reports (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_group_id INTEGER NOT NULL,
    year            INTEGER NOT NULL,
    month           INTEGER NOT NULL,
    template_id     INTEGER,
    status          TEXT NOT NULL DEFAULT 'черновик',
                    -- черновик / на_проверке / утверждён / выпущен
    created_by      INTEGER,
    submitted_at    TEXT,
    reviewed_by     INTEGER,
    approved_at     TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    -- version в ключе: версии допускаются, случайный дубль черновика — нет
    CONSTRAINT monthly_reports_period_key UNIQUE (report_group_id, year, month, version)
);

-- Один блок на ключ в отчёте: повторная сборка обновляет строку (upsert),
-- поэтому ручные правки в content_json не задваиваются.
CREATE TABLE IF NOT EXISTS monthly_report_blocks (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id    INTEGER NOT NULL REFERENCES monthly_reports(id) ON DELETE CASCADE,
    block_key    TEXT NOT NULL,
    content_json TEXT,
    is_edited    INTEGER NOT NULL DEFAULT 0,
    updated_by   INTEGER,
    updated_at   TEXT,
    CONSTRAINT monthly_report_blocks_key UNIQUE (report_id, block_key)
);

-- ─────────────────────────────────────────────────────────────
-- 2.4. РЕЕСТРЫ РУЧНЫХ БЛОКОВ
-- Ручной блок — список записей с жизненным циклом, а не текст.
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS author_supervision (
    id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id  INTEGER NOT NULL,
    issue_date TEXT,
    author     TEXT,
    essence    TEXT,
    status     TEXT,
    closed_at  TEXT,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS volume_changes (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id        INTEGER NOT NULL,
    contractor_id    INTEGER,
    contract_ref     TEXT,
    work_name        TEXT,
    volume_estimate  TEXT,
    volume_fact      TEXT,
    project_sheet    TEXT,
    note             TEXT,
    created_at       TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS schedule_notes (
    id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id  INTEGER NOT NULL,
    essence    TEXT,
    event_date TEXT,
    status     TEXT,
    closed_at  TEXT,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS design_approvals (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id       INTEGER NOT NULL,
    decision        TEXT,
    initiated_at    TEXT,
    issued_at       TEXT,
    approval_status TEXT,
    note            TEXT
);

-- ─────────────────────────────────────────────────────────────
-- 2.5. ВРЕМЕННАЯ ТАБЛИЦА ПРОЦЕНТОВ
-- До реализации ведомости объёмов (шаг 4) проценты вводятся вручную.
-- Справочник видов работ в этой подзадаче НЕ наполняется.
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS work_types (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_type TEXT,
    group_name  TEXT,
    name        TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS object_work_types (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id    INTEGER NOT NULL,
    work_type_id INTEGER NOT NULL,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    is_active    INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT object_work_types_key UNIQUE (object_id, work_type_id)
);

CREATE TABLE IF NOT EXISTS monthly_progress (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_id    INTEGER NOT NULL,
    section_id   INTEGER,
    work_type_id INTEGER,
    year         INTEGER NOT NULL,
    month        INTEGER NOT NULL,
    percent      INTEGER,
    note         TEXT,
    updated_by   INTEGER,
    updated_at   TEXT
);

-- Индексы генератора отчёта
CREATE INDEX IF NOT EXISTS idx_rgo_group        ON report_group_objects (report_group_id);
CREATE INDEX IF NOT EXISTS idx_rgo_object       ON report_group_objects (object_id);
CREATE INDEX IF NOT EXISTS idx_mr_group_period  ON monthly_reports (report_group_id, year, month);
CREATE INDEX IF NOT EXISTS idx_mrb_report       ON monthly_report_blocks (report_id);
CREATE INDEX IF NOT EXISTS idx_rtb_template     ON report_template_blocks (template_id);
CREATE INDEX IF NOT EXISTS idx_authsup_object   ON author_supervision (object_id);
CREATE INDEX IF NOT EXISTS idx_volchg_object    ON volume_changes (object_id);
CREATE INDEX IF NOT EXISTS idx_schednotes_object ON schedule_notes (object_id);
CREATE INDEX IF NOT EXISTS idx_designappr_object ON design_approvals (object_id);
CREATE INDEX IF NOT EXISTS idx_progress_period  ON monthly_progress (object_id, year, month);
