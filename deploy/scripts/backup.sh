#!/usr/bin/env sh
# M8.3: ежедневный бэкап PostgreSQL + проверка дампа сразу после снятия.
#
# Режимы:
#   backup.sh          — цикл: ежедневный dump в PARTNER_BACKUP_AT (по умолчанию 02:00)
#   backup.sh once     — разовый dump (для хостового cron: см. installation.md §5)
#
# Хранение: PARTNER_BACKUP_KEEP_DAYS дней (дефолт 14, ADR-0020).
# Каждый дамп сразу проверяется pg_restore --list — битый дамп не считается
# бэкапом. Лог: $PARTNER_BACKUP_DIR/backup.log.
set -eu

BACKUP_DIR="${PARTNER_BACKUP_DIR:-/srv/backups}"
KEEP_DAYS="${PARTNER_BACKUP_KEEP_DAYS:-14}"
AT="${PARTNER_BACKUP_AT:-02:00}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$BACKUP_DIR/partner_$STAMP.dump.gz"
LOG="$BACKUP_DIR/backup.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

dump_once() {
    mkdir -p "$BACKUP_DIR"
    log "начало дампа → $OUT"
    pg_dump -Fc | gzip > "$OUT"
    # битый дамп не считается бэкапом: проверка сразу после снятия
    if ! gzip -t "$OUT"; then
        log "ОШИБКА: дамп $OUT поврежден (gzip -t)"; rm -f "$OUT"; return 1
    fi
    if ! gunzip -c "$OUT" | pg_restore --list > /dev/null 2>&1; then
        log "ОШИБКА: pg_restore --list не читает $OUT"; rm -f "$OUT"; return 1
    fi
    size="$(du -h "$OUT" | cut -f1)"
    log "дамп готов и проверен: $OUT ($size)"
    prune_old
}

prune_old() {
    # хранение N дней: удаляем старее PARTNER_BACKUP_KEEP_DAYS
    find "$BACKUP_DIR" -name 'partner_*.dump.gz' -mtime "+$KEEP_DAYS" -print -delete \
        | while read -r f; do log "retention: удален $f (старше $KEEP_DAYS дн.)"; done
}

wait_next_run() {
    # ближайшее время HH:MM (сегодня или завтра)
    now="$(date +%s)"
    target="$(date -d "today $AT" +%s 2>/dev/null || date -j -f "%H:%M" "$AT" +%s)"
    if [ "$target" -le "$now" ]; then
        target="$(date -d "tomorrow $AT" +%s 2>/dev/null || echo $((target + 86400)))"
    fi
    log "следующий бэкап в $AT (сон $(( (target - now) / 60 )) мин)"
    sleep "$((target - now))"
}

case "${1:-loop}" in
    once) dump_once ;;
    *)
        log "планировщик бэкапов запущен (ежедневно в $AT, хранение $KEEP_DAYS дн.)"
        while true; do
            wait_next_run || true
            dump_once || log "ОШИБКА: бэкап не удался — алерт (runbook §алерты)"
        done
        ;;
esac
