"""
Вход на приложението.

РЕАЛНО-ВРЕМЕВА версия: вместо да чака фиксирани N минути и да оцени
монетата само веднъж (което пропуска ранните pump-ове), сега при всяко
graduation стартираме monitoring task, който проверява монетата на всеки
config.POLL_INTERVAL_SECONDS секунди в рамките на config.MONITOR_WINDOW_MINUTES
минути и праща алърт веднага щом реално наблюдаваният моментум + ликвидност/
обем пресекат прага - не на фиксирана минута.

Render-съвместимо: мъничък Flask health-check сървър + фонов asyncio loop.

Локално: python main.py
На Render: Start Command = python main.py
"""
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask

import config
from pumpportal_client import listen_for_migrations
from data_sources import get_dexscreener_pairs, get_rugcheck_report, extract_mint_address
from scoring import score_token
from seen_store import load_seen, mark_seen
from notifier import send_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")

app = Flask(__name__)
_status = {
    "started_at": None,
    "last_migration_at": None,
    "last_alert_at": None,
    "seen_count": 0,
    "currently_monitoring": [],
}
_seen = load_seen()
_monitoring = set()


@app.route("/")
def health():
    _status["currently_monitoring"] = list(_monitoring)
    return {"status": "ok", **_status}


@app.route("/test-email")
def test_email():
    """Изпраща тестов email алърт през Resend, за да провериш дали
    ALERT_EMAIL_ENABLED/RESEND_API_KEY/ALERT_EMAIL_TO са настроени правилно.
    Просто отвори този URL в браузъра веднъж."""
    from scoring import MemeScoreResult
    fake = MemeScoreResult(
        mint="TestMint1111111111111111111111111111111111",
        score=99,
        reasons=["Това е тестов алърт за проверка на Resend интеграцията."],
        liquidity_usd=12345,
        raw={},
    )
    send_alert(fake)
    if not config.ALERT_EMAIL_ENABLED:
        return {"sent": False, "reason": "ALERT_EMAIL_ENABLED е false - провери Render Environment Variables."}
    if not config.RESEND_API_KEY:
        return {"sent": False, "reason": "RESEND_API_KEY липсва - провери Render Environment Variables."}
    return {"sent": True, "to": config.ALERT_EMAIL_TO, "note": "Провери логовете (Logs таб) и пощата си."}


def _safe_float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


async def monitor_token(mint: str):
    """Следи монетата на живо и праща алърт веднага щом пресече прага -
    вместо да чака фиксирано изчакване и да провери само веднъж."""
    _monitoring.add(mint)
    try:
        await asyncio.sleep(config.INITIAL_INDEX_DELAY_SECONDS)

        # RugCheck се дърпа веднъж в началото - mint/freeze/risk флаговете не
        # се менят на всяка минута, няма смисъл да го питаме на всеки poll.
        rugcheck_report = get_rugcheck_report(mint)

        first_price = None
        deadline = datetime.now(timezone.utc) + timedelta(minutes=config.MONITOR_WINDOW_MINUTES)
        poll_num = 0

        while datetime.now(timezone.utc) < deadline:
            poll_num += 1
            pairs = get_dexscreener_pairs(mint)
            best_pair = pairs[0] if pairs else {}
            price = _safe_float(best_pair.get("priceUsd"))

            if first_price is None and price:
                first_price = price
            momentum_pct = ((price - first_price) / first_price * 100) if (first_price and price) else 0.0

            result = score_token(mint, best_pair, rugcheck_report, momentum_pct)
            log.info(
                "[%s] poll #%d score=%.1f моментум=%.1f%% ликвидност=$%.0f (%s)",
                mint, poll_num, result.score, momentum_pct, result.liquidity_usd,
                "; ".join(result.reasons),
            )

            if result.is_high_potential:
                send_alert(result)
                _status["last_alert_at"] = datetime.now(timezone.utc).isoformat()
                break

            await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
        else:
            log.info("%s: monitoring прозорецът (%d мин) изтече без сигнал.", mint, config.MONITOR_WINDOW_MINUTES)

    except Exception as e:
        log.warning("Грешка при следене на %s: %s", mint, e)
    finally:
        _monitoring.discard(mint)
        mark_seen(mint, _seen)
        _status["seen_count"] = len(_seen)


async def on_migration(event: dict):
    mint = extract_mint_address(event)
    if not mint or mint in _seen or mint in _monitoring:
        return
    _status["last_migration_at"] = datetime.now(timezone.utc).isoformat()
    log.info("Ново graduation събитие: %s - започвам реално-времево следене.", mint)
    await monitor_token(mint)


def _run_async_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _dispatch(event):
        # всяко събитие се обработва в собствена task, за да следим много
        # монети едновременно, без да блокираме слушането на нови graduation-и
        loop.create_task(on_migration(event))

    _status["started_at"] = datetime.now(timezone.utc).isoformat()
    loop.run_until_complete(listen_for_migrations(_dispatch))


def main():
    thread = threading.Thread(target=_run_async_loop, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
