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
import logging
import os

import requests

import config

log = logging.getLogger("seen_store")

# Ключ в Upstash Redis, под който пазим целия "seen" списък като един JSON blob.
_UPSTASH_KEY = "memecoinscanner:seen"


def _upstash_configured() -> bool:
    return bool(config.UPSTASH_REDIS_REST_URL and config.UPSTASH_REDIS_REST_TOKEN)


def _upstash_cmd(*args):
    """Едно REST повикване към Upstash Redis - виж идентичната функция и
    коментар в PennyStockScanner/watchlist.py за пълния контекст (18.09)."""
    resp = requests.post(
        config.UPSTASH_REDIS_REST_URL,
        headers={"Authorization": f"Bearer {config.UPSTASH_REDIS_REST_TOKEN}"},
        json=list(args),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("result")


def _ensure_data_dir():
    os.makedirs(config.DATA_DIR, exist_ok=True)


def load_seen() -> dict:
    """Ако UPSTASH_REDIS_REST_URL/TOKEN са зададени (безплатен Upstash Redis
    акаунт - виж README/.env.example), пазим "seen" списъка ТАМ вместо на
    локалния Render диск, който се изтрива при всеки redeploy (18.09, същия
    проблем като watchlist-а в PennyStockScanner - виж коментара там за
    пълния контекст). Ако не са зададени - старото поведение с локален файл."""
    if _upstash_configured():
        try:
            raw = _upstash_cmd("GET", _UPSTASH_KEY)
            return dict.fromkeys(json.loads(raw)) if raw else {}
        except Exception as e:
            log.error("Upstash GET се провали (%s) - тръгвам с празен 'seen' списък тази сесия.", e)
            return {}
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
    # пазим само последните 5000 по РЕАЛЕН ред на добавяне, за да не расте
    # паметта безкрайно (виж бележката горе защо `dict`, не `set`).
    if len(seen) > 5000:
        for old_key in list(seen.keys())[:-5000]:
            del seen[old_key]
    if _upstash_configured():
        try:
            _upstash_cmd("SET", _UPSTASH_KEY, json.dumps(list(seen.keys())))
            return
        except Exception as e:
            log.error("Upstash SET се провали (%s) - 'seen' промяната за %s НЕ е запазена трайно тази обиколка.", e, mint)
            return
    _ensure_data_dir()
    with open(config.SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen.keys()), f)
