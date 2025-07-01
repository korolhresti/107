#!/bin/bash

# Застосовуємо схему до бази даних
echo "Applying database schema..."
psql $DATABASE_URL -f schema.sql -X

# Запускаємо веб-сервер Uvicorn для bot.py
echo "Starting Uvicorn web server for bot.py..."

uvicorn bot:app --host 0.0.0.0 --port 10000

