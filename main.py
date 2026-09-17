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
from data_sources import get_dexscreener_pairs_batch, get_dexscreener_pairs, get_rugcheck_report, extract_mint_address
from scoring import score_token
from seen_store import load_seen, mark_seen
from notifier import send_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")

# --- Стартова самопроверка ---
# Реален случай (17.09): локалният config.py беше стара версия - липсваше
# config.BLOCK_ON_LOW_LIQUIDITY_RISK - и score_token() гърмеше с
# AttributeError за АБСОЛЮТНО ВСЯКА монета. Ботът изглеждаше "жив"
# (health-check-ът минаваше, PumpPortal слушаше), но реално не пращаше
# НИКАКЪВ алърт часове наред, защото всяка monitor_token() задача умираше
# тихо на първия score_token() опит (само WARNING в логовете, по един ред
# на монета - лесно за пропускане). _startup_self_check() хваща точно
# този клас бъг (config.py разминат/непълен спрямо scoring.py) веднага при
# стартиране, с ясна CRITICAL грешка и спиране на процеса, вместо часове/
# дни по-късно да разбираме случайно от липсващи алърти.
REQUIRED_CONFIG_ATTRS = [
    "PUMPPORTAL_WS_URL", "INITIAL_INDEX_DELAY_SECONDS", "POLL_INTERVAL_SECONDS",
    "MONITOR_WINDOW_MINUTES", "MIN_POLLS_BEFORE_ALERT", "PEAK_DRAWDOWN_STOP_PCT",
    "MIN_LP_LOCKED_PCT", "BLOCK_ON_LOW_LIQUIDITY_RISK", "MIN_LIQUIDITY_USD",
    "MAX_INSIDER_CLUSTERS", "RUGCHECK_REFRESH_EVERY_N_POLLS", "MAX_MARKET_CAP_USD",
    "HIGH_POTENTIAL_THRESHOLD", "FINAL_CHECK_MAX_DRAWDOWN_PCT", "IMPERSONATION_KEYWORDS",
    "IMPERSONATION_LEGITIMACY_WORDS", "ALERT_EMAIL_ENABLED", "RESEND_API_KEY",
    "RESEND_FROM_EMAIL", "ALERT_EMAIL_TO", "MIN_EMAIL_INTERVAL_SECONDS",
    "MAX_EMAILS_PER_DAY", "ALERT_QUIET_HOURS_TZ", "PORT",
]


def _startup_self_check():
    missing = [name for name in REQUIRED_CONFIG_ATTRS if not hasattr(config, name)]
    if missing:
        log.critical(
            "СТАРТОВА ПРОВЕРКА ПРОВАЛЕНА: config.py липсват настройки: %s. "
            "Най-вероятно файлът (локално или на Render) е стара/непълна версия - "
            "провери git push/pull и redeploy-ни. Спирам стартирането, вместо да "
            "оставя всяка монета да крашва тихо във фона.",
            ", ".join(missing),
        )
        raise SystemExit(1)

    fake_pair = {
        "liquidity": {"usd": 20000},
        "marketCap": 50000,
        "fdv": 50000,
        "baseToken": {"name": "SelfTestCoin", "symbol": "SELFTEST"},
        "volume": {"h1": 10000},
    }
    fake_rugcheck = {"risks": [], "markets": [], "graphInsidersDetected": 0}
    try:
        score_token("SelfTest11111111111111111111111111111111111", fake_pair, fake_rugcheck, momentum_pct=30.0)
    except Exception as e:
        log.critical(
            "СТАРТОВА ПРОВЕРКА ПРОВАЛЕНА: score_token() гърми на синтетичен тест "
            "(%s) - има бъг/несъответствие между config.py и scoring.py. Спирам "
            "стартирането, вместо да оставя всяка монета да крашва тихо.",
            e,
        )
        raise SystemExit(1)

    log.info("Стартова самопроверка: config.py и scoring.py изглеждат съвместими.")


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

