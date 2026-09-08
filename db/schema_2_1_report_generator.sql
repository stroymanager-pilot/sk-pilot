-- Шаг 2, подзадача 2-1: схема генератора ежемесячного отчёта.
-- PostgreSQL. Выполняется на боевой базе вручную:
--
--   sudo -u postgres psql -d sk_pilot -f db/schema_2_1_report_generator.sql
--
-- auto_migrate() под PostgreSQL не выполняется (ранний выход в app.py),
-- поэтому изменения схемы вносятся только этим скриптом плюс правкой
-- db/schema_postgres.sql для новых развёртываний.
--
-- Скрипт идемпотентен: CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS,
-- наполнение через ON CONFLICT DO NOTHING / WHERE NOT EXISTS.
-- Повторный запуск ничего не изменит и не создаст дублей.
--
-- Данные не удаляются и не изменяются: скрипт только добавляет.

BEGIN;

-- ─────────────────────────────────────────────────────────────
-- 2.1. ГРУППЫ ОТЧЁТОВ
-- Отчёт строится по группе объектов, а не по объекту.
-- Состав группы меняется во времени: дом передан собственникам —
-- закрывается date_to; новый дом входит с date_from.
-- ─────────────────────────────────────────────────────────────

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

-- ─────────────────────────────────────────────────────────────
-- 2.6. ДОПОЛНИТЕЛЬНЫЕ ПОЛЯ СУЩЕСТВУЮЩИХ ТАБЛИЦ
-- Все nullable, без значений по умолчанию: добавление затрагивает
-- только метаданные, таблицы не переписываются, простоя нет.
-- ─────────────────────────────────────────────────────────────

ALTER TABLE objects ADD COLUMN IF NOT EXISTS report_name               TEXT;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS object_type               TEXT;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS contract_date             TEXT;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS contract_amendment        TEXT;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS client_partner_id         INTEGER;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS client_signatory          TEXT;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS client_signatory_role     TEXT;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS contractor_signatory      TEXT;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS contractor_signatory_role TEXT;

-- корпус / зона / общая площадка. Ведомость и проценты — только по «корпус».
ALTER TABLE sections ADD COLUMN IF NOT EXISTS section_type TEXT;

ALTER TABLE prescriptions_log ADD COLUMN IF NOT EXISTS issued_by_name TEXT;

-- ─────────────────────────────────────────────────────────────
-- ИНДЕКСЫ
-- ─────────────────────────────────────────────────────────────

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

-- ─────────────────────────────────────────────────────────────
-- НАЧАЛЬНОЕ НАПОЛНЕНИЕ
-- ─────────────────────────────────────────────────────────────

-- Группы отчётов
INSERT INTO report_groups (name, report_title) VALUES
    ('Суздальское',  'Суздальское'),
    ('Школа',        'Школа'),
    ('ЖК Гатчина',   'ЖК Гатчина'),
    ('ДОУ Гатчина',  'ДОУ Гатчина')
ON CONFLICT (name) DO NOTHING;

-- Состав групп. Вставляем только если такой пары ещё нет с открытым периодом.
INSERT INTO report_group_objects (report_group_id, object_id)
SELECT g.id, v.object_id
  FROM (VALUES
        ('Суздальское', 11),
        ('Суздальское', 12),
        ('Школа',       13),
        ('ЖК Гатчина',   2),
        ('ДОУ Гатчина', 14)
       ) AS v(group_name, object_id)
  JOIN report_groups g ON g.name = v.group_name
 WHERE NOT EXISTS (
        SELECT 1 FROM report_group_objects x
         WHERE x.report_group_id = g.id
           AND x.object_id = v.object_id
       );

-- Шаблон по образцу отчётов за июль 2026
INSERT INTO report_templates (name) VALUES ('Базовый отчёт 2026')
ON CONFLICT (name) DO NOTHING;

-- Блоки шаблона. Перечень фиксирован.
-- fill_mode: auto — собирается из данных Сводки;
--            manual — ведётся в реестрах (подзадача 2-3);
--            external — приходит извне (TeamJect).
-- safety выключен: заказчик подтвердил, что предписания по ТБ не нужны.
INSERT INTO report_template_blocks (template_id, block_key, is_enabled, fill_mode, sort_order)
SELECT t.id, v.block_key, v.is_enabled, v.fill_mode, v.sort_order
  FROM (VALUES
        ('title',                  1, 'auto',     10),
        ('control_summary',        1, 'auto',     20),
        ('deviations',             1, 'auto',     30),
        ('prescriptions',          1, 'auto',     40),
        ('author_supervision',     1, 'manual',   50),
        ('progress',               1, 'manual',   60),
        ('volume_changes',         1, 'manual',   70),
        ('organizations',          1, 'auto',     80),
        ('personnel',              1, 'auto',     90),
        ('schedule',               1, 'manual',  100),
        ('design_docs',            1, 'manual',  110),
        ('as_built_docs',          1, 'auto',    120),
        ('safety',                 0, 'external',130),
        ('general',                1, 'manual',  140),
        ('photos',                 1, 'auto',    150),
        ('prescriptions_appendix', 1, 'external',160)
       ) AS v(block_key, is_enabled, fill_mode, sort_order)
 CROSS JOIN (SELECT id FROM report_templates WHERE name = 'Базовый отчёт 2026') t
ON CONFLICT (template_id, block_key) DO NOTHING;

COMMIT;

-- ─────────────────────────────────────────────────────────────
-- КОНТРОЛЬ
-- ─────────────────────────────────────────────────────────────

\echo '--- Созданные таблицы ---'
SELECT table_name
  FROM information_schema.tables
 WHERE table_schema = 'public'
   AND table_name IN ('report_groups','report_group_objects','report_templates',
                      'report_template_blocks','monthly_reports','monthly_report_blocks',
                      'author_supervision','volume_changes','schedule_notes',
                      'design_approvals','work_types','object_work_types','monthly_progress')
 ORDER BY table_name;

\echo '--- Добавленные колонки ---'
SELECT table_name, column_name
  FROM information_schema.columns
 WHERE table_schema = 'public'
   AND ((table_name = 'objects' AND column_name IN
         ('report_name','object_type','contract_date','contract_amendment',
          'client_partner_id','client_signatory','client_signatory_role',
          'contractor_signatory','contractor_signatory_role'))
     OR (table_name = 'sections' AND column_name = 'section_type')
     OR (table_name = 'prescriptions_log' AND column_name = 'issued_by_name'))
 ORDER BY table_name, column_name;

\echo '--- Группы отчётов и их объекты ---'
SELECT g.id, g.name AS группа, o.id AS объект, o.name AS наименование, o.is_active
  FROM report_groups g
  LEFT JOIN report_group_objects rgo ON rgo.report_group_id = g.id
  LEFT JOIN objects o ON o.id = rgo.object_id
 ORDER BY g.id, o.id;

\echo '--- Блоки шаблона ---'
SELECT b.sort_order, b.block_key, b.is_enabled, b.fill_mode
  FROM report_template_blocks b
  JOIN report_templates t ON t.id = b.template_id
 WHERE t.name = 'Базовый отчёт 2026'
 ORDER BY b.sort_order;

\echo '--- Данные на месте ---'
SELECT (SELECT COUNT(*) FROM daily_reports) AS сводок,
       (SELECT COUNT(*) FROM objects WHERE is_active = 1) AS активных_объектов,
       (SELECT COUNT(*) FROM users WHERE COALESCE(is_active,1) = 1) AS активных_пользователей;
