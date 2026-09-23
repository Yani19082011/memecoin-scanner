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
import html
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request

import config
from pumpportal_client import listen_for_migrations
from data_sources import get_dexscreener_pairs_batch, get_dexscreener_pairs, get_rugcheck_report, extract_mint_address
from scoring import score_token
from seen_store import load_seen, mark_seen
from notifier import send_alert, can_send_now, send_watch_digest

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
    "MAX_EMAILS_PER_DAY", "ALERT_QUIET_HOURS_TZ", "PORT", "KEEP_ALIVE_PING_MINUTES",
    # --- добавени 18.09 при цялостен преглед на кода - тези липсваха от
    # проверката, въпреки че се четат реално от бота (main.py при модулно
    # ниво/load_seen, scoring.py за wash-trading защитата) ---
    "MAX_VOLUME_TO_LIQUIDITY_RATIO", "DATA_DIR", "SEEN_FILE",
    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
    "ALERT_ACTIVE_START_HOUR", "ALERT_ACTIVE_START_MINUTE",
    "ALERT_ACTIVE_END_HOUR", "ALERT_ACTIVE_END_MINUTE",
    # --- добавено 18.09 вечерта - периодичен "heartbeat" email на всеки 5 мин ---
    "WATCH_DIGEST_ENABLED",
    # --- добавено 23.09 - минимален score, за да си струва изобщо digest email ---
    "WATCH_DIGEST_MIN_SCORE",
    # --- добавени 18.09 вечерта - НОВА СТРАТЕГИЯ, "тренд" сигнали (виж
    # scoring.py/config.py) - EXTENDED_MAX_MARKET_CAP_USD/EXTENDED_ZONE_MIN_
    # LIQUIDITY_USD вече НЕ съществуват (старата "разширена зона" е махната,
    # флат таван MAX_MARKET_CAP_USD=120k вместо това) ---
    "MIN_BUY_RATIO_FOR_FULL_TREND_BONUS", "EARLY_STAGE_MAX_AGE_HOURS",
    "EARLY_STAGE_FADE_AGE_HOURS", "DEATH_SPIRAL_H1_DROP_PCT",
    "DEATH_SPIRAL_MIN_M5_SELLS", "SUSPICIOUS_AVG_TRADE_SIZE_USD",
    "WASH_TRADE_SIZE_PENALTY_POINTS",
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
        # НОВИ полета (18.09 вечерта, "тренд" стратегия) - включени тук, за
        # да може _startup_self_check() реално да упражни новия код път
        # (buy pressure/early stage/death spiral), не само старите полета.
        "txns": {"h1": {"buys": 80, "sells": 40}, "m5": {"buys": 5, "sells": 2}},
        "priceChange": {"h1": 15.0},
        "pairCreatedAt": time.time() * 1000 - (3 * 3_600_000),  # ~3ч отпреди
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


# ВАЖНО (18.09, намерено при цялостен преглед на кода): _startup_self_check()
# трябва да се извика ТУК, ПРЕДИ load_seen() по-долу - иначе load_seen() (чете
# config.DATA_DIR/SEEN_FILE/UPSTASH_REDIS_REST_URL/TOKEN) може да гръмне с
# гол, неясен AttributeError при стар/непълен config.py, преди самата
# самопроверка изобщо да успее да покаже ясната CRITICAL диагностика по-горе.
# main() по-долу вече НЕ вика проверката пак - извикана е веднъж, тук, при
# импортиране на модула.
_startup_self_check()

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
# Заключва достъпа до _monitoring - мутира се от asyncio нишката
# (monitor_token добавя/маха mint-ове), а се ЧЕТЕ както от Flask нишката
# (health() route по-долу), така и от _refresh_market_data_loop - без
# заключване, list(_monitoring) точно докато друга нишка прави add()/
# discard() може да гръмне с "RuntimeError: Set changed size during
# iteration" (намерено при цялостен преглед на кода, 18.09).
_monitoring_lock = threading.Lock()

# Споделен кеш с последните DexScreener данни за всяка следена монета -
# пълни се от ЕДИН централен loop (_refresh_market_data_loop), който прави
# batch заявки (до 30 адреса наведнъж) вместо всяка следена монета да си
# праща собствена HTTP заявка на всеки poll. Причина (17.09, живи Render
# логове): при 20+ едновременно следени монети, толкова отделни заявки на
# всеки ~60с редовно удряха DexScreener rate limit-а (429 Too Many Requests),
# което губеше/забавяше ценови данни точно когато монетата реално мърда.
_market_data_cache: dict = {}

# --- Данни за "Провери сега" бутона на /dashboard (18.09, по избор на
# потребителя - "искам сайт с бутон, като го натисна да ми дава адрес") ---
# ВАЖНО: ботът НЕ работи по фиксиран списък, който да сканира при поискване
# (за разлика от PennyStockScanner) - монетите идват в реално време през
# PumpPortal WebSocket. Затова бутонът не "сканира наново", а показва
# най-добрата информация, която ботът вече има В МОМЕНТА:
#   1. _recent_alerts - монети, които РЕАЛНО минаха всички твърди защити И
#      прага (config.HIGH_POTENTIAL_THRESHOLD) - същите като имейл алъртите.
#   2. Ако няма скорошен алърт - _latest_scores - най-високият текущ score
#      измежду В МОМЕНТА следените монети, дори да е под прага (показва се
#      ясно като "все още не потвърдена", за да не подвежда).
_latest_scores: dict = {}
_latest_scores_lock = threading.Lock()
_recent_alerts: list = []
_recent_alerts_lock = threading.Lock()
MAX_RECENT_ALERTS = 20


@app.route("/")
def health():
    with _monitoring_lock:
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


_MINT_SAFE_RE = re.compile(r"^[A-Za-z0-9]{20,64}$")


def _is_safe_mint(mint: str) -> bool:
    """Защита в дълбочина - mint адресите (base58 Solana pubkeys) никога не
    би трябвало да съдържат кавички/HTML символи, но понеже адресът се
    вгражда директно в onclick="..." JS атрибут в таблицата по-долу,
    проверяваме изрично charset-а тук, преди да го вградим някъде,
    вместо да разчитаме мълчаливо на произхода на данните."""
    return bool(_MINT_SAFE_RE.match(mint or ""))


def _risk_label(reasons: list, rugcheck_report: dict) -> tuple[str, str]:
    """Връща (текст, цвят) за колоната 'Риск' в таблицата - извлечено от
    вече изчислените reasons (⚠️ префикс = флаг от score_token), не отделна
    нова проверка, за да остане на 100% в синхрон с реалната логика."""
    if any(isinstance(r, str) and r.startswith("⚠️") for r in (reasons or [])):
        return "флагнат", "#c0392b"
    if not rugcheck_report:
        return "няма доклад", "#a66b00"
    return "чист", "#0a8a3f"


def _candidate_card_html(mint: str, data: dict, confirmed: bool) -> str:
    mint_safe = html.escape(mint) if _is_safe_mint(mint) else "(невалиден адрес)"
    score = data.get("score", 0)
    reasons = data.get("reasons") or []
    potential_label = data.get("potential_label", "")
    liquidity_usd = data.get("liquidity_usd", 0) or 0
    market_cap_usd = data.get("market_cap_usd", 0) or 0
    dexscreener_link = f"https://dexscreener.com/solana/{mint_safe}"
    pumpfun_link = f"https://pump.fun/coin/{mint_safe}"
    solscan_link = f"https://solscan.io/token/{mint_safe}"
    status_html = (
        '<p style="color:#0a8a3f;font-weight:600;margin:0 0 10px;">'
        '✅ Потвърден алърт - мина всички защити и прага (същото като имейла).</p>'
        if confirmed else
        '<p style="color:#a66b00;font-weight:600;margin:0 0 10px;">'
        '⏳ Все още се следи в момента - НЕ е потвърдена (score под прага, или чака още последователни '
        'проверки) - показвам я само защото е най-добрата налична в момента.</p>'
    )
    market_cap_html = f"${market_cap_usd:,.0f}" if market_cap_usd else "няма данни"
    potential_html = f"<p style='margin:0 0 10px;'>{html.escape(potential_label)}</p>" if potential_label else ""
    reasons_html = (
        "<p class='muted' style='margin:8px 0 0;'>" + html.escape("; ".join(reasons)) + "</p>"
    ) if reasons else ""
    return f"""
    <div class="card">
      {status_html}
      <p style="margin:0 0 6px;"><b>Score:</b> {score}/100</p>
      <p style="margin:0 0 10px;"><b>Ликвидност:</b> ${liquidity_usd:,.0f} | <b>Market Cap:</b> {market_cap_html}</p>
      {potential_html}
      <p class="addr">{mint_safe}</p>
      <p style="margin:10px 0 0;">
        <a class="link" href="{dexscreener_link}" target="_blank" rel="noopener">DexScreener →</a> ·
        <a class="link" href="{pumpfun_link}" target="_blank" rel="noopener">pump.fun →</a> ·
        <a class="link" href="{solscan_link}" target="_blank" rel="noopener">Solscan →</a>
      </p>
      {reasons_html}
    </div>
    """