# Споделен кеш с последните DexScreener данни за всяка следена монета -
# пълни се от ЕДИН централен loop (_refresh_market_data_loop), който прави
# batch заявки (до 30 адреса наведнъж) вместо всяка следена монета да си
# праща собствена HTTP заявка на всеки poll. Причина (17.09, живи Render
# логове): при 20+ едновременно следени монети, толкова отделни заявки на
# всеки ~60с редовно удряха DexScreener rate limit-а (429 Too Many Requests),
# което губеше/забавяше ценови данни точно когато монетата реално мърда.
_market_data_cache: dict = {}


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
        market_cap_usd=45000,
        potential_label="🚀 Потенциален голям runner (нисък market cap + силен ранен моментум + висок обем) - но силно спекулативно, повечето такива монети пак отиват на 0",
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


async def _refresh_market_data_loop():
    """Централен loop - batch-ва DexScreener заявки за ВСИЧКИ следени в
    момента монети наведнъж (виж data_sources.get_dexscreener_pairs_batch),
    вместо всяка от monitor_token() task-овете да пита поотделно. Тече
    независимо от индивидуалните monitor_token() задачи, докато процесът е
    жив - виж коментара при _market_data_cache по-горе за причината."""
    while True:
        try:
            mints = list(_monitoring)
            if mints:
                fresh = await asyncio.to_thread(get_dexscreener_pairs_batch, mints)
                _market_data_cache.update(fresh)
                # чистим кеша от монети, които вече не следим (излезли от
                # monitor_token поради timeout/алърт/грешка) - да не расте
                # неограничено през дни наред работа на процеса.
                for stale_mint in list(_market_data_cache.keys()):
                    if stale_mint not in _monitoring:
                        _market_data_cache.pop(stale_mint, None)
        except Exception as e:
            log.warning("Грешка в централния market-data refresh loop: %s", e)
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)


