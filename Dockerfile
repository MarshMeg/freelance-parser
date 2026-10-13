FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

LABEL org.opencontainers.image.source="https://github.com/MarshMeg/freelance-parser"

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# зависимости отдельным слоем — кэшируется, пока не меняются pyproject/uv.lock
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY config.py models.py llm_analyzer.py prefilter.py github_integration.py main.py utils.py ./
COPY parsers ./parsers

ENTRYPOINT ["python", "main.py"]
