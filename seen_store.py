"""Проста персистенция на вече-оценени mint адреси, за да не пращаме дублиран
алърт за същата монета при рестарт на процеса."""
import json
import os

import config


def _ensure_data_dir():
    os.makedirs(config.DATA_DIR, exist_ok=True)


def load_seen() -> set:
    _ensure_data_dir()
    if not os.path.exists(config.SEEN_FILE):
        return set()
    try:
        with open(config.SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def mark_seen(mint: str, seen: set):
    seen.add(mint)
    _ensure_data_dir()
    # пазим само последните 5000, за да не расте файлът безкрайно
    trimmed = list(seen)[-5000:]
    with open(config.SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)
