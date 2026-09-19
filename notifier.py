import logging
import smtplib
from email.mime.text import MIMEText

import config
from scoring import MemeScoreResult

log = logging.getLogger("notifier")


def format_alert(result: MemeScoreResult) -> str:
    dexscreener_link = f"https://dexscreener.com/solana/{result.mint}"
    lines = [
        f"Coin ID (mint): {result.mint}",
        f"Score: {result.score}/100 | Потенциал: {result.estimated_multiplier}",
        f"Ликвидност: ${result.liquidity_usd:,.0f}",
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
    if not (config.SMTP_USERNAME and config.SMTP_APP_PASSWORD and config.ALERT_EMAIL_TO):
        log.warning("Email алъртите са включени, но SMTP_*/ALERT_EMAIL_TO не са попълнени.")
        return
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = config.SMTP_USERNAME
    msg["To"] = config.ALERT_EMAIL_TO
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.SMTP_USERNAME, config.SMTP_APP_PASSWORD)
            server.send_message(msg)
        log.info("Email алърт изпратен до %s", config.ALERT_EMAIL_TO)
    except Exception as e:
        log.error("Изпращането на email се провали: %s", e)
