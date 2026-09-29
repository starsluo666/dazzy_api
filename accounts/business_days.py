"""Reviewed Chinese work calendar; never silently treat statutory holidays as weekdays."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import ValidationError


# 国办发明电〔2025〕7号 (2025-11-04).
# https://www.beijing.gov.cn/cs/gncs/zcwj/202603/t20260327_4568275.html
DEFAULT_CALENDARS = {
    "2026": {
        "holidays": [
            "2026-01-01", "2026-01-02", "2026-01-03",
            *[f"2026-02-{day:02d}" for day in range(15, 24)],
            "2026-04-04", "2026-04-05", "2026-04-06",
            *[f"2026-05-{day:02d}" for day in range(1, 6)],
            "2026-06-19", "2026-06-20", "2026-06-21",
            "2026-09-25", "2026-09-26", "2026-09-27",
            *[f"2026-10-{day:02d}" for day in range(1, 8)],
        ],
        "working_weekends": [
            "2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09",
            "2026-09-20", "2026-10-10",
        ],
    },
}


def closure_deadline(requested_at):
    """Exclude the submission day; count five working days at the same Shanghai time."""
    calendars = {**DEFAULT_CALENDARS, **settings.ACCOUNT_CLOSURE_CALENDARS}
    local = requested_at.astimezone(ZoneInfo("Asia/Shanghai"))
    day = local.date()
    remaining = 5
    parsed = {}
    while remaining:
        day += timedelta(days=1)
        year = str(day.year)
        if year not in calendars:
            raise ValidationError("注销工作日历尚未配置完整，请联系客服后再申请。")
        if year not in parsed:
            try:
                calendar = calendars[year]
                holidays = {date.fromisoformat(value) for value in calendar["holidays"]}
                working = {date.fromisoformat(value) for value in calendar["working_weekends"]}
                if holidays & working or any(value.year != day.year for value in holidays | working):
                    raise ValueError("conflicting dates or wrong year")
                parsed[year] = holidays, working
            except (KeyError, TypeError, ValueError) as exc:
                raise ImproperlyConfigured("账号注销工作日历格式错误。") from exc
        holidays, working = parsed[year]
        if day in working or (day.weekday() < 5 and day not in holidays):
            remaining -= 1
    return local.replace(year=day.year, month=day.month, day=day.day)
