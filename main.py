"""
Вход на приложението.

РЕАЛНО-ВРЕМЕВА версия: два цикъла вместо един.
  - run_fast_check() на всеки config.FAST_INTERVAL_MINUTES (по подразбиране
    2 мин) - прескорира САМО вече наблюдаваните 5 тикера, за да хванем
    пробив възможно най-бързо, без да чакаме следващото пълно сканиране.
  - run_full_scan() на всеки config.SCAN_INTERVAL_MINUTES (по подразбиране
    10 мин) - сканира целия universe за нови кандидати и обновява watchlist-а.

Render free tier няма безплатен "background worker" - само Web Service
(който трябва да отговаря на HTTP и заспива след 15 мин без трафик). Затова
тук вдигаме мъничък Flask health-check сървър на config.PORT + пускаме
циклите във фонова нишка. За да не заспива, ползвай безплатен external
pinger (cron-job.org / UptimeRobot) към "/" на всеки 10 мин - виж README.md.

Локално: python main.py
На Render: Start Command = python main.py (виж README.md за детайли).
"""
import logging
import threading
import time
from datetime import datetime, timezone

import schedule
from flask import Flask

import config
from data_sources import AlpacaClient, FinnhubClient, FMPClient, get_sec_dilution_flags
from indicators import compute_all
from scoring import score_symbol
from universe import get_universe
from watchlist import load_watchlist, save_watchlist, update_watchlist
from notifier import send_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")

alpaca = AlpacaClient()
finnhub = FinnhubClient()
fmp = FMPClient()

app = Flask(__name__)
_status = {"last_full_scan_at": None, "last_fast_check_at": None, "last_watchlist": {}}


@app.route("/")
def health():
    """Health-check endpoint - и за Render, и за external keep-alive pinger-а."""
    return {"status": "ok", **_status}


@app.route("/test-email")
def test_email():
    """Изпраща тестов email алърт през Resend, за да провериш дали
    ALERT_EMAIL_ENABLED/RESEND_API_KEY/ALERT_EMAIL_TO са настроени правилно.
    Просто отвори този URL в браузъра веднъж."""
    from scoring import ScoreResult
    fake = ScoreResult(
        symbol="TEST",
        score=99,
        reasons=["Това е тестов алърт за проверка на Resend интеграцията."],
        has_catalyst=True,
        raw={"price": 1.23},
    )
    send_alert("ТЕСТ", fake)
    if not config.ALERT_EMAIL_ENABLED:
        return {"sent": False, "reason": "ALERT_EMAIL_ENABLED е false - провери Render Environment Variables."}
    if not config.RESEND_API_KEY:
        return {"sent": False, "reason": "RESEND_API_KEY липсва - провери Render Environment Variables."}
    return {"sent": True, "to": config.ALERT_EMAIL_TO, "note": "Провери логовете (Logs таб) и пощата си."}


def _has_news_catalyst(symbol: str) -> bool:
    news = alpaca.get_news(symbol, limit=5) or finnhub.company_news(symbol, days_back=2) or fmp.stock_news(symbol, limit=5)
    return bool(news)


def _is_market_hours() -> bool:
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    # NASDAQ/NYSE: 13:30-20:00 UTC (9:30-16:00 ET, без DST нюанси - достатъчно за MVP)
    minutes = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= minutes <= 20 * 60


def _score_symbols(symbols) -> list:
    scores = []
    for symbol in symbols:
        try:
            bars = alpaca.get_bars(symbol, timeframe="5Min", limit=100)
            ind = compute_all(bars)
            if not ind or ind["price"] > config.MAX_UNIVERSE_PRICE:
                continue
            catalyst = _has_news_catalyst(symbol)
            dilution = get_sec_dilution_flags(symbol)
            scores.append(score_symbol(symbol, ind, catalyst, dilution))
        except Exception as e:
            log.warning("Грешка при %s: %s", symbol, e)
    return scores


def _maybe_alert_high_potential(symbol: str, meta: dict, result):
    """Праща алърт само при НОВО пресичане на прага - не спамва на всеки
    бърз цикъл докато тикерът си стои над прага."""
    if result and result.is_high_potential:
        if not meta.get("alerted_high_potential"):
            send_alert(f"ВИСОК ПОТЕНЦИАЛ (~{round(config.TARGET_PROFIT_PCT*100)}%)", result)
            meta["alerted_high_potential"] = True
    else:
        meta["alerted_high_potential"] = False


def run_fast_check():
    """Бърз цикъл - само текущия watchlist, за реално-времеви алърти."""
    if not _is_market_hours():
        return
    current_watchlist = load_watchlist()
    if not current_watchlist:
        return

    scores = _score_symbols(current_watchlist.keys())
    scores_by_symbol = {s.symbol: s for s in scores}

    for symbol, meta in current_watchlist.items():
        result = scores_by_symbol.get(symbol)
        if result:
            meta["last_score"] = result.score
        _maybe_alert_high_potential(symbol, meta, result)

    save_watchlist(current_watchlist)
    _status["last_fast_check_at"] = datetime.now(timezone.utc).isoformat()
    _status["last_watchlist"] = current_watchlist
    log.info("Бърз цикъл: %s", {s: m.get("last_score") for s, m in current_watchlist.items()})


def run_full_scan():
    """Пълно сканиране - целия universe, за нови кандидати + watchlist ротация."""
    if not _is_market_hours():
        log.info("Извън пазарни часове - пропускам пълното сканиране.")
        return

    log.info("Стартирам пълно сканиране...")
    current_watchlist = load_watchlist()
    universe = set(get_universe()) | set(current_watchlist.keys())

    all_scores = _score_symbols(universe)
    new_watchlist, added, dropped = update_watchlist(current_watchlist, all_scores)

    scores_by_symbol = {s.symbol: s for s in all_scores}
    for symbol in added:
        send_alert("НОВ в watchlist", scores_by_symbol[symbol])
    for symbol in dropped:
        log.info("%s излиза от watchlist (score падна под прага).", symbol)

    for symbol, meta in new_watchlist.items():
        _maybe_alert_high_potential(symbol, meta, scores_by_symbol.get(symbol))

    save_watchlist(new_watchlist)
    _status["last_full_scan_at"] = datetime.now(timezone.utc).isoformat()
    _status["last_watchlist"] = new_watchlist

    log.info(
        "Пълно сканиране завършено. Watchlist (%d/%d): %s",
        len(new_watchlist), config.WATCHLIST_SIZE, list(new_watchlist.keys()),
    )


def _scan_loop():
    log.info(
        "Penny Stock Scanner фонов loop стартира. Universe price cap: $%.2f, watchlist size: %d, "
        "fast check на всеки %d мин, пълно сканиране на всеки %d мин.",
        config.MAX_UNIVERSE_PRICE, config.WATCHLIST_SIZE, config.FAST_INTERVAL_MINUTES, config.SCAN_INTERVAL_MINUTES,
    )
    run_full_scan()  # веднага при старт, после по разписание
    schedule.every(config.SCAN_INTERVAL_MINUTES).minutes.do(run_full_scan)
    schedule.every(config.FAST_INTERVAL_MINUTES).minutes.do(run_fast_check)
    while True:
        schedule.run_pending()
        time.sleep(15)


def main():
    thread = threading.Thread(target=_scan_loop, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
