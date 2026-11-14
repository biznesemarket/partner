#!/usr/bin/env sh
# M9.2: сборка релиза — образ с тегом, контрольная сумма (подписанный артефакт).
#
# Запуск с хоста с Docker: deploy/scripts/release.sh 1.0.0
# Результат: dist/partner-<VER>.tar (docker save), dist/SHA256SUMS,
#            dist/RELEASE-<VER>.md — манифест для передачи клиенту.
#
# Подпись: SHA256SUMS подписывается ключом вендора (openssl dgst -sha256
# -sign vendor.key) при реальной поставке; в контуре без ключа — минимум
# sha256-манифест, сверяемый клиентом (installation.md §2).
set -eu

VER="${1:?укажите версию, например 1.0.0}"
cd "$(dirname "$0")/.."

echo "1/4 сборка образа partner:$VER"
docker build -t "partner:$VER" -f Dockerfile ..

echo "2/4 экспорт образа + контрольная сумма"
mkdir -p dist
docker save "partner:$VER" -o "dist/partner-$VER.tar"
sha256sum "dist/partner-$VER.tar" > dist/SHA256SUMS

echo "3/4 smoketest конфигурации compose"
docker compose config -q

echo "4/4 манифест релиза"
cat > "dist/RELEASE-$VER.md" <<EOF
# Релиз partner:$VER

| Поле | Значение |
|---|---|
| Версия | $VER |
| Дата | $(date +%F) |
| Образ | partner:$VER |
| SHA256 | $(sha256sum "dist/partner-$VER.tar" | cut -d' ' -f1) |
| git tag | v$VER (см. CHANGELOG.md) |

Установка: docs/07-ops/installation.md §2 (Docker Compose, .env, up -d).
Обновление с предыдущей версии: docs/07-ops/migration-guide.md.
Приемка: docs/05-process/release-acceptance.md; чек-лист —
development-plan.md §6.
EOF

echo "готово: dist/partner-$VER.tar, dist/SHA256SUMS, dist/RELEASE-$VER.md"
echo "передача клиенту: архив + SHA256SUMS (подпись вендора — openssl dgst)"
