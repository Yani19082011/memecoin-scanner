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

import requests

import config
from scoring import MemeScoreResult

log = logging.getLogger("notifier")

RESEND_API_URL = "https://api.resend.com/emails"


def format_alert(result: MemeScoreResult) -> str:
    dexscreener_link = f"https://dexscreener.com/solana/{result.mint}"
    lines = [
        f"Coin ID (mint): {result.mint}",
        f"Score: {result.score}/100 | Потенциал: {result.estimated_multiplier}",
        f"Ликвидност: ${result.liquidity_usd:,.0f}",
        f"Market Cap: ${result.market_cap_usd:,.0f}" if result.market_cap_usd else "Market Cap: няма данни",
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
            log.info("Email алърт изпратен до %s през Resend", config.ALERT_EMAIL_TO)
    except Exception as e:
        log.error("Изпращането на email през Resend се провали: %s", e)
