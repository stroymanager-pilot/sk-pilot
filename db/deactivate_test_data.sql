BEGIN;
-- Тестовые проекты
UPDATE projects SET is_active=0 WHERE id IN (1, 3);  -- ЖК Окла, ЖК "Ромашка"
-- Партнёр, привязанный только к тестовым проектам
UPDATE partners SET is_active=0 WHERE id=2;  -- Специализированный застройщик «ЛСТ Эксперт»
-- Ранее выявленный тестовый мусор в партнёрах
UPDATE partners SET is_active=0 WHERE id IN (40,5,6,41,42,43);  -- Котики, Партнер, Партнер 12, тест тест 2, тест_12, тест_12
COMMIT;
