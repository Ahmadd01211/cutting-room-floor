FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY crf/ ./crf/
COPY sql/ ./sql/
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir -e .

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "scripts.demo", "--seed", "--report"]
