"""M8: разовая джоба закрытия дня (compose-сервис `dayclose`, runbook §7).

Запуск: `python -m partner_server.dayclose_cli [--date YYYY-MM-DD]`

Без --date закрывает все незакрытые дни всех проектов до сегодняшнего дня
в поясе проекта (догоняющий прогон close_catch_up, как при старте сервера).
С --date закрывает указанный день всем участникам проекта с интервалами.

Закрытие идемпотентно (ADR-0017): повторный запуск дает те же sha256 и
не создает дублей — команда безопасна для повтора из runbook.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import dayclose, models
from .config import settings
from .db import make_engine, make_sessionmaker


def main(argv: list[str] | None = None, db_url: str | None = None) -> int:
    from partner_cli.console_io import configure_stdio

    configure_stdio()
    parser = argparse.ArgumentParser(
        description="Разовое закрытие дня (идемпотентно, ADR-0017)"
    )
    parser.add_argument(
        "--date",
        default=None,
        help="день YYYY-MM-DD в поясе проекта; по умолчанию — все незакрытые до сегодня",
    )
    args = parser.parse_args(argv)

    engine = make_engine(db_url or settings.db_url)
    if (db_url or settings.db_url).startswith("sqlite"):
        from .db import Base

        Base.metadata.create_all(engine)
    factory = make_sessionmaker(engine)

    closed_total: list[str] = []
    with factory() as sess:
        for project in sess.query(models.Project).all():
            tz = ZoneInfo(project.timezone)
            if args.date:
                # закрыть конкретный день всем участникам проекта с интервалами
                participant_ids = sess.scalars(
                    select(models.Session.participant_id)
                    .where(models.Session.project_id == project.project_id)
                    .distinct()
                ).all()
                for participant_id in sorted(participant_ids):
                    try:
                        from . import service

                        service.close_day(sess, project, participant_id, args.date)
                        closed_total.append(f"{participant_id}|{args.date}")
                    except Exception as err:  # noqa: BLE001
                        print(
                            f"! {participant_id}|{args.date}: {err}",
                            file=sys.stderr,
                        )
            else:
                today = datetime.now(tz).date().isoformat()
                closed_total += dayclose.close_catch_up(sess, project, today)
            # M6-обслуживание после закрытия (ретраи, споры, месяц)
            dayclose._m6_jobs(sess, project)

    if closed_total:
        print("закрыты дни:", ", ".join(closed_total))
    else:
        print("незакрытых дней нет — закрытие не потребовалось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
