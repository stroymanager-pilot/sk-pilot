#!/bin/bash
# Ежедневная резервная копия базы SK-pilot (PostgreSQL).
#
# Кладёт дамп формата custom в /root/backups/, хранит последние 14 дней,
# более старые удаляет. Старые копии удаляются ТОЛЬКО после успешного
# создания новой — иначе неудачный запуск оставил бы систему без копий.
#
# Установка и запуск по расписанию описаны в README.
#
# Пароль берётся из /root/.pgpass (режим 600) и не передаётся аргументом:
# аргументы команды видны всем в выводе ps.

set -uo pipefail

BACKUP_DIR="${SK_BACKUP_DIR:-/root/backups}"
KEEP_DAYS="${SK_BACKUP_KEEP_DAYS:-14}"
LOG="${SK_BACKUP_LOG:-/var/log/sk-pilot-backup.log}"

PGHOST="${SK_PG_HOST:-localhost}"
PGPORT="${SK_PG_PORT:-5432}"
PGDATABASE="${SK_PG_DB:-sk_pilot}"
PGUSER="${SK_PG_USER:-sk}"

STAMP="$(date +%Y-%m-%d_%H%M)"
TARGET="${BACKUP_DIR}/sk_pilot_${STAMP}.dump"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" | tee -a "$LOG"; }

mkdir -p "$BACKUP_DIR" || { log "ОШИБКА: не удалось создать $BACKUP_DIR"; exit 1; }

if ! command -v pg_dump >/dev/null 2>&1; then
    log "ОШИБКА: pg_dump не найден. Установите postgresql-client."
    exit 1
fi

log "Начало копирования: ${PGDATABASE}@${PGHOST}:${PGPORT} → ${TARGET}"

# --no-password: при отсутствии .pgpass команда не зависнет в ожидании ввода
if ! pg_dump -Fc --no-password \
        -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
        -f "$TARGET" 2>>"$LOG"; then
    log "ОШИБКА: pg_dump завершился неудачно, копия не создана"
    rm -f "$TARGET"
    exit 1
fi

SIZE=$(stat -c%s "$TARGET" 2>/dev/null || stat -f%z "$TARGET" 2>/dev/null || echo 0)
if [ "$SIZE" -lt 1000 ]; then
    log "ОШИБКА: файл копии подозрительно мал (${SIZE} байт), удаляю"
    rm -f "$TARGET"
    exit 1
fi

# Проверяем, что дамп читается — иначе это не копия, а набор байтов
if command -v pg_restore >/dev/null 2>&1; then
    if ! pg_restore --list "$TARGET" >/dev/null 2>>"$LOG"; then
        log "ОШИБКА: созданный дамп не читается pg_restore, удаляю"
        rm -f "$TARGET"
        exit 1
    fi
fi

log "Готово: $(basename "$TARGET"), размер ${SIZE} байт"

# Чистка старых копий — только после успешного создания новой
REMOVED=$(find "$BACKUP_DIR" -maxdepth 1 -name 'sk_pilot_*.dump' \
          -type f -mtime "+${KEEP_DAYS}" -print -delete | wc -l)
log "Удалено копий старше ${KEEP_DAYS} дней: ${REMOVED}"
log "Всего копий в каталоге: $(find "$BACKUP_DIR" -maxdepth 1 -name 'sk_pilot_*.dump' -type f | wc -l)"