def _potential_verdict_style(potential_label: str):
    """Извлича кратък вердикт (ДА/ПО-СКОРО ДА/ПО-СКОРО НЕ/НЕ) и цвят от
    пълния potential_label текст (виж scoring.py::classify_potential) - за
    компактна "Потенциал" колона в таблицата (по избор на потребителя, 18.09
    вечерта: "искам potential Long runner да не или към кое клониш и защо"
    да е видимо и в таблицата, не само в имейла/картата). Пълният текст с
    обяснението остава достъпен през title-атрибут (hover)."""
    text = potential_label or ""
    if "ПО-СКОРО ДА" in text:
        return "ПО-СКОРО ДА", "#1a7a3c"
    if "ПО-СКОРО НЕ" in text:
        return "ПО-СКОРО НЕ", "#a66b00"
    if "ДА" in text:
        return "ДА", "#0a8a3f"
    if "НЕ" in text:
        return "НЕ", "#c0392b"
    return "?", "#666f80"


def _render_tracked_table_html() -> str:
    """Пълна таблица с ВСИЧКИ монети, които ботът в момента следи на живо -
    по избор на потребителя (18.09 вечерта, "искам да траква с подобни
    графи"), вдъхновено от друг подобен инструмент, който показа. За разлика
    от "най-добра монета" картата по-горе, тук се вижда ЦЯЛАТА картина - и
    монетите, които изобщо не приближават прага."""
    with _latest_scores_lock:
        snapshot = sorted(_latest_scores.items(), key=lambda kv: kv[1].get("score", 0), reverse=True)
    if not snapshot:
        return '<p class="muted" style="margin-top:16px;">В момента няма следени монети.</p>'

    rows = []
    for mint, data in snapshot:
        if not _is_safe_mint(mint):
            continue  # защита в дълбочина - виж _is_safe_mint
        short_mint = f"{mint[:6]}...{mint[-4:]}"
        score = data.get("score", 0) or 0
        liquidity_usd = data.get("liquidity_usd", 0) or 0
        momentum_pct = data.get("momentum_pct", 0) or 0
        risk_text, risk_color = data.get("risk_label") or ("?", "#666f80")
        potential_label = data.get("potential_label", "")
        verdict_text, verdict_color = _potential_verdict_style(potential_label)
        progress = f"{data.get('consecutive_high_potential', 0)}/{config.MIN_POLLS_BEFORE_ALERT}"
        rows.append(f"""
        <tr>
          <td class="addr-cell" title="{mint}">{short_mint}</td>
          <td><button type="button" class="copy-btn" onclick="copyMint('{mint}', this)">Copy</button></td>
          <td>{score:.0f}</td>
          <td>${liquidity_usd:,.0f}</td>
          <td>{momentum_pct:+.1f}%</td>
          <td style="color:{risk_color};font-weight:600;">{risk_text}</td>
          <td style="color:{verdict_color};font-weight:600;cursor:help;" title="{html.escape(potential_label)}">{verdict_text}</td>
          <td>{progress}</td>
        </tr>""")
    return f"""
    <table class="tracked-table">
      <thead>
        <tr><th>Mint</th><th></th><th>Score</th><th>Ликвидност</th><th>Моментум</th><th>Риск</th><th>Потенциал</th><th>Алърт</th></tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def _render_manual_check_html(mint: str) -> str:
    """'Check one coin' - ръчна проверка на КОЙТО и да е mint адрес, дори да
    не се следи активно от бота (по избор на потребителя, 18.09 вечерта).
    За разлика от таблицата по-горе, тук правим ЖИВА заявка точно СЕГА -
    затова е по-бавно (реален HTTP round-trip), не се кешира.

    ВАЖНО: ако монетата не се следи активно, нямаме исторически first_price,
    затова моментум винаги е 0% тук - ясно обозначено в резултата, за да не
    се обърка с реален проследен моментум от таблицата по-горе."""
    if not _is_safe_mint(mint):
        return '<div class="card"><p style="margin:0;color:#c0392b;">Невалиден mint адрес.</p></div>'
    try:
        pairs = get_dexscreener_pairs(mint)
        rugcheck_report = get_rugcheck_report(mint)
    except Exception as e:
        log.warning("Ръчна /dashboard проверка за %s се провали: %s", mint, e)
        return '<div class="card"><p style="margin:0;color:#c0392b;">Грешка при извличане на данни - пробвай пак.</p></div>'
    best_pair = pairs[0] if pairs else {}
    result = score_token(mint, best_pair, rugcheck_report, momentum_pct=0.0)
    data = {
        "score": result.score,
        "reasons": result.reasons + ["ℹ️ моментум не е измерен тук (монетата не се следи активно от бота)"],
        "liquidity_usd": result.liquidity_usd,
        "market_cap_usd": result.market_cap_usd,
        "potential_label": result.potential_label,
    }
    return _candidate_card_html(mint, data, confirmed=result.is_high_potential)


def _render_best_candidate_html() -> str:
    # ВАЖНО (18.09 вечерта, по оплакване на потребителя - "седи на един" на
    # /dashboard): преди тук ВИНАГИ показвахме последния потвърден алърт,
    # завинаги, докато не дойде нов - дори ако е отпреди часове и монетата
    # вече е паднала/умряла. Сега показваме потвърден алърт само ако е
    # достатъчно ПРЕСЕН (в рамките на MONITOR_WINDOW_MINUTES - същия
    # прозорец, в който монетата така или иначе се следи активно) - иначе
    # падаме към текущата най-добра следена монета, за да страницата реално
    # се сменя с времето, не залепва за старо.
    with _recent_alerts_lock:
        alerts_snapshot = list(_recent_alerts)
    if alerts_snapshot:
        best = alerts_snapshot[0]
        is_fresh = True
        try:
            alerted_at = datetime.fromisoformat(best.get("alerted_at", ""))
            age_minutes = (datetime.now(timezone.utc) - alerted_at).total_seconds() / 60
            is_fresh = age_minutes <= config.MONITOR_WINDOW_MINUTES
        except (ValueError, TypeError):
            pass  # непарсваема дата - по-добре да покажем алърта, отколкото да скрием валиден резултат
        if is_fresh:
            return _candidate_card_html(best["mint"], best, confirmed=True)

    with _latest_scores_lock:
        scores_snapshot = dict(_latest_scores)
    if scores_snapshot:
        best_mint, best_data = max(scores_snapshot.items(), key=lambda kv: kv[1].get("score", 0))
        return _candidate_card_html(best_mint, best_data, confirmed=False)

    return (
        '<div class="card"><p style="margin:0;">В момента не следим нито една нова монета '
        '(или е твърде рано след стартиране на бота) - пробвай пак след малко.</p></div>'
    )


_DASHBOARD_PAGE = """<!doctype html>
<html lang="bg">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Memecoin Scanner</title>
<style>
  body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 32px auto;
          padding: 0 16px 40px; color:#12161f; background:#f7f8fa; }}
  h1 {{ font-size: 21px; margin-bottom: 4px; }}
  h2 {{ font-size: 16px; margin: 28px 0 8px; }}
  .muted {{ color:#666f80; font-size:13px; }}
  .status {{ color:#666f80; font-size:13px; margin-top:8px; }}
  .btn {{ display:inline-block; background:#5b47e0; color:#fff !important; padding:14px 30px; border-radius:10px;
          font-size:16px; font-weight:600; text-decoration:none; border:none; cursor:pointer; margin-top:14px; }}
  .btn-small {{ background:#5b47e0; color:#fff !important; padding:10px 18px; border-radius:8px; font-size:14px;
                font-weight:600; border:none; cursor:pointer; }}
  .card {{ background:#fff; border:1px solid #dde1e8; border-radius:12px; padding:18px; margin-top:20px; }}
  .addr {{ font-family:'SFMono-Regular',Consolas,monospace; font-size:14px; word-break:break-all;
           background:#f1f3f6; padding:12px; border-radius:8px; user-select:all; -webkit-user-select:all; }}
  a.link {{ color:#5b47e0; text-decoration:none; }}
  .tracked-table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid #dde1e8;
                     border-radius:12px; overflow:hidden; margin-top:10px; font-size:13px; }}
  .tracked-table th, .tracked-table td {{ padding:9px 10px; text-align:left; border-bottom:1px solid #eef0f4; }}
  .tracked-table th {{ background:#f1f3f6; color:#444d5c; font-weight:600; }}
  .tracked-table tr:last-child td {{ border-bottom:none; }}
  .addr-cell {{ font-family:'SFMono-Regular',Consolas,monospace; }}
  .copy-btn {{ background:#eef0f4; border:1px solid #dde1e8; border-radius:6px; padding:4px 10px;
               font-size:12px; cursor:pointer; }}
  .check-form input[type=text] {{ padding:12px; border:1px solid #dde1e8; border-radius:8px; font-size:14px;
                                   width:100%; max-width:420px; box-sizing:border-box; margin-right:8px; }}
  .check-form {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:8px; }}
</style>
</head>
<body>
  <h1>🚀 Memecoin Scanner</h1>
  <p class="muted">Ботът следи новите graduated монети на живо (PumpPortal) и праща email алърти автоматично
  (веднага при потвърден алърт, и на всеки {digest_minutes:.0f} мин heartbeat) - независимо дали отваряш тази
  страница.</p>
  <p class="status">В момента следени монети: <b>{tracked_count}</b> | Последно ново graduation: {last_migration}</p>

  <form method="get" action="/dashboard">
    <input type="hidden" name="check" value="1">
    <button class="btn" type="submit">Провери сега</button>
  </form>
  {result_html}

  <h2>Следени монети в момента</h2>
  {table_html}

  <h2>Провери конкретна монета</h2>
  <p class="muted">Провери на живо ЛЮБОЙ mint адрес, дори да не се следи активно от бота в момента (моментумът
  тогава винаги ще е 0%, защото нямаме исторически данни за нея).</p>
  <form method="get" action="/dashboard" class="check-form">
    <input type="hidden" name="check" value="1">
    <input type="text" name="mint" placeholder="mint адрес" value="{mint_value}">
    <button class="btn-small" type="submit">Провери</button>
  </form>
  {manual_html}

  <p class="muted" style="margin-top:30px;">Не е финансов съвет - graduated memecoin-ите са изключително
  волатилни и рискови. Пълните алърти пак идват по email, както досега.</p>

<script>
function copyMint(mint, btn) {{
  var restore = btn.textContent;
  function done(ok) {{ btn.textContent = ok ? 'Copied!' : 'Failed'; setTimeout(function() {{ btn.textContent = restore; }}, 1200); }}
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(mint).then(function() {{ done(true); }}, function() {{ done(false); }});
  }} else {{
    done(false);
  }}
}}
</script>
</body>
</html>"""


@app.route("/dashboard")
def dashboard():
    show_result = request.args.get("check") == "1"
    mint_param = (request.args.get("mint") or "").strip()

    result_html = _render_best_candidate_html() if show_result else ""
    table_html = _render_tracked_table_html() if show_result else '<p class="muted">Натисни "Провери сега", за да видиш таблицата.</p>'
    manual_html = _render_manual_check_html(mint_param) if mint_param else ""

    with _monitoring_lock:
        tracked_count = len(_monitoring)
    last_migration = _status.get("last_migration_at") or "няма още"

    return _DASHBOARD_PAGE.format(
        result_html=result_html,
        table_html=table_html,
        manual_html=manual_html,
        mint_value=html.escape(mint_param),
        tracked_count=tracked_count,
        last_migration=last_migration,
        digest_minutes=config.MIN_EMAIL_INTERVAL_SECONDS / 60,
    )


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
            with _monitoring_lock:
                mints = list(_monitoring)
            if mints:
                fresh = await asyncio.to_thread(get_dexscreener_pairs_batch, mints)
                _market_data_cache.update(fresh)
                # чистим кеша от монети, които вече не следим (излезли от
                # monitor_token поради timeout/алърт/грешка) - да не расте
                # неограничено през дни наред работа на процеса.
                mints_set = set(mints)
                for stale_mint in list(_market_data_cache.keys()):
                    if stale_mint not in mints_set:
                        _market_data_cache.pop(stale_mint, None)
        except Exception as e:
            log.warning("Грешка в централния market-data refresh loop: %s", e)
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)


async def _watch_digest_loop():
    """Периодичен 'heartbeat' email на всеки config.MIN_EMAIL_INTERVAL_SECONDS
    (18.09 вечерта, по изричен избор на потребителя: "изпраща ми имейл на
    всеки 5 минути за койн", "не да седи на един") - виж коментара в
    notifier.py::send_watch_digest за пълния контекст. Пуска се НЕЗАВИСИМО
    от monitor_token()-a, но споделя СЪЩОТО anti-spam темпо (notifier.
    can_send_now()) - ако вече е пратен истински потвърден алърт наскоро,
    просто прескача този цикъл, вместо да удвои честотата."""
    from scoring import MemeScoreResult
    while True:
        await asyncio.sleep(config.MIN_EMAIL_INTERVAL_SECONDS)
        try:
            if not config.WATCH_DIGEST_ENABLED or not config.ALERT_EMAIL_ENABLED:
                continue
            if not can_send_now():
                continue
            with _latest_scores_lock:
                scores_snapshot = dict(_latest_scores)
            if not scores_snapshot:
                continue
            best_mint, best_data = max(scores_snapshot.items(), key=lambda kv: kv[1].get("score", 0))
            best_score = best_data.get("score", 0) or 0
            # ПРОМЯНА (23.09, по изричен избор на потребителя: "искам да ми
            # дава само монети със score над 50, нали да си заслужава" -
            # оплакване за 30 heartbeat имейла, от които само 1 с score 50,
            # останалите дори 0) - преди тук се пращаше "най-добрата в
            # момента следена" БЕЗУСЛОВНО, дори когато буквално нищо
            # прилично не се следи. Сега просто прескачаме цикъла (чакаме
            # следващия, монетите се сменят на всеки ~90с-neколко мин), ако
            # дори най-добрият текущ score е под config.WATCH_DIGEST_MIN_SCORE.
            if best_score < config.WATCH_DIGEST_MIN_SCORE:
                log.info(
                    "Digest прескочен - най-добрият текущ score (%.0f, %s) е под прага %.0f, не си струва имейл.",
                    best_score, best_mint, config.WATCH_DIGEST_MIN_SCORE,
                )
                continue
            result = MemeScoreResult(
                mint=best_mint,
                score=best_data.get("score", 0),
                reasons=best_data.get("reasons") or [],
                liquidity_usd=best_data.get("liquidity_usd", 0) or 0,
                market_cap_usd=best_data.get("market_cap_usd", 0) or 0,
                potential_label=best_data.get("potential_label", ""),
            )
            send_watch_digest(result)
        except Exception as e:
            log.warning("Грешка в периодичния watch-digest loop: %s", e)


async def monitor_token(mint: str):
    """Следи монетата на живо и праща алърт веднага щом пресече прага -
    вместо да чака фиксирано изчакване и да провери само веднъж."""
    with _monitoring_lock:
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

            # За /dashboard таблицата (виж коментара при _latest_scores по-горе) -
            # ТУК, СЛЕД като consecutive_high_potential вече е обновен за тази
            # проверка, за да показва актуалния прогрес (X/MIN_POLLS_BEFORE_ALERT).
            with _latest_scores_lock:
                _latest_scores[mint] = {
                    "score": result.score,
                    "reasons": result.reasons,
                    "liquidity_usd": result.liquidity_usd,
                    "market_cap_usd": result.market_cap_usd,
                    "potential_label": result.potential_label,
                    "momentum_pct": momentum_pct,
                    "consecutive_high_potential": consecutive_high_potential,
                    "risk_label": _risk_label(result.reasons, rugcheck_report),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }

            if result.is_high_potential:
                already_rolling_over = drawdown_pct >= config.PEAK_DRAWDOWN_STOP_PCT
                confirmed = consecutive_high_potential >= config.MIN_POLLS_BEFORE_ALERT
                if confirmed and not already_rolling_over and config.ALERT_EMAIL_ENABLED and not can_send_now():
                    # ВАЖНО (18.09, по изричен избор на потребителя): монетата
                    # Е потвърдена и безопасна точно СЕГА, но anti-spam
                    # темпото (друг алърт е пратен наскоро - MIN_EMAIL_
                    # INTERVAL_SECONDS/MAX_EMAILS_PER_DAY) не позволява email
                    # този момент. НЕ break-ваме и НЕ пращаме стари данни по-
                    # късно - просто продължаваме да следим монетата (while
                    # цикълът продължава) и ще пробваме пак на СЛЕДВАЩИЯ poll
                    # с изцяло ПРЕСНИ данни (нов drawdown/score от кеша, и
                    # изцяло нова финална live проверка, ако темпото вече е
                    # освободено тогава) - вместо да губим готова, потвърдена
                    # монета само защото друг алърт е излязъл секунди по-рано.
                    log.info(
                        "%s: score е потвърден (%.1f), НО anti-spam темпото не позволява email точно сега - "
                        "продължавам да следя и ще пробвам пак на следващия poll с прясна проверка.",
                        mint, result.score,
                    )
                elif confirmed and not already_rolling_over:
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
                    # Освен спад в цената, проверяваме и живата ликвидност точно
                    # преди изпращане (18.09, след преглед на кода - "liquidity
                    # rug" чрез изтегляне на пула не винаги удря цената веднага
                    # в СЪЩИЯ момент, но е директен, недвусмислен сигнал сам по
                    # себе си - ако ликвидността точно СЕГА е под минимума, няма
                    # смисъл да чакаме драудаун-а да го "настигне").
                    final_liquidity_usd = (final_best_pair.get("liquidity") or {}).get("usd", 0) or 0
                    liquidity_collapsed = final_liquidity_usd < config.MIN_LIQUIDITY_USD
                    if final_drawdown_pct >= config.FINAL_CHECK_MAX_DRAWDOWN_PCT:
                        log.info(
                            "%s: финалната проверка точно преди изпращане показа спад %.1f%% от пика "
                            "(над прага %.1f%%) - отменям алърта в последния момент, монетата вече пада.",
                            mint, final_drawdown_pct, config.FINAL_CHECK_MAX_DRAWDOWN_PCT,
                        )
                    elif liquidity_collapsed:
                        log.info(
                            "%s: финалната проверка точно преди изпращане показа ликвидност $%.0f "
                            "(под минимума $%.0f) - отменям алърта в последния момент, изглежда като "
                            "изтегляне на ликвидността в движение.",
                            mint, final_liquidity_usd, config.MIN_LIQUIDITY_USD,
                        )
                    elif send_alert(result):
                        # send_alert() връща False САМО ако anti-spam темпото
                        # блокира точно в този момент (виж notifier.py) - тук
                        # все пак е възможно (рядко) заради race condition:
                        # друга едновременно следена монета (различен asyncio
                        # task) да е "изпреварила" и да е използвала темпото
                        # МЕЖДУ can_send_now() проверката по-горе и реалния
                        # HTTP send тук. True тук значи наистина обработено
                        # (пратено, или email-ите изключени/грешка - виж
                        # notifier.py::send_alert за пълния списък).
                        _status["last_alert_at"] = datetime.now(timezone.utc).isoformat()
                        # За /dashboard бутона - същата монета/данни като email алърта.
                        with _recent_alerts_lock:
                            _recent_alerts.insert(0, {
                                "mint": mint,
                                "score": result.score,
                                "reasons": result.reasons,
                                "liquidity_usd": result.liquidity_usd,
                                "market_cap_usd": result.market_cap_usd,
                                "potential_label": result.potential_label,
                                "alerted_at": datetime.now(timezone.utc).isoformat(),
                            })
                            del _recent_alerts[MAX_RECENT_ALERTS:]
                        break
                    else:
                        log.info(
                            "%s: anti-spam темпото блокира изпращането точно в последния момент (race с друга "
                            "монета) - продължавам да следя и ще пробвам пак на следващия poll.",
                            mint,
                        )
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
        with _monitoring_lock:
            _monitoring.discard(mint)
        with _latest_scores_lock:
            _latest_scores.pop(mint, None)
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
    loop.create_task(_watch_digest_loop())
    loop.run_until_complete(listen_for_migrations(_dispatch))


def _self_ping_loop():
    """Праща GET заявка към собствения публичен Render URL на всеки
    config.KEEP_ALIVE_PING_MINUTES минути.

    ЗАЩО (18.09, по оплакване на потребителя "от час и нещо няма никакви
    сигнали"): Render безплатният план приспива service-а след 15 мин БЕЗ
    входящ HTTP трафик (виж README) - докато спи, WebSocket връзката към
    PumpPortal се къса и се пропускат ВСИЧКИ graduation събития дотогава.
    Досега единствената защита беше ВЪНШЕН pinger (cron-job.org/UptimeRobot),
    който трябваше потребителят сам да настрои и поддържа активен - ако не е
    бил реално пуснат (или е спрял тихо), нищо вътре в бота не забелязва
    това. Затова сега ботът сам си праща заявка към собствения публичен
    адрес - Render автоматично слага RENDER_EXTERNAL_URL env variable-а с
    точно този адрес, затова не се налага да го въвеждаме ръчно.

    Ако RENDER_EXTERNAL_URL липсва (напр. локално стартиране, или хостинг
    без публичен URL) - просто прескачаме тихо, самопроверката не е
    приложима. Външният pinger пак е добра ДОПЪЛНИТЕЛНА защита (различен
    произход на трафика), но вече не е единствената линия."""
    external_url = os.getenv("RENDER_EXTERNAL_URL")
    if not external_url:
        log.info("RENDER_EXTERNAL_URL не е зададен (вероятно локално стартиране) - self-ping е изключен.")
        return
    log.info(
        "Self-ping активен: %s на всеки %d мин (пази Render service-а буден).",
        external_url, config.KEEP_ALIVE_PING_MINUTES,
    )
    while True:
        time.sleep(config.KEEP_ALIVE_PING_MINUTES * 60)
        try:
            requests.get(external_url, timeout=10)
            log.info("Self-ping към %s - ОК.", external_url)
        except Exception as e:
            log.warning("Self-ping към %s се провали: %s (ще пробвам пак след %d мин).", external_url, e, config.KEEP_ALIVE_PING_MINUTES)


def main():
    # _startup_self_check() вече е извикана веднъж при импортиране на модула
    # (виж по-горе, ПРЕДИ load_seen()) - не се налага втори път тук.
    thread = threading.Thread(target=_run_async_loop, daemon=True)
    thread.start()
    ping_thread = threading.Thread(target=_self_ping_loop, daemon=True)
    ping_thread.start()
    app.run(host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
