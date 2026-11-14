"""Доставка уведомлений (M6.3): очередь + каналы + повтор при недоставке.

Каналы (development-plan.md §6.3, порядок фиксирован):
- agent_chat — основной: доставка в чат агента; в embedded-режиме текст
  попадает в журнал уведомлений и печатается CLI-командой `partner digest`;
- email — fallback: SMTP; включается, только если задан PARTNER_SMTP_HOST;
- webhook — мессенджеры: HTTP POST JSON на PARTNER_WEBHOOK_URL.

Канал считается «не настроенным» (пропускается), а не «отказавшим»:
недоставка — это ошибка активной попытки. После notify_max_attempts
неудачных попыток уведомление переводится в failed и попадает в метрику
ошибок доставки (M8.4).

Хук on_day_closed вызывается из service.close_day — дайджест дня
ставится в очередь сразу после закрытия (development-plan.md §6.1).
"""
from __future__ import annotations

import json
import logging
import smtplib
import urllib.request
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .config import settings

log = logging.getLogger("partner.notify")

SENT = "sent"
PENDING = "pending"
FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def queue(
    sess: Session,
    *,
    kind: str,
    recipient: str,
    payload: dict,
    text: str | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> str:
    """Поставить уведомление в очередь (status=pending). Возвращает id."""
    notification_id = f"ntf-{uuid.uuid4().hex[:12]}"
    sess.add(
        models.Notification(
            notification_id=notification_id,
            tenant_id=tenant_id,
            project_id=project_id,
            kind=kind,
            recipient=recipient,
            status=PENDING,
            payload_json=payload,
            text=text,
            created_at=_now(),
        )
    )
    sess.commit()
    return notification_id


# ---------- каналы ----------


def _send_agent_chat(sess: Session, n: models.Notification) -> None:
    """Основной канал: чат агента.

    Всегда пишет в журнал сервера. Если задан PARTNER_AGENT_CHAT_URL,
    дополнительно отправляет JSON в адаптер чата (мост IDE/мессенджера);
    ошибка адаптера — ошибка активной попытки, включается fallback.
    """
    log.info("дайджест [%s] для %s:\n%s", n.kind, n.recipient, n.text or "")
    if not settings.agent_chat_url:
        return
    body = json.dumps(
        {
            "notification_id": n.notification_id,
            "kind": n.kind,
            "recipient": n.recipient,
            "text": n.text,
            "payload": n.payload_json,
            "tenant_id": n.tenant_id,
            "project_id": n.project_id,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if settings.agent_chat_token:
        headers["Authorization"] = f"Bearer {settings.agent_chat_token}"
    req = urllib.request.Request(
        settings.agent_chat_url,
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"agent_chat вернул {resp.status}")


def _send_email(sess: Session, n: models.Notification) -> None:
    if not settings.smtp_host:
        raise RuntimeError("SMTP не настроен (PARTNER_SMTP_HOST пуст)")
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = settings.smtp_to or n.recipient
    msg["Subject"] = f"Партнер: {n.kind} для {n.recipient}"
    msg.set_content(n.text or json.dumps(n.payload_json, ensure_ascii=False))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        smtp.starttls()
        smtp.send_message(msg)


def _send_webhook(sess: Session, n: models.Notification) -> None:
    if not settings.webhook_url:
        raise RuntimeError("webhook не настроен (PARTNER_WEBHOOK_URL пуст)")
    body = json.dumps(
        {
            "kind": n.kind,
            "recipient": n.recipient,
            "text": n.text,
            "payload": n.payload_json,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        settings.webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"webhook вернул {resp.status}")


_SENDERS = {
    "agent_chat": _send_agent_chat,
    "email": _send_email,
    "webhook": _send_webhook,
}


def _channel_configured(channel: str) -> bool:
    if channel == "agent_chat":
        return True  # журнал сервера доступен всегда; URL — опциональный push
    if channel == "email":
        return bool(settings.smtp_host)
    if channel == "webhook":
        return bool(settings.webhook_url)
    return False


def deliver(sess: Session, n: models.Notification) -> bool:
    """Одна попытка доставки по цепочке каналов. True — доставлено.

    Не настроенный канал пропускается; ошибка активного канала учитывается
    в attempts/last_error. Исчерпание попыток → failed.
    """
    n.attempts = (n.attempts or 0) + 1
    error: str | None = None
    for channel in settings.notify_channels:
        if not _channel_configured(channel):
            continue
        try:
            _SENDERS[channel](sess, n)
            n.status = SENT
            n.channel = channel
            n.sent_at = _now()
            n.last_error = None
            sess.commit()
            return True
        except Exception as err:  # noqa: BLE001 — ошибка канала не роняет прием
            error = f"{channel}: {err}"
            log.warning("канал %s не доставил %s: %s", channel, n.notification_id, err)
    n.last_error = error or "нет настроенных каналов доставки"
    if n.attempts >= settings.notify_max_attempts:
        n.status = FAILED
    sess.commit()
    return False


def retry_pending(sess: Session) -> dict:
    """Повтор недоставленных уведомлений (вызывается планировщиком и при старте)."""
    rows = sess.scalars(
        select(models.Notification)
        .where(models.Notification.status == PENDING)
        .order_by(models.Notification.created_at)
    ).all()
    sent = 0
    for n in rows:
        if deliver(sess, n):
            sent += 1
    return {"pending": len(rows), "sent": sent}


def on_day_closed(
    sess: Session, project: models.Project, participant_id: str, date_iso: str, text: str, payload: dict
) -> str | None:
    """Хук после закрытия дня: дайджест дня в очередь (M6.1).

    Импортируется локально из service.close_day — здесь цикла нет.
    Доставка выполняется сразу; при недоставке уведомление остается в
    очереди и подхватывается retry_pending.
    """

    notification_id = queue(
        sess,
        kind="day_digest",
        recipient=participant_id,
        payload=payload,
        text=text,
        tenant_id=project.tenant_id,
        project_id=project.project_id,
    )
    n = sess.get(models.Notification, notification_id)
    deliver(sess, n)
    return notification_id


def dispatch_monthly(sess: Session, project: models.Project, month: str) -> list[str]:
    """Месячные дайджесты всем участникам с закрытыми днями месяца (M6.2)."""
    from . import digest  # локальный импорт

    ids: list[str] = []
    participant_ids = sorted(
        {
            pid
            for (pid,) in sess.execute(
                select(models.DayRecord.participant_id).where(
                    models.DayRecord.date.like(f"{month}%"),
                    models.DayRecord.project_id == project.project_id,
                )
            )
        }
    )
    for pid in participant_ids:
        try:
            md = digest.month_digest(sess, project, pid, month)
        except digest.DigestError:
            continue
        notification_id = queue(
            sess,
            kind="month_digest",
            recipient=pid,
            payload=md,
            text=digest.render_month_digest(md),
            tenant_id=project.tenant_id,
            project_id=project.project_id,
        )
        deliver(sess, sess.get(models.Notification, notification_id))
        ids.append(notification_id)
    return ids
