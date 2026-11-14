#!/usr/bin/env sh
# M8.2: обновление версии без потери дней.
# Порядок (installation.md §4): backup → alembic upgrade → перезапуск → healthz.
#
# Запуск с хоста: deploy/scripts/upgrade.sh [VERSION]
#   VERSION — тег нового образа (например partner:0.8.1); по умолчанию latest.
set -eu

cd "$(dirname "$0")/.."   # deploy/
VERSION="${1:-latest}"
export PARTNER_IMAGE="partner:$VERSION"

echo "1/5 сборка образа partner:$VERSION"
docker compose build server

echo "2/5 бэкап перед миграцией"
docker compose exec -T backup /srv/scripts/backup.sh once \
    || docker compose run --rm backup /srv/scripts/backup.sh once

echo "3/5 миграции схемы (alembic upgrade head)"
# run --rm возвращает код контейнера: ненулевой код — прерываем до перезапуска server
docker compose run --rm migrate || {
    echo "ОШИБКА: миграция не прошла — сервер НЕ перезапущен (откат: rollback.sh)" >&2
    exit 1
}

echo "4/5 перезапуск сервера"
docker compose up -d server

echo "5/5 ожидание /healthz"
for i in $(seq 1 30); do
    if curl -fsS "http://${PARTNER_BIND:-127.0.0.1:8080}/healthz" > /dev/null 2>&1; then
        echo "обновление до partner:$VERSION завершено"
        exit 0
    fi
    sleep 2
done
echo "ОШИБКА: /healthz не отвечает — см. docker compose logs server; откат: rollback.sh" >&2
exit 1
