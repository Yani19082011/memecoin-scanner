"""Проста персистенция на вече-оценени mint адреси, за да не пращаме дублиран
алърт за същата монета при рестарт на процеса.

ЗАБЕЛЕЖКА (18.09, намерено при цялостен преглед на кода): по-рано тук се
ползваше обикновен Python `set`. Set-овете НЕ пазят ред на вкарване -
"последните 5000" при `list(seen)[-5000:]` реално вземаше произволни 5000
елемента (по hash ред), не действително последно видените mint адреси. При
рестарт близо до 5000-ния праг това можеше тихо да "забрави" наскоро видяна
монета (риск от дублиран алърт) и да задържи произволна стара - обратното на
целта на trim-а. Сега ползваме `dict` (Python 3.7+ пази ред на вкарване) като
ordered set - `in`/`len()` работят еднакво като на `set`, но trim-ът вече
реално маха НАЙ-СТАРИТЕ по ред на добавяне."""
import json
import os

import config


def _ensure_data_dir():
    os.makedirs(config.DATA_DIR, exist_ok=True)


def load_seen() -> dict:
    _ensure_data_dir()
    if not os.path.exists(config.SEEN_FILE):
        return {}
    try:
        with open(config.SEEN_FILE, "r", encoding="utf-8") as f:
            return dict.fromkeys(json.load(f))
    except (json.JSONDecodeError, OSError):
        return {}


def mark_seen(mint: str, seen: dict):
    # Ако вече е имало запис, махаме го първо - re-insert-ва го накрая
    # (най-новите по ред), вместо да остане на старата си позиция.
    seen.pop(mint, None)
    seen[mint] = True
    _ensure_data_dir()
    # пазим само последните 5000 по РЕАЛЕН ред на добавяне, за да не расте
    # файлът безкрайно (виж бележката горе защо `dict`, не `set`).
    if len(seen) > 5000:
        for old_key in list(seen.keys())[:-5000]:
            del seen[old_key]
    with open(config.SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen.keys()), f)
