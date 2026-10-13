import json
import logging
import re
import time
from datetime import timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Kwork и Freelance.ru отдают время по Москве без указания пояса
MSK = timezone(timedelta(hours=3), "MSK")


def parse_timezone(value: str) -> tzinfo:
    """"UTC+4", "+04:00", "-3" или имя IANA ("Europe/Samara") -> tzinfo"""
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", value.strip(), re.IGNORECASE)
    if m:
        sign, hours, minutes = m.groups()
        offset = timedelta(hours=int(hours), minutes=int(minutes or 0))
        return timezone(-offset if sign == "-" else offset)
    if value.strip().upper() in ("UTC", "GMT"):
        return timezone.utc
    try:
        return ZoneInfo(value.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Неизвестный часовой пояс: {value!r} (пример: UTC+4 или Europe/Samara)") from None


def format_money(amount: float | None, currency: str | None, default: str) -> str:
    """5000.0, "RUB" -> "5000 RUB"; None -> default"""
    if amount is None:
        return default
    return f"{amount:g} {currency or ''}".strip()


class SeenStore:
    """Файловый кэш уже проанализированных заказов (чтобы не тратить LLM повторно)"""

    def __init__(self, path: str, ttl_days: float):
        self.path = Path(path)
        self.ttl = ttl_days * 86400
        self._data: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        now = time.time()
        self._data = {k: t for k, t in raw.items() if now - t < self.ttl}

    def __contains__(self, job_id: str) -> bool:
        return job_id in self._data

    def add(self, job_id: str) -> None:
        self._data[job_id] = time.time()

    def save(self) -> None:
        # через временный файл: прерванная запись не должна обнулить кэш
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data))
        tmp.replace(self.path)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
