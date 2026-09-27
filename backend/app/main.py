"""Совместимость с тестовым импортом `from app.main import app`.

Канонический модуль приложения — backend/main.py
(запуск: `uvicorn backend.main:app`, см. README и корневой Dockerfile).
Этот файл лишь переэкспортирует тот же объект app, чтобы существующие
тесты (tests/test_api.py, строка `from app.main import app`) продолжали
работать без изменений.
"""
from main import app  # noqa: F401  (re-export приложения)

__all__ = ["app"]