# Публикация и настройка GitHub-репозитория

| Поле | Значение |
|---|---|
| Версия | 1.1 |
| Дата | 2026-11-14 |
| Аудитория | владелец репозитория, тимлид |
| Репозиторий | https://github.com/biznesemarket/partner (public) |

## 1. Первый push

Локальный репозиторий уже содержит коммиты `main`. Если remote ещё не
настроен:

```bash
git remote add origin https://github.com/biznesemarket/partner.git
git fetch origin
git merge --allow-unrelated-histories origin/main   # только если в remote был README
git push -u origin main
```

Если remote содержит только автоматический README/.gitignore, конфликт
решается в пользу локальной версии:

```bash
git checkout --ours README.md .gitignore
git add README.md .gitignore
git commit -m "merge: keep project README and Python .gitignore"
git push -u origin main
```

Альтернатива без merge — одноразовый `git push --force-with-lease`, но
только если история remote не нужна.

## 2. Автоматическая настройка

```bash
gh auth status                          # аккаунт с правами admin на repo
./tools/setup_github.sh                 # или: ./tools/setup_github.sh owner/repo
```

Скрипт выставляет описание и topics, создает метки (`p0`, `p1`, `p2`,
`agent-kit`, `connectors`, `docs`, `tech-debt`, `security`, `ci`,
`dependencies`, `paid-task`, `dispute`), вехи `v1.1`–`v1.3`, backlog-issues
из `docs/05-process/backlog.md`, защиту `main` и security-функции.

Публичный репозиторий на Free-плане поддерживает branch protection.
Текущая защита `main` (идемпотентно выставляется скриптом):

- Require a pull request before merging (0 approvals — единственный
  коллаборатор, временное исключение AGENTS.md §2; `dismiss_stale_reviews`);
- Require status checks (strict): `test (ubuntu-latest)`,
  `test (windows-latest)`, `postgres-smoke`, `docker-build`,
  `package-smoke`;
- Require conversation resolution;
- Do not allow force pushes;
- Do not allow deletions;
- Enforce for administrators.

Если API не сработал: Settings → Branches → Add branch protection rule
для `main` и повторите те же пункты.

## 3. Безопасность публичного репозитория

Включено в Settings → Code security and analysis (скрипт выставляет
идемпотентно; часть функций зависит от плана GitHub):

| Функция | Назначение |
|---|---|
| Secret scanning | поиск секретов в истории и новых push |
| Secret scanning push protection | блокировка push с найденным секретом |
| Dependabot alerts + security updates | уязвимые зависимости |
| Private vulnerability reporting | приватный канал для исследователей (`SECURITY.md`) |
| CodeQL default setup (Python) | статический анализ на push/PR |

Проверка: `gh api repos/<owner/repo>/secret-scanning/alerts?state=open`
должен вернуть `[]` (алертов нет), а `gh api
repos/<owner/repo>/private-vulnerability-reporting` — `{"enabled": true}`.
Секреты в клиентские репозитории не коммитятся и не передаются в PR
публичного репозитория.

## 4. Проверка после публикации

| Проверка | Ожидание |
|---|---|
| Actions → partner-ci | зеленый на Linux и Windows |
| Actions → CodeQL | default setup для Python выполняется |
| Issues → New issue | доступны шаблоны `partner-issue` и `contribution-dispute` |
| Settings → Actions | workflows включены, секреты в PR из форков не нужны |
| Code → main | README, docs, сервер, CLI, тесты на месте |
| Releases | тег `v1.3.0` и `dist-manifest.json` (публичные артефакты) |

## 5. Что не автоматизировано

- Выдача лицензионных ключей (`tools/license_issue.py`) — вендорский
  процесс, реестр `license-registry.json` не коммитится.
- Приватный индекс — опционален для организаций; базовая поставка —
  публичный wheel из GitHub Release с проверкой `sha256` (B-05).
- Живой пилот у клиента — организационный пункт акта приемки.
