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
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "yani.kolev2011@gmail.com")

PORT = int(os.getenv("PORT", "10000"))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
