"""
Wrapper-и над публичните безплатни API-та за on-chain/риск данни.

ВАЖНО за поддръжка: точните имена на JSON полетата на RugCheck и
формата на PumpPortal migration съобщенията не са напълно документирани
публично и могат да се различават леко от това, което е тук - кодът е
писан defensively (winner .get() навсякъде, никога няма да гръмне при
липсващо поле), но провери логовете след първия деплой (`log.info` реда
с "RAW migration payload" / "RAW rugcheck report") и коригирай ключовете
тук, ако DexScreener/RugCheck са сменили схемата си междувременно.
"""
import logging
import requests

import config

log = logging.getLogger("data_sources")


def get_dexscreener_pairs(mint_address: str) -> list[dict]:
    """Връща списък от DEX двойки за токена (Raydium/PumpSwap/...), най-ликвидната първа."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
    except Exception as e:
        log.warning("DexScreener fail за %s: %s", mint_address, e)
        return []
    pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd", 0), reverse=True)
    return pairs


def get_rugcheck_report(mint_address: str) -> dict:
    """RugCheck риск доклад. Публичен endpoint, без ключ за базов report (виж бележката горе)."""
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report"
    try:
        r = requests.get(url, timeout=10, headers={"Accept": "application/json"})
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        report = r.json()
        log.debug("RAW rugcheck report за %s: %s", mint_address, report)
        return report
    except Exception as e:
        log.warning("RugCheck fail за %s: %s", mint_address, e)
        return {}


def extract_mint_address(migration_event: dict) -> str | None:
    """
    PumpPortal не публикува верижна схема за migration съобщението - пробваме
    най-вероятните имена на ключа с адреса на токена. Ако нищо не съвпадне,
    логваме целия payload за ръчна проверка (виж README.md).
    """
    for key in ("mint", "ca", "token", "mint_address", "tokenAddress", "address"):
        val = migration_event.get(key)
        if isinstance(val, str) and len(val) >= 32:
            return val
    log.warning("Не разпознах mint адрес в migration event, пълен payload: %s", migration_event)
    return None
