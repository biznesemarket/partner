# Публикация и настройка GitHub-репозитория

| Поле | Значение |
|---|---|
| Версия | 1.0 |
| Дата | 2026-11-14 |
| Аудитория | владелец репозитория, тимлид |
| Репозиторий | https://github.com/biznesemarket/partner (private) |

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
из `docs/05-process/backlog.md` и защиту `main` (PR + 1 ревью + CI).

Если защита не включилась (приватный репозиторий на бесплатном плане):
Settings → Branches → Add branch protection rule для `main`:

- Require a pull request before merging (1 approval, dismiss stale reviews);
- Require status checks: `test (ubuntu-latest)`, `test (windows-latest)`;
- Require conversation resolution;
- Do not allow force pushes.

## 3. Проверка после публикации

| Проверка | Ожидание |
|---|---|
| Actions → partner-ci | зеленый на Linux и Windows |
| Issues → New issue | доступны шаблоны `partner-issue` и `contribution-dispute` |
| Settings → Actions | workflows включены, секреты не требуются |
| Code → main | README, docs, сервер, CLI, тесты на месте |
| Releases | тег `v1.0.0` (по желанию: `git tag -a v1.0.0 -m "Partner v1.0.0" && git push origin v1.0.0`) |

## 4. Что не автоматизировано

- Выдача лицензионных ключей (`tools/license_issue.py`) — вендорский
  процесс, реестр `license-registry.json` не коммитится.
- Публикация `partner-tracker` в приватный индекс — отдельная задача
  (backlog B-05).
- Живой пилот у клиента — организационный пункт акта приемки.
