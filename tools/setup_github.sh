#!/usr/bin/env bash
# Настройка GitHub-репозитория «Партнер»: описание, topics, метки, вехи,
# backlog-issues и защита main. Требует gh с правами admin на репозиторий.
#
# Запуск:  ./tools/setup_github.sh [owner/repo]
# По умолчанию: biznesemarket/partner
#
# Скрипт идемпотентен: метки обновляются (--force), существующие issues
# с тем же заголовком пропускаются, вехи создаются один раз.
set -euo pipefail

REPO="${1:-biznesemarket/partner}"
echo "== настройка $REPO =="
gh auth status >/dev/null || { echo "ОШИБКА: gh не авторизован"; exit 1; }

gh repo edit "$REPO" \
  --description "«Партнёр» — self-hosted учет времени работы с ИИ-агентами, ежедневное начисление вознаграждений и долей" \
  --add-topic python \
  --add-topic fastapi \
  --add-topic time-tracking \
  --add-topic ai-agents \
  --add-topic self-hosted \
  --add-topic equity \
  --add-topic partner

echo "-- метки"
create_label() { gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force >/dev/null; }
create_label "p0"           "b60205" "Блокирует сквозной продукт"
create_label "p1"           "d93f0b" "Важно для ближайшего релиза"
create_label "p2"           "fbca04" "Можно после v1.1"
create_label "agent-kit"    "1d76db" "Плагин IDE и MCP-сервер"
create_label "connectors"   "5319e7" "GitHub / Forgejo / Gitea"
create_label "docs"         "0075ca" "Документация"
create_label "tech-debt"    "6b7280" "Технический долг"
create_label "security"     "b60205" "Безопасность и изоляция"
create_label "ci"           "0e8a16" "CI и поставка"
create_label "dependencies" "6b7280" "Обновления зависимостей"
create_label "paid-task"    "0e8a16" "Оплачиваемая задача (issue-шаблон)"
create_label "dispute"      "fbca04" "Спор по начислению (issue-шаблон)"

echo "-- вехи"
create_milestone() {
  gh api "repos/$REPO/milestones" -f title="$1" -f description="$2" >/dev/null 2>&1 \
    || echo "   веха '$1' уже есть"
}
create_milestone "v1.1 — сквозной учет" "agent-kit, коннекторы, no_output, PostgreSQL в CI"
create_milestone "v1.2 — эксплуатация" "tenant/RBAC, доставка agent_chat, публикация пакета, инвариант закрытого дня"
create_milestone "v1.3 — витрина" "веб-дашборд директора, отчеты, публичный docs-сайт"

echo "-- issues"
create_issue() {
  local title="$1" labels="$2" milestone="$3" body="$4"
  local existing
  existing="$(gh issue list --repo "$REPO" --state all --search "$title in:title" --json title --jq '.[].title' 2>/dev/null || true)"
  if printf '%s\n' "$existing" | grep -Fxq "$title"; then
    echo "   пропуск (уже есть): $title"
    return
  fi
  gh issue create --repo "$REPO" --title "$title" --label "$labels" \
    --milestone "$milestone" --body "$body" >/dev/null
  echo "   создан: $title"
}

create_issue "[P0] partner-agent-kit: плагин IDE и MCP-сервер" \
  "p0,agent-kit" "v1.1 — сквозной учет" \
  "Реализовать контракт docs/03-architecture/agent-kit.md: сессии, heartbeat 30 с, офлайн-буфер, подпись устройства, MCP-инструменты. Приемка: T-сценарии через кит, replay/duplicate, подпись, no_task_link, отсутствие кода в телеметрии."

create_issue "[P0] Коннекторы GitHub/Forgejo: ветки, PR, задачи, вебхуки" \
  "p0,connectors" "v1.1 — сквозной учет" \
  "Реализовать контракт docs/03-architecture/connectors.md: вебхуки push/pull_request/issues, проверка подписи, привязка сессии к задаче и ветке, факты коммитов/PR. Начать с GitHub, затем Forgejo/Gitea."

create_issue "[P1] Реализовать флаг no_output" \
  "p1,tech-debt,security" "v1.1 — сквозной учет" \
  "Флаг описан в time-accounting.md §флаги и connectors.md, но не реализован. Считать по signals (input_events/focus/tool_calls) и фактам коннектора; > 3 оплачиваемых часов без результата → флаг. Добавить тесты и explain."

create_issue "[P1] Tenant/RBAC hardening и фильтры API" \
  "p1,security,tech-debt" "v1.2 — эксплуатация" \
  "status/summary, disputes, notifications, audit, snapshots не фильтруются по tenant/project; DayRecord не хранит project_id. Добавить tenant-контекст (токен/заголовок), project_id в day_records, тесты изоляции."

create_issue "[P1] Docker Compose + PostgreSQL в CI" \
  "p1,ci" "v1.1 — сквозной учет" \
  "Добавить job с service postgres:16: alembic upgrade head, запуск сервера, smoke /healthz, закрытие дня. Сейчас PG проверен только offline и SQLite."

create_issue "[P1] Публикация partner-tracker и repo-pack workflow" \
  "p1,tech-debt,ci" "v1.2 — эксплуатация" \
  "repo-pack/workflows и quickstart ссылаются на pip install partner-tracker, но пакет не публикуется. Настроить приватный индекс/артефакт поставки и проверить workflow на чистом репозитории клиента."

create_issue "[P1] Инвариант закрытого дня: запрет перезаписи" \
  "p1,tech-debt,security" "v1.2 — эксплуатация" \
  "service.close_day перезаписывает day_record при повторном закрытии и изменившихся данных. Решить: запрет после окна или компенсирующая запись; ADR + тест идемпотентности/неизменности."

create_issue "[P2] Документация D4/D6: overview, connectors, гайды" \
  "p2,docs" "v1.2 — эксплуатация" \
  "Закрыть хвосты docs-plan: полный C4 overview, протоколы connectors, morning-developer.md, director-day.md; юридический модуль — ревью юриста (D7)."

create_issue "[P2] Веб-дашборд директора" \
  "p2" "v1.3 — витрина" \
  "Вне MVP: сводка, флаги, споры, снапшоты, экспорт. Требования — после v1.1 (backlog v1.3)."

echo "-- защита main (требует прав admin; на приватном репозитории может требовать GitHub Pro)"
PROTECTION_JSON="$(mktemp)"
cat > "$PROTECTION_JSON" <<'JSON'
{
  "required_status_checks": {"strict": true, "contexts": ["test (ubuntu-latest)", "test (windows-latest)"]},
  "enforce_admins": false,
  "required_pull_request_reviews": {"dismiss_stale_reviews": true, "required_approving_review_count": 1},
  "restrictions": null,
  "required_conversation_resolution": true
}
JSON
if gh api -X PUT "repos/$REPO/branches/main/protection" --input "$PROTECTION_JSON" >/dev/null; then
  echo "   main защищен: PR + 1 ревью + CI (ubuntu/windows)"
else
  echo "ПРЕДУПРЕЖДЕНИЕ: защита main не включена (план/права GitHub); настройте в Settings → Branches"
fi
rm -f "$PROTECTION_JSON"

echo "== готово =="
