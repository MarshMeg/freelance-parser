import os
from pathlib import Path
from dotenv import load_dotenv

from utils import parse_timezone

# Загрузить переменные окружения
env_file = Path(__file__).parent / ".env"
load_dotenv(env_file)


class Config:
    """Конфигурация приложения"""

    # GitHub
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
    # репозиторий, в котором создаются Issues (может быть отдельным приватным)
    GITHUB_REPO = os.getenv("GITHUB_REPO") or "MarshMeg/freelance-parser"
    GITHUB_PROJECT_ID = int(os.getenv("GITHUB_PROJECT_ID") or "4")  # номер проекта из его URL
    # владелец проекта (user/org); по умолчанию — владелец репозитория
    GITHUB_PROJECT_OWNER = os.getenv("GITHUB_PROJECT_OWNER") or GITHUB_REPO.split("/")[0]
    # поле проекта и его значение (колонка), куда кладутся новые Issues
    GITHUB_PROJECT_STATUS_FIELD = os.getenv("GITHUB_PROJECT_STATUS_FIELD") or "Status"
    GITHUB_PROJECT_STATUS_OPTION = os.getenv("GITHUB_PROJECT_STATUS_OPTION") or "Backlog"
    # метка всех Issues парсера; по ней же ищутся уже созданные (дедупликация)
    GITHUB_ISSUE_LABEL = os.getenv("GITHUB_ISSUE_LABEL") or "freelance"

    # Anthropic API
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    ANALYZER_MODEL = os.getenv("ANALYZER_MODEL") or "claude-haiku-4-5"  # пустая строка из workflow = по умолчанию
    ANALYZER_CONCURRENCY = int(os.getenv("ANALYZER_CONCURRENCY", "3"))
    # стек исполнителя: подставляется в промпты анализатора и префильтра
    DEVELOPER_STACK = (
        os.getenv("DEVELOPER_STACK")
        or "Go, TypeScript, JavaScript, Python (бэкенд, API, боты, парсеры, автоматизация, веб-фронтенд)"
    )

    # Префильтр через OpenRouter (без ключа отключён): отсеивает явно неподходящие заказы до Claude
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
    PREFILTER_MODEL = os.getenv("PREFILTER_MODEL", "~typesafe/jev-latest")
    # отсев, если вероятность «заказ вне стека» не ниже порога; выше порог — осторожнее отсев
    PREFILTER_THRESHOLD = float(os.getenv("PREFILTER_THRESHOLD") or "0.8")

    # Парсинг
    # активные парсеры через запятую (имена — BaseParser.source): kwork, freelance_ru, freelancer
    ENABLED_PARSERS = [p.strip().lower() for p in (os.getenv("ENABLED_PARSERS") or "kwork,freelance_ru").split(",") if p.strip()]
    # категории ленты Freelance.ru (параметр c[]): 4 — «Веб-разработка и IT», 724 — «ИИ и Нейросети».
    # Без фильтра лента общая, и её забивают заказы не про разработку
    FREELANCE_RU_CATEGORIES = [c.strip() for c in (os.getenv("FREELANCE_RU_CATEGORIES") or "4,724").split(",") if c.strip()]
    # категория биржи проектов Kwork (параметр c): 41 — «Разработка и IT»; 0 — без фильтра
    KWORK_CATEGORY_ID = int(os.getenv("KWORK_CATEGORY_ID") or "41")
    # id навыков freelancer.com (параметр jobs[]): Python, JavaScript, Node.js, Go, TypeScript, React
    FREELANCER_SKILL_IDS = [
        int(s) for s in (os.getenv("FREELANCER_SKILL_IDS") or "13,9,759,1093,1612,305").split(",") if s.strip()
    ]
    PARSER_TIMEOUT = int(os.getenv("PARSER_TIMEOUT", "30"))
    # сколько помнить проанализированные заказы; должно быть дольше, чем заказ висит в ленте,
    # иначе нерекомендованные (без Issue) будут анализироваться повторно
    CACHE_DAYS = int(os.getenv("CACHE_DAYS", "30"))
    # потолок заказов на один запуск; 0 — без ограничения (обрабатываются все новые)
    MAX_JOBS_PER_RUN = int(os.getenv("MAX_JOBS_PER_RUN", "0"))
    SEEN_FILE = os.getenv("SEEN_FILE", ".cache/seen_jobs.json")

    # часовой пояс для дат в Issues: смещение (UTC+4, +04:00) или имя IANA (Europe/Samara)
    TIMEZONE = parse_timezone(os.getenv("TIMEZONE") or "UTC+4")

    # Логирование
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    # писать ли в лог названия заказов; false — только ID (логи Actions публичного репозитория видны всем)
    LOG_JOB_TITLES = (os.getenv("LOG_JOB_TITLES") or "true").strip().lower() not in ("0", "false", "no", "off")

    @classmethod
    def validate(cls):
        """Проверить наличие обязательных конфигураций"""
        required = ["GITHUB_TOKEN", "ANTHROPIC_API_KEY"]
        missing = [key for key in required if not getattr(cls, key)]

        if missing:
            raise ValueError(f"Отсутствуют обязательные переменные окружения: {', '.join(missing)}")

        return True


config = Config()
