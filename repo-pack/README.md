# repo-pack — пакет для репозитория проекта клиента

Пакет ставится в репозиторий проекта, где работают разработчики с
ИИ-агентами. Устанавливается мастером `partner init` без программиста
(см. `docs/00-start/quickstart.md`, `docs/03-architecture/onboarding-wizard.md`).

## Состав

| Файл | Куда попадает в репозитории клиента | Назначение |
|---|---|---|
| `AGENTS.block.md` | вставляется в `AGENTS.md` корня (маркеры BEGIN/END) | Правила учета времени, обязательные для ИИ-агента |
| `policy.example.yaml` | `policy.yaml` корня | Политика проекта: оценка, ставка, капы, коэффициенты, пул |
| `workflows/partner-policy-check.yml` | `.github/workflows/` | CI-проверка валидности политики при каждом PR |

## Порядок установки (кратко)

1. Установить `partner-tracker` из поставки:
   - приватный индекс: `pip install --index-url <PRIVATE_INDEX> partner-tracker`;
   - wheel из приватного GitHub Release: вендор выдает API URL asset и
     `sha256` из `dist-manifest.json`; скачать можно с токеном
     (`Authorization: Bearer <PAT>`, `Accept: application/octet-stream`)
     и установить локальный `.whl`;
   - локальная разработка: `pip install -e .` из этого репозитория.
   В CI клиента задайте секреты `PARTNER_TRACKER_WHEEL` (API URL asset),
   `PARTNER_TRACKER_TOKEN` (fine-grained PAT, Contents: Read-only на
   репозиторий вендора) и `PARTNER_TRACKER_SHA256`; альтернатива —
   `PARTNER_TRACKER_INDEX_URL`. Публичный PyPI не используется: без
   секретов workflow падает, а не ставит одноименный чужой пакет.
2. `partner init` — ответить на 10 обязательных вопросов; опциональные
   (юридические) можно пропустить и заполнить позже.
3. Проверить предпросмотр, подтвердить — мастер коммитит «policy v1.0».
4. Проверки: `python -m partner_cli.policy_lint policy.yaml` → OK;
   `partner day --demo` → 6,6 ч → 3 300 ₽ → 0,0330%.

## Правила изменения после установки

- Ставка, оценка, кап, коэффициент — только новыми версиями
  (`docs/02-policy/versioning.md`); заднее число запрещено.
- Блок AGENTS.md правится только мастером/CLI (обновление версии блока).
- Юридический блок включается после снятия гейтов юристом
  (`docs/06-legal/legal-module.md` §4).
