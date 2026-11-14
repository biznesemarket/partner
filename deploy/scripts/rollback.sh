#!/usr/bin/env sh
# M8.2: откат версии (сегментация сбоя миграции, runbook §7).
#
#   deploy/scripts/rollback.sh [PREVIOUS_VERSION] [дамп.dump.gz]
#   PREVIOUS_VERSION — тег образа, с которого обновлялись (по умолчанию из .env PARTNER_IMAGE_PREV)
#   дамп — опционально: восстановить БД на момент до неудачного обновления
#           (нужен ТОЛЬКО если миграция изменила данные; downgrade схемы не трогает дни).
set -eu

cd "$(dirname "$0")/.."   # deploy/
VERSION="${1:-${PARTNER_IMAGE_PREV:-latest}}"
DUMP="${2:-}"

echo "1/3 откат образа на partner:$VERSION"
PARTNER_IMAGE="partner:$VERSION" docker compose up -d --no-deps server

if [ -n "$DUMP" ]; then
    echo "2/3 восстановление БД из $DUMP (боевая БД будет заменена)"
    echo "   ввод 'yes' для подтверждения:"
    [ "$(head -n1)" = "yes" ] || { echo "отменено"; exit 1; }
    docker compose exec -T db psql -d postgres -c \
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='partner' AND pid <> pg_backend_pid()" \
      > /dev/null
    docker compose exec -T db dropdb --if-exists partner_tmp_rollback
    gunzip -c "$DUMP" | docker compose exec -T db pg_restore -d partner --no-owner --clean --if-exists --exit-on-error
    echo "   БД восстановлена из дампа"
else
    echo "2/3 БД не трогаем: схема откатывается только при явной необходимости"
    echo "   (alembic downgrade на своей риск — дни закрыты и неизменны, V-02/V-04)"
fi

echo "3/3 проверка /healthz"
for i in $(seq 1 30); do
    curl -fsS "http://${PARTNER_BIND:-127.0.0.1:8080}/healthz" > /dev/null 2>&1 && {
        echo "откат на partner:$VERSION завершен"; exit 0; }
    sleep 2
done
echo "ОШИБКА: /healthz не отвечает — docker compose logs server" >&2
exit 1
