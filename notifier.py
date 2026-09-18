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
from zoneinfo import ZoneInfo

import requests

import config
from scoring import MemeScoreResult

log = logging.getLogger("notifier")

RESEND_API_URL = "https://api.resend.com/emails"

# --- "Копирай адреса" ---
# ВАЖНО (18.09): преди тук имаше бутон-линк към отделна MintClip страничка
# (Claude Artifact), която да чете адреса от URL параметър и да копира с
# едно докосване - идеята беше "email клиентите блокират JS, затова води
# към страница, която МОЖЕ да го направи". Оказа се, че Claude Artifact
# страниците се render-ват в sandbox iframe, който НЕ получава URL
# параметрите на външния линк изобщо (потвърдено с реален тест в браузър) -
# страницата винаги показваше "няма адрес", независимо какво е в линка.
# Това е ограничение на самата платформа, не поправим откъм HTML/JS код тук.
# Решение вместо това: адресът вече стои в имейла като ясно откроен,
# избираем текст - на телефон, задържане с пръст върху него показва системно
# "Copy" меню автоматично, без нужда от външна страница или бутон.


def _within_active_hours() -> bool:
    """Проверява дали текущият момент е в разрешения прозорец за имейли
    (config.ALERT_ACTIVE_START_* / ALERT_ACTIVE_END_* в ALERT_QUIET_HOURS_TZ).
    Ако timezone данните липсват по някаква причина - НЕ блокираме (по-добре
    да получиш имейл в грешен час, отколкото да мълчим заради bug)."""
    try:
        tz = ZoneInfo(config.ALERT_QUIET_HOURS_TZ)
    except Exception as e:
        log.warning("ALERT_QUIET_HOURS_TZ (%s) невалиден: %s - пропускам проверката за часове.", config.ALERT_QUIET_HOURS_TZ, e)
        return True
    now_local = datetime.now(tz)
    start = now_local.replace(hour=config.ALERT_ACTIVE_START_HOUR, minute=config.ALERT_ACTIVE_START_MINUTE, second=0, microsecond=0)
    end = now_local.replace(hour=config.ALERT_ACTIVE_END_HOUR, minute=config.ALERT_ACTIVE_END_MINUTE, second=0, microsecond=0)
    return start <= now_local <= end

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


def format_alert_html(result: MemeScoreResult) -> str:
    """HTML версия за email-а. Адресът е показан като ясно откроен, избираем
    текстов блок (виж коментара горе за "Копирай адреса" - защо няма линк/
    бутон към отделна страница) - на телефон, задържане с пръст върху него
    показва системно "Copy" меню автоматично."""
    dexscreener_link = f"https://dexscreener.com/solana/{result.mint}"
    market_cap_html = f"${result.market_cap_usd:,.0f}" if result.market_cap_usd else "няма данни"
    potential_html = (
        f"<p style='margin:0 0 12px;color:#12161f;'><b>Оценка:</b> {result.potential_label}</p>"
        if result.potential_label else ""
    )
    reasons_html = (
        "<p style='margin:0 0 6px;color:#12161f;'><b>Причини:</b></p>"
        "<ul style='margin:0 0 16px;padding-left:18px;color:#333;'>"
        + "".join(f"<li style='margin:0 0 4px;'>{r}</li>" for r in result.reasons)
        + "</ul>"
    ) if result.reasons else ""

    return f"""
    <div style="font-family:-apple-system,'Segoe UI',Roboto,sans-serif;max-width:480px;margin:0 auto;color:#12161f;">
      <h2 style="margin:0 0 14px;">🚀 Memecoin алърт</h2>
      <p style="margin:0 0 4px;"><b>Score:</b> {result.score}/100 ({result.estimated_multiplier})</p>
      <p style="margin:0 0 4px;"><b>Ликвидност:</b> ${result.liquidity_usd:,.0f}</p>
      <p style="margin:0 0 12px;"><b>Market Cap:</b> {market_cap_html}</p>
      {potential_html}
      <p style="margin:0 0 6px;color:#666f80;font-size:12px;">📋 Адрес на монетата - кликни/задръж върху него, после Ctrl+C (компютър) или "Copy" от менюто (телефон):</p>
      <p style="margin:0 0 16px;font-family:'SFMono-Regular',Consolas,monospace;font-size:14px;
                word-break:break-all;background:#f1f3f6;padding:14px 12px;border-radius:8px;color:#12161f;
                border:1px solid #dde1e8;user-select:all;-webkit-user-select:all;">
        {result.mint}
      </p>
      <p style="margin:0 0 16px;text-align:center;">
        <a href="{dexscreener_link}" style="color:#5b47e0;text-decoration:none;">Виж в DexScreener →</a>
      </p>
      {reasons_html}
      <p style="color:#8a93a6;font-size:12px;margin-top:18px;">
        Не е финансов съвет - graduated memecoin-ите са изключително волатилни и рискови.
      </p>
    </div>
    """


