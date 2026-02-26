FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY ./src /app/src
COPY ./tests /app/tests

ENV PYTHONPATH=/app

CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
