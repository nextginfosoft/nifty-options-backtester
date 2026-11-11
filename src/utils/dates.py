from __future__ import annotations

from datetime import date, datetime, timedelta


def daterange(start_date: date, end_date: date) -> list[date]:
    days = (end_date - start_date).days
    return [start_date + timedelta(days=offset) for offset in range(days + 1)]


def parse_nse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%d-%b-%Y").date()


def parse_cdsl_archive_input(value: date) -> str:
    return f"{value.strftime('%B')} {value.day}, {value.year}"  # portable (no %-d on Windows)


def month_iter(start_year: int, end_year: int) -> list[tuple[int, int]]:
    items: list[tuple[int, int]] = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            items.append((year, month))
    return items
