BEGIN;
INSERT INTO users (full_name, email, role, is_active, organization_id)
SELECT 'Никоненко Владислав', 'nikonenko@stroymanager.ru', 'root', 1, 1
WHERE NOT EXISTS (SELECT 1 FROM users WHERE email='nikonenko@stroymanager.ru');
COMMIT;
SELECT id, full_name, email, role FROM users WHERE email='nikonenko@stroymanager.ru';
