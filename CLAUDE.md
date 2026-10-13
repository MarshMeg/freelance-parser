# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Пайплайн: парсеры фриланс-бирж → Claude-анализ → GitHub Issues в колонке Backlog GitHub Project (номер — `GITHUB_PROJECT_ID`). Документация для людей — в `README.md` (на русском; общаться с владельцем тоже по-русски).

## Команды

```bash
uv sync                                   # зависимости
uv run pytest                             # все тесты (без сети и без LLM)
uv run pytest tests/test_pipeline.py::test_select_new_filters_sorts_limits   # один тест
uv run python main.py --dry-run           # живой парсинг + отбор новых, без LLM и GitHub
uv run python main.py                     # полный цикл (нужен .env)
```

**LLM-вызовы запускает только владелец** (лимиты подписки): не запускай `main.py` без `--dry-run` и `scripts/try_analyzer.py`, не добавляй тесты, которые ходят в Anthropic API или OpenRouter. Тесты должны оставаться офлайн (моки/фейки).

## Архитектура

Плоские модули в корне (не пакет): `main.py`, `config.py`, `models.py`, `llm_analyzer.py`, `github_integration.py`, `utils.py`, `parsers/`. Запуск из корня; pytest настроен через `pythonpath = ["."]`; в `pyproject.toml` `package = false` — uv ставит только зависимости.

Поток в `main.run()`:
1. `collect_jobs` — активные парсеры (`ENABLED_PARSERS`, имена — `BaseParser.source`; по умолчанию `kwork,freelance_ru`, Freelancer выключен; реестр — `main.PARSERS`) параллельно; упавший парсер логируется и не ломает остальные; дедуп по `Job.id` (`<source>_<id>`).
2. `select_new` — отсев по `SeenStore` (файловый кэш `.cache/seen_jobs.json`, TTL `CACHE_DAYS`, по умолчанию 30 — дольше жизни заказа в ленте) и по Issues, уже существующим в репозитории; свежие первыми; потолок `MAX_JOBS_PER_RUN` (по умолчанию 0 — обрабатываются все новые). Это главная защита от лишних LLM-вызовов — нерекомендованные заказы Issues не создают, поэтому только кэш не даёт анализировать их повторно.
   2a. `Prefilter.split` (`prefilter.py`, только если задан `OPENROUTER_API_KEY`, не в dry-run) — по запросу на заказ (до 8 параллельно) в OpenRouter Decisions API (`/api/alpha/decisions`): Jev (`PREFILTER_MODEL`) — decision-модель, не чат; через `chat/completions` отвечает 400. На `noul`-вопрос «заказ явно вне стека?» приходит вероятность; при значении ≥ `PREFILTER_THRESHOLD` (0.8) заказ отсеивается, логируется и попадает в кэш. Fail-open: ошибка запроса или непонятный ответ — заказ идёт в Claude.
   Шаги 3–5 идут партиями по `BATCH_SIZE`; после каждой кэш сохраняется, так что прерванный запуск не теряет сделанный анализ. Заказы, упавшие в анализе, в кэш не попадают и повторяются в следующий запуск.
3. `enrich_jobs` — хук `BaseParser.enrich(job)` догружает полное описание только для заказов, идущих в LLM (сейчас реализован для Freelance.ru, в ленте текст обрезан).
4. `LLMAnalyzer.analyze_many` — structured outputs (`messages.parse`, схема `AnalysisResult` — pydantic; принудительный `tool_choice` Sonnet 5.5/Opus 5.5 отклоняют с 400), модель — `ANALYZER_MODEL` (по умолчанию `claude-haiku-4-5`); упавшие заказы пропускаются. Для рекомендованных заказов не на русском модель заполняет `title_ru`/`description_ru` — в Issue идёт перевод, оригинал прячется под `<details>`; отклик остаётся на языке заказа. Ещё модель оценивает `sonnet_confidence`/`opus_confidence` (0–1, выполнит ли заказ ИИ-агент на Sonnet 5.5 / Opus 5.5) и пишет `llm_notes` — в Issue это раздел «Решаемость через LLM».
5. `GitHubIntegration.publish` — Issue через PyGithub, затем GraphQL: добавить в Project v2 и выставить Status=Backlog. Дедуп — по скрытому маркеру `<!-- job-id: ... -->` в теле Issue (читаются все Issues с меткой `freelance`, включая закрытые). Создаются только Issues для `is_recommended`.

Парсеры наследуют `BaseParser` (атрибут класса `source`, async httpx-клиент, контекстный менеджер, `fetch`/`fetch_page`, `_create_job`). Клиент создаётся в `__aenter__` (и пересоздаётся, если закрыт) — парсер используется дважды (`parse`, затем `enrich`). Источники данных у всех разные: Freelance.ru — HTML (BeautifulSoup), Kwork — JSON, вшитый в HTML (`"wantsListData":`), Freelancer — публичный JSON API.

## Нюансы

- `Job.posted_at` всегда с часовым поясом: Kwork и Freelance.ru отдают московское время без пояса (`utils.MSK`), Freelancer — unix time (UTC). Без этого `select_new` сортировал источники со сдвигом на 3 часа. В Issues даты выводятся в `config.TIMEZONE` (env `TIMEZONE`, по умолчанию `UTC+4`).
- Freelance.ru читается с фильтром категорий `c[]` (`FREELANCE_RU_CATEGORIES`, по умолчанию `4,724`): общая лента `/task` почти целиком не про разработку, и IT-заказы в первые 25 записей не попадали.
- `httpx` по умолчанию не следует редиректам — в `BaseParser` включён `follow_redirects=True` (иначе Freelance.ru падал на 302).
- Токен из `claude setup-token` — OAuth (`sk-ant-oat…`); `llm_analyzer._build_client` передаёт его как `auth_token` с бета-заголовком. Это не проверялось реальным вызовом.
- Upwork (Cloudflare-челлендж) и Fiverr (нет ленты заказов) сознательно не подключены; Хабр Фриланс закрыт.
- Для Projects v2 токену нужен scope `project`; проект и поле Status/опция Backlog ищутся по именам (`GITHUB_PROJECT_STATUS_FIELD`, `GITHUB_PROJECT_STATUS_OPTION` в `config.py`).
- Прод: `ci.yml` (тесты → сборка и пуш образа в GHCR только из main) и `parser-cron.yml` (почасовой `docker run` образа `:latest`). Секрет называется `GH_PAT` (секреты не могут начинаться с `GITHUB_`) и передаётся в контейнер как `GITHUB_TOKEN`; кэш `.cache/seen_jobs.json` монтируется в контейнер; восстанавливается `actions/cache/restore` и сохраняется `actions/cache/save` с `if: always()` — даже если запуск упал или упёрся в тайм-аут. Новые корневые модули нужно добавлять в `COPY` в `Dockerfile` — копируется явный список файлов.
- Конфиг — только переменные окружения/`.env` через `config.py`; шаблон в `.env.example`. Личное (стек `DEVELOPER_STACK`, репозиторий, проект, метка, категории бирж) в код не вшивается; пустая переменная означает значение по умолчанию.
- В `parser-cron.yml` Issues создаются в `vars.ISSUES_REPO` (по умолчанию — этот же репозиторий), а названия заказов в логи не пишутся (`LOG_JOB_TITLES=false`): репозиторий публичный, логи и Issues видны всем.
