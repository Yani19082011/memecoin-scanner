"""
Изпращане на алърти. Два канала:
  - лог (винаги, вижда се в Render "Logs" таба)
  - email през Resend (https://resend.com) - HTTP API, само ако
    ALERT_EMAIL_ENABLED=true и RESEND_API_KEY е попълнен.

Забележка: НЕ ползваме Gmail SMTP/App Password, защото Google не позволява
App Passwords на Family Link (supervised) акаунти. Resend е безплатна услуга,
праща email през обикновен HTTP POST с API ключ - виж README.md за стъпките.
"""
import logging
from datetime import datetime, timezone

import requests

import config
from scoring import MemeScoreResult

log = logging.getLogger("notifier")

RESEND_API_URL = "https://api.resend.com/emails"

# --- Anti-spam темпо-ограничител за Resend (виж config.MIN_EMAIL_INTERVAL_SECONDS /
# MAX_EMAILS_PER_DAY) - пази в паметта на процеса, реду се при restart на Render,
# но това е ок - целта е само да не гърмим 20 имейла наведнъж при серия алърти. ---
_last_sent_at = None
_daily_count = 0
_daily_reset_date = None


def _rate_limit_ok() -> bool:
    global _last_sent_at, _daily_count, _daily_reset_date
    now = datetime.now(timezone.utc)
    today = now.date()

    if _daily_reset_date != today:
        _daily_reset_date = today
        _daily_count = 0

    if _daily_count >= config.MAX_EMAILS_PER_DAY:
        log.warning(
            "Дневният лимит от %d имейла е достигнат - пропускам email-а (алъртът е в логовете).",
            config.MAX_EMAILS_PER_DAY,
        )
        return False

    if _last_sent_at is not None:
        elapsed = (now - _last_sent_at).total_seconds()
        if elapsed < config.MIN_EMAIL_INTERVAL_SECONDS:
            log.info(
                "Прескачам email (анти-спам темпо) - оставащи %.0fс до следващия разрешен имейл.",
                config.MIN_EMAIL_INTERVAL_SECONDS - elapsed,
            )
            return False

    return True


def _mark_email_sent():
    global _last_sent_at, _daily_count
    _last_sent_at = datetime.now(timezone.utc)
    _daily_count += 1


def format_alert(result: MemeScoreResult) -> str:
    dexscreener_link = f"https://dexscreener.com/solana/{result.mint}"
    lines = [
        f"Coin ID (mint): {result.mint}",
        f"Score: {result.score}/100 | Потенциал: {result.estimated_multiplier}",
        f"Ликвидност: ${result.liquidity_usd:,.0f}",
        f"Market Cap: ${result.market_cap_usd:,.0f}" if result.market_cap_usd else "Market Cap: няма данни",
        f"Оценка (спекулативна, НЕ прогноза): {result.potential_label}" if result.potential_label else "",
        f"DexScreener: {dexscreener_link}",
        "Причини: " + "; ".join(result.reasons) if result.reasons else "",
        "",
        "Не е финансов съвет - graduated memecoin-ите са изключително волатилни и рискови.",
    ]
    return "\n".join(l for l in lines if l is not None)


def send_alert(result: MemeScoreResult):
    message = format_alert(result)
    log.info("MEMECOIN ALERT:\n%s", message)

    if config.ALERT_EMAIL_ENABLED:
        _send_email(subject=f"[Memecoin Scanner] {result.mint[:8]}... - {result.estimated_multiplier}", body=message)


def _send_email(subject: str, body: str):
    if not (config.RESEND_API_KEY and config.ALERT_EMAIL_TO):
        log.warning("Email алъртите са включени, но RESEND_API_KEY/ALERT_EMAIL_TO не са попълнени.")
        return
    if not _rate_limit_ok():
        return
    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {config.RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": config.RESEND_FROM_EMAIL,
                "to": [config.ALERT_EMAIL_TO],
                "subject": subject,
                "text": body,
            },
            timeout=15,
        )
        if resp.status_code >= 300:
            log.error("Resend отказа изпращането (%s): %s", resp.status_code, resp.text)
        else:
            _mark_email_sent()
            log.info("Email алърт изпратен до %s през Resend", config.ALERT_EMAIL_TO)
    except Exception as e:
        log.error("Изпращането на email през Resend се провали: %s", e)
