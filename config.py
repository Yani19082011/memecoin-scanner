import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


PUMPPORTAL_WS_URL = os.getenv("PUMPPORTAL_WS_URL", "wss://pumpportal.fun/api/data")

# --- Реално-времево следене вместо еднократна проверка след фиксирано изчакване ---
# Изчакване DexScreener да индексира новия pool (обикновено 1-5 мин, виж README)
INITIAL_INDEX_DELAY_SECONDS = int(os.getenv("INITIAL_INDEX_DELAY_SECONDS", "90"))
# На всеки колко секунди се проверява всяка следена монета
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
# Колко минути общо следим монетата, преди да се откажем (ако не пресече прага)
MONITOR_WINDOW_MINUTES = int(os.getenv("MONITOR_WINDOW_MINUTES", "45"))

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
HIGH_POTENTIAL_THRESHOLD = float(os.getenv("HIGH_POTENTIAL_THRESHOLD", "65"))

ALERT_EMAIL_ENABLED = _bool("ALERT_EMAIL_ENABLED", False)
# --- Resend (https://resend.com) - изпраща email през HTTP API, не през Gmail SMTP.
# Ползваме го вместо Gmail App Password, защото Family Link/supervised Google
# акаунти не позволяват App Passwords изобщо. Виж README.md за регистрация.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "yani.kolev2011@gmail.com")

PORT = int(os.getenv("PORT", "10000"))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
