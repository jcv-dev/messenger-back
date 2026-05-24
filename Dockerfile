FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["gunicorn", "config.asgi:application", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "4", "--thread-pool-size", "16", \
     "--bind", "0.0.0.0:8000"]
