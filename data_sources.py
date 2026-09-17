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


def _sort_pairs_by_liquidity(pairs: list[dict]) -> list[dict]:
    pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd", 0), reverse=True)
    return pairs


def get_dexscreener_pairs(mint_address: str) -> list[dict]:
    """Връща списък от DEX двойки за токена (Raydium/PumpSwap/...), най-ликвидната първа.
    Виж get_dexscreener_pairs_batch() за batch версията, ползвана от main.py при
    следене на много монети едновременно (за да не удряме DexScreener rate limit-а)."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
    except Exception as e:
        log.warning("DexScreener fail за %s: %s", mint_address, e)
        return []
    return _sort_pairs_by_liquidity(pairs)


# DexScreener позволява няколко token адреса в ЕДНА заявка, разделени със
# запетая (потвърдено чрез живо тестване 17.09 - връща pairs и за двата
# адреса в тестов пример) - точен максимален брой на заявка НЕ е официално
# документиран, 30 е консервативна, разумна стойност (често срещана норма
# при подобни batch API-та).
DEXSCREENER_BATCH_SIZE = 30


def get_dexscreener_pairs_batch(mint_addresses: list[str]) -> dict[str, list[dict]]:
    """Batch версия на get_dexscreener_pairs() - ЕДНА HTTP заявка за до
    DEXSCREENER_BATCH_SIZE адреса наведнъж, вместо по една заявка на монета.

    Защо е нужно: при следене на 20+ монети едновременно, ако всяка сама
    си пита DexScreener на всеки poll (виж main.py::monitor_token), общият
    брой заявки/минута лесно удря DexScreener-ския rate limit -> 429 Too
    Many Requests -> изгубени/забавени ценови данни точно когато монетата
    реално мърда (потвърдено в живите Render логове 17.09). Batch заявките
    намаляват броя HTTP повиквания ~30x за същото покритие.

    Връща {mint: sorted_pairs_list} за всеки заявен адрес (празен списък
    ако адресът не е намерен/грешка в конкретния chunk - не гърми целия
    batch заради един лош chunk)."""
    result: dict[str, list[dict]] = {addr: [] for addr in mint_addresses}
    if not mint_addresses:
        return result

    for i in range(0, len(mint_addresses), DEXSCREENER_BATCH_SIZE):
        chunk = mint_addresses[i:i + DEXSCREENER_BATCH_SIZE]
        url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
        except Exception as e:
            log.warning("DexScreener batch fail за %d адреса: %s", len(chunk), e)
            continue

        chunk_set = set(chunk)
        for pair in pairs:
            base_addr = (pair.get("baseToken") or {}).get("address")
            quote_addr = (pair.get("quoteToken") or {}).get("address")
            matched = base_addr if base_addr in chunk_set else (quote_addr if quote_addr in chunk_set else None)
            if matched:
                result[matched].append(pair)

    for addr, pairs in result.items():
        if pairs:
            result[addr] = _sort_pairs_by_liquidity(pairs)
    return result


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
