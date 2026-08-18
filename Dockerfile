FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY . .

# The application uses only Python's standard library; no pip install is needed.
CMD ["python", "run.py"]