async def monitor_token(mint: str):
    """Следи монетата на живо и праща алърт веднага щом пресече прага -
    вместо да чака фиксирано изчакване и да провери само веднъж."""
    _monitoring.add(mint)
    try:
        await asyncio.sleep(config.INITIAL_INDEX_DELAY_SECONDS)

        # RugCheck: опитваме пак на всеки poll, ДОКАТО не получим реален
        # доклад - веднага след graduation монетата често още не е
        # индексирана (празен report), а преди кодът приемаше "няма флагове"
        # (защото няма доклад изобщо) като "монетата е чиста" и я score-ваше
        # високо въпреки нулева реална риск-проверка.
        #
        # ВАЖНО (17.09, реален rug pull малко след алърт - TWOSIDES/68KXLo...):
        # преди спирахме да питаме RugCheck ОТНОВО веднага щом получим първи
        # непразен доклад - и после го ползвахме до 45 мин напред, без да го
        # опресняваме. Проблемът: RugCheck-ските рискови флагове (Low
        # Liquidity, LP lock %, insider клъстъри) се менят в реално време
        # заедно с монетата - ако първият доклад е хванат рано (преди
        # ликвидността да е пропаднала или insider клъстъри да са открити),
        # монетата може да мине филтрите на poll #1-2 с "чист" стар доклад,
        # докато реалната картина вече се е влошила. Затова сега опресняваме
        # RugCheck периодично (на всеки config.RUGCHECK_REFRESH_EVERY_N_POLLS
        # проверки), не само докато е бил празен - живата DexScreener
        # ликвидност/цена вече се опресняват на всеки poll, но RugCheck
        # флаговете не бяха. Пазим стария доклад, ако новата заявка се провали
        # временно (НЕ го трием заради мрежова грешка).
        rugcheck_report = await asyncio.to_thread(get_rugcheck_report, mint)

        first_price = None
        peak_price = None
        consecutive_high_potential = 0
        deadline = datetime.now(timezone.utc) + timedelta(minutes=config.MONITOR_WINDOW_MINUTES)
        poll_num = 0

        while datetime.now(timezone.utc) < deadline:
            poll_num += 1
            should_refresh_rugcheck = (not rugcheck_report) or (poll_num % config.RUGCHECK_REFRESH_EVERY_N_POLLS == 0)
            if should_refresh_rugcheck:
                fresh_rugcheck = await asyncio.to_thread(get_rugcheck_report, mint)
                if fresh_rugcheck:
                    rugcheck_report = fresh_rugcheck
            # Четем от споделения кеш (пълни се от _refresh_market_data_loop),
            # НЕ директна HTTP заявка тук - виж коментара при _market_data_cache.
            pairs = _market_data_cache.get(mint) or []
            best_pair = pairs[0] if pairs else {}
            price = _safe_float(best_pair.get("priceUsd"))

            if first_price is None and price:
                first_price = price
            if price:
                peak_price = max(peak_price, price) if peak_price else price
            momentum_pct = ((price - first_price) / first_price * 100) if (first_price and price) else 0.0
            drawdown_pct = ((peak_price - price) / peak_price * 100) if (peak_price and price) else 0.0

            result = score_token(mint, best_pair, rugcheck_report, momentum_pct)
            log.info(
                "[%s] poll #%d score=%.1f моментум=%.1f%% спад_от_пика=%.1f%% ликвидност=$%.0f (%s)",
                mint, poll_num, result.score, momentum_pct, drawdown_pct, result.liquidity_usd,
                "; ".join(result.reasons),
            )

            # Защита срещу "купуване на върха" / еднократен spike - виж
            # config.MIN_POLLS_BEFORE_ALERT. РЕАЛЕН БЪГ (17.09, TWOSIDES/
            # 68KXLo... rug pull малко след алърт): преди тук проверявахме
            # "poll_num >= MIN_POLLS_BEFORE_ALERT" - т.е. САМО колко общо
            # проверки сме направили откакто следим монетата, НЕ колко от
            # тях подред са били над прага. Монета можеше да е боклук на
            # poll #1-2 и да получи ЕДИНСТВЕН случаен (wash-trading) spike
            # точно на poll #3 - и понеже 3 >= MIN_POLLS_BEFORE_ALERT(3), се
            # третираше като "потвърдено" и пращахме алърт веднага, без
            # реално нито едно предишно потвърждение. Сега броим ПОСЛЕДОВАТЕЛНИ
            # high-potential резултати (нулира се веднага щом score падне под
            # прага) - същия принцип, който вече ползваме в PennyStockScanner.
            if result.is_high_potential:
                consecutive_high_potential += 1
            else:
                consecutive_high_potential = 0

            if result.is_high_potential:
                already_rolling_over = drawdown_pct >= config.PEAK_DRAWDOWN_STOP_PCT
                confirmed = consecutive_high_potential >= config.MIN_POLLS_BEFORE_ALERT
                if confirmed and not already_rolling_over:
                    # Финална live проверка "в последната секунда" - виж
                    # config.FINAL_CHECK_MAX_DRAWDOWN_PCT. Директна свежа
                    # DexScreener заявка (НЕ кеша, който е до
                    # POLL_INTERVAL_SECONDS стар) точно преди да пратим -
                    # хваща случая, в който монетата пада МЕЖДУ последното
                    # потвърждение и реалния момент на изпращане.
                    final_pairs = await asyncio.to_thread(get_dexscreener_pairs, mint)
                    final_best_pair = final_pairs[0] if final_pairs else best_pair
                    final_price = _safe_float(final_best_pair.get("priceUsd")) or price
                    final_drawdown_pct = (
                        ((peak_price - final_price) / peak_price * 100)
                        if (peak_price and final_price) else drawdown_pct
                    )
                    if final_drawdown_pct >= config.FINAL_CHECK_MAX_DRAWDOWN_PCT:
                        log.info(
                            "%s: финалната проверка точно преди изпращане показа спад %.1f%% от пика "
                            "(над прага %.1f%%) - отменям алърта в последния момент, монетата вече пада.",
                            mint, final_drawdown_pct, config.FINAL_CHECK_MAX_DRAWDOWN_PCT,
                        )
                    else:
                        send_alert(result)
                        _status["last_alert_at"] = datetime.now(timezone.utc).isoformat()
                        break
                elif already_rolling_over:
                    log.info(
                        "%s: score е висок (%.1f), НО цената вече е паднала %.1f%% от пика - "
                        "най-вероятно върхът е изпуснат, пропускам алърта.",
                        mint, result.score, drawdown_pct,
                    )
                else:
                    log.info(
                        "%s: score е висок (%.1f) - %d/%d последователни проверки над прага, "
                        "чакам още преди да пратя алърт.",
                        mint, result.score, consecutive_high_potential, config.MIN_POLLS_BEFORE_ALERT,
                    )

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
    loop.create_task(_refresh_market_data_loop())
    loop.run_until_complete(listen_for_migrations(_dispatch))


def main():
    _startup_self_check()
    thread = threading.Thread(target=_run_async_loop, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
