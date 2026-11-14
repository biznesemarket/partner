#!/usr/bin/env sh
# M8.3: репетиция восстановления — восстанавливает дамп во временную БД
# на том же экземпляре PostgreSQL и сверяет содержимое с боевой БД.
#
# Запуск (runbook §потеря данных / ежеквартально):
#   docker compose exec backup /srv/scripts/restore_check.sh [файл.dump.gz]
#   без аргумента — берется последний partner_*.dump.gz в PARTNER_BACKUP_DIR
#
# Проверки: схемы 0004_m8; число таблиц; количество day_records и сумма
# amount_cents совпадают с боевой БД; sha256 первой закрытой записи.
# Код возврата 0 — восстановление отрепетировано успешно.
set -eu

BACKUP_DIR="${PARTNER_BACKUP_DIR:-/srv/backups}"
CHECK_DB="partner_restore_check_$$"

pick_dump() {
    if [ "${1:-}" != "" ]; then printf '%s\n' "$1"; return; fi
    ls -1t "$BACKUP_DIR"/partner_*.dump.gz 2>/dev/null | head -n 1 || true
}

DUMP="$(pick_dump "${1:-}")"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
    echo "ОШИБКА: дампов нет — сначала backup.sh once" >&2; exit 1
fi
echo "репетиция восстановления: $DUMP → временная БД $CHECK_DB"

cleanup() {
    psql -d postgres -c "DROP DATABASE IF EXISTS $CHECK_DB" > /dev/null 2>&1 || true
}
trap cleanup EXIT

# 1. восстановление во временную БД (это и есть репетиция)
psql -d postgres -c "CREATE DATABASE $CHECK_DB" > /dev/null
gunzip -c "$DUMP" | pg_restore -d "$CHECK_DB" --no-owner --exit-on-error 2>/dev/null \
    || { echo "ОШИБКА: pg_restore не смог восстановить дамп" >&2; exit 1; }

fail() { echo "ОШИБКА проверки: $1" >&2; exit 1; }

# 2. версия схемы
CHECKED_SCHEMA="$(psql -d "$CHECK_DB" -t -A -c "SELECT version_num FROM alembic_version LIMIT 1")"
LIVE_SCHEMA="$(psql -d "$PGDATABASE" -t -A -c "SELECT version_num FROM alembic_version LIMIT 1")"
[ "$CHECKED_SCHEMA" = "$LIVE_SCHEMA" ] || fail "версия схемы $CHECKED_SCHEMA ≠ боевой $LIVE_SCHEMA"
echo "ok: версия схемы alembic = $CHECKED_SCHEMA"

# 3. число таблиц (14 моделей + alembic_version = 15)
TABLES="$(psql -d "$CHECK_DB" -t -A -c \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")"
[ "$TABLES" -ge 15 ] || fail "в restored-БД таблиц $TABLES, ожидалось ≥ 15"
echo "ok: таблиц в восстановленной БД = $TABLES"

# 4. day_records: количество и сумма совпадают с боевой БД
SRC_ROWS="$(psql -d "$PGDATABASE" -t -A -c "SELECT count(*) FROM day_records")"
RST_ROWS="$(psql -d "$CHECK_DB" -t -A -c "SELECT count(*) FROM day_records")"
[ "$SRC_ROWS" = "$RST_ROWS" ] || fail "day_records: $RST_ROWS ≠ $SRC_ROWS"

SRC_SUM="$(psql -d "$PGDATABASE" -t -A -c "SELECT coalesce(sum(amount_cents),0) FROM day_records")"
RST_SUM="$(psql -d "$CHECK_DB" -t -A -c "SELECT coalesce(sum(amount_cents),0) FROM day_records")"
[ "$SRC_SUM" = "$RST_SUM" ] || fail "sum(amount_cents): $RST_SUM ≠ $SRC_SUM"
echo "ok: day_records = $RST_ROWS, сумма начислений (копейки) = $RST_SUM"

# 5. целостность первой закрытой записи: sha256_input совпадает
FIRST_SHA_SRC="$(psql -d "$PGDATABASE" -t -A -c \
  "SELECT sha256_input FROM day_records ORDER BY day_id LIMIT 1")"
FIRST_SHA_RST="$(psql -d "$CHECK_DB" -t -A -c \
  "SELECT sha256_input FROM day_records ORDER BY day_id LIMIT 1")"
[ "$FIRST_SHA_SRC" = "$FIRST_SHA_RST" ] || fail "sha256 первой записи расходится"
echo "ok: sha256 первой закрытой записи совпадает"

echo "РЕПЕТИЦИЯ ВОССТАНОВЛЕНИЯ ПРОЙДЕНА: $DUMP"
