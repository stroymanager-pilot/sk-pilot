BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- Привязка contractors → partners по partner_id
-- (миграция по имени не сопоставила из-за расхождений кавычек/регистра)
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE contractors SET partner_id=16 WHERE id=3;   -- Антан-Сервис
UPDATE contractors SET partner_id=29 WHERE id=5;   -- БестКлимат
UPDATE contractors SET partner_id=28 WHERE id=8;   -- КОРН
UPDATE contractors SET partner_id=20 WHERE id=12;  -- Лабрадор
UPDATE contractors SET partner_id=27 WHERE id=15;  -- МЕТИОР -> МЕТЕОР ЛИФТ
UPDATE contractors SET partner_id=17 WHERE id=4;   -- Максима Груп
UPDATE contractors SET partner_id=26 WHERE id=14;  -- НОРДКОМ
UPDATE contractors SET partner_id=19 WHERE id=9;   -- РСК
UPDATE contractors SET partner_id=33 WHERE id=7;   -- СИТИ
UPDATE contractors SET partner_id=24 WHERE id=10;  -- СПМУ -> рабочая карточка 24
UPDATE contractors SET partner_id=21 WHERE id=6;   -- Стройквадро
UPDATE contractors SET partner_id=25 WHERE id=11;  -- ТЭК СПб -> ТЭК
UPDATE contractors SET partner_id=18 WHERE id=16;  -- Х-Строй -> ИКССТРОЙ
UPDATE contractors SET partner_id=22 WHERE id=13;  -- Энсейв Констракшен

-- Пинстрой: все записи на объектах -> рабочая карточка 9
UPDATE contractors SET partner_id=9 WHERE id IN (1,44,46,48,54,57);
-- Если что-то уже успело привязаться к дублю 39 -> переносим на 9
UPDATE contractors SET partner_id=9 WHERE partner_id=39;

-- ─────────────────────────────────────────────────────────────────────────────
-- Дубль партнёра 39 (АО Пинстрой): убираем привязки к проектам и деактивируем
-- Рабочая карточка: id=9
-- ─────────────────────────────────────────────────────────────────────────────
DELETE FROM partner_projects WHERE partner_id=39;
UPDATE partners SET is_active=0 WHERE id=39;

-- ─────────────────────────────────────────────────────────────────────────────
-- Дубль партнёра 23 (СПМУ): привязок к проектам нет, деактивируем
-- Рабочая карточка: id=24
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE partners SET is_active=0 WHERE id=23;

-- ─────────────────────────────────────────────────────────────────────────────
-- Прочее
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE contractors SET is_active=0 WHERE id=187;   -- тест_1

COMMIT;