def can_send_now() -> bool:
    """Pure "peek" (не мърда никакво състояние) - True ако email, пратен точно
    СЕГА, НЕ би бил пропуснат заради anti-spam темпото
    (MIN_EMAIL_INTERVAL_SECONDS/MAX_EMAILS_PER_DAY).

    ЗАЩО СЪЩЕСТВУВА (18.09, по изричен избор на потребителя): main.py я
    ползва, за да прецени ПРЕДИ да похарчи финалната live проверка, дали
    изобщо си струва - ако темпото не позволява, main.py::monitor_token НЕ
    спира да следи монетата (не праща стари данни по-късно), а просто чака
    следващия poll и проверява пак с ПРЕСНИ данни, докато или темпото се
    освободи, или monitoring прозорецът (MONITOR_WINDOW_MINUTES) изтече.
    Преди тази промяна: ако две добри монети се потвърдяха в рамките на
    същия ~5-мин anti-spam прозорец, втората се губеше напълно (само лог,
    без email) - потребителят изрично поиска да не се случва това."""
    return _rate_limit_ok()


def send_alert(result: MemeScoreResult) -> bool:
    """Връща True ако е "обработено" (email пратен успешно, ИЛИ email-ите са
    изключени/не са конфигурирани, ИЛИ извън разрешените часове - в тези
    случаи чакане не помага, няма смисъл от retry), False САМО когато е
    пропуснат чисто заради anti-spam темпото - виж can_send_now() по-горе за
    защо тази разлика има значение за main.py::monitor_token."""
    message = format_alert(result)
    log.info("MEMECOIN ALERT:\n%s", message)

    if not config.ALERT_EMAIL_ENABLED:
        return True
    return _send_email(
        subject=f"[Memecoin Scanner] {result.mint[:8]}... - {result.estimated_multiplier}",
        body=message,
        html=format_alert_html(result),
    )


def send_watch_digest(result: MemeScoreResult) -> bool:
    """Периодичен 'heartbeat' email на всеки config.MIN_EMAIL_INTERVAL_SECONDS
    (18.09 вечерта, по изричен избор на потребителя: "изпраща ми имейл на
    всеки 5 минути за койн", "не да седи на един") - показва НАЙ-ДОБРАТА в
    момента следена монета, дори да НЕ е минала целия HIGH_POTENTIAL_
    THRESHOLD/MIN_POLLS_BEFORE_ALERT процес на потвърждение. За разлика от
    send_alert() (пълен потвърден сигнал), тук ЯСНО пишем в темата и тялото,
    че е само периодична, непотвърдена информация - за да не се обърка с
    истински потвърден алърт. Ползва СЪЩИЯ _send_email()/anti-spam темпо
    като send_alert(), затова не може да удвои честотата отгоре."""
    message = (
        "⏳ ПЕРИОДИЧНА МОНЕТА - все още НЕ е напълно потвърдена (score под прага, или чака още "
        "последователни проверки) - показана само защото е най-добрата в момента следена.\n\n"
        + format_alert(result)
    )
    log.info("MEMECOIN WATCH DIGEST:\n%s", message)

    if not config.ALERT_EMAIL_ENABLED:
        return True
    html = (
        "<p style='margin:0 0 14px;padding:10px 12px;background:#fff6e5;border-radius:8px;"
        "color:#a66b00;font-weight:600;'>⏳ Периодична монета - все още НЕ е напълно потвърдена "
        "(score под прага, или чака още проверки) - само информативно, показана защото в момента "
        "е най-добрата следена.</p>"
        + format_alert_html(result)
    )
    return _send_email(
        subject=f"[Memecoin Scanner] (непотвърдено, score {result.score:.0f}) {result.mint[:8]}...",
        body=message,
        html=html,
    )


def _send_email(subject: str, body: str, html: str = None) -> bool:
    """Връща True ако е "приключено" (пратен успешно, или причината да не се
    прати НЕ е anti-spam темпото - конфигурация/часове/HTTP грешка, retry
    не би помогнал), False САМО ако е пропуснат чисто заради темпото (виж
    can_send_now()/send_alert() по-горе)."""
    if not (config.RESEND_API_KEY and config.ALERT_EMAIL_TO):
        log.warning("Email алъртите са включени, но RESEND_API_KEY/ALERT_EMAIL_TO не са попълнени.")
        return True
    if not _within_active_hours():
        log.info(
            "Извън разрешените часове за имейли (%02d:%02d-%02d:%02d %s) - пропускам email-а (алъртът е в логовете).",
            config.ALERT_ACTIVE_START_HOUR, config.ALERT_ACTIVE_START_MINUTE,
            config.ALERT_ACTIVE_END_HOUR, config.ALERT_ACTIVE_END_MINUTE, config.ALERT_QUIET_HOURS_TZ,
        )
        return True
    if not _rate_limit_ok():
        return False
    payload = {
        "from": config.RESEND_FROM_EMAIL,
        "to": [config.ALERT_EMAIL_TO],
        "subject": subject,
        "text": body,
    }
    if html:
        # Resend показва html, ако е налично - text си остава fallback за
        # клиенти, които не го рендират. Виж format_alert_html() за бутона.
        payload["html"] = html
    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {config.RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        if resp.status_code >= 300:
            log.error("Resend отказа изпращането (%s): %s", resp.status_code, resp.text)
            return True  # HTTP грешка, не anti-spam темпо - retry тук не би помогнал по същия начин
        _mark_email_sent()
        log.info("Email алърт изпратен до %s през Resend", config.ALERT_EMAIL_TO)
        return True
    except Exception as e:
        log.error("Изпращането на email през Resend се провали: %s", e)
        return True  # мрежова грешка, не anti-spam темпо - вече е логнато, не искаме безкраен retry цикъл
