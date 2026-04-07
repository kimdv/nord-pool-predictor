FROM python:3.14-slim AS builder

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY src/ ./src/
COPY areas.yaml ./
COPY migrations/ ./migrations/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "."

FROM python:3.14-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=builder /app .

RUN mkdir -p /data/models && chown -R appuser:appuser /data/models

USER appuser

EXPOSE 8000

CMD ["uvicorn", "nordpool_predictor.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
