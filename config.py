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
# Горен праг на market cap - за да хващаме монети РАНО, преди да са
# набъбнали много. Монета над този таван никога не се score-ва/алъртва,
# независимо от другите фактори.
MAX_MARKET_CAP_USD = float(os.getenv("MAX_MARKET_CAP_USD", "200000"))
HIGH_POTENTIAL_THRESHOLD = float(os.getenv("HIGH_POTENTIAL_THRESHOLD", "65"))

ALERT_EMAIL_ENABLED = _bool("ALERT_EMAIL_ENABLED", False)
# --- Resend (https://resend.com) - изпраща email през HTTP API, не през Gmail SMTP.
# Ползваме го вместо Gmail App Password, защото Family Link/supervised Google
# акаунти не позволяват App Passwords изобщо. Виж README.md за регистрация.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "yani.kolev2011@gmail.com")

# --- Anti-spam защита за Resend дневната квота (безплатен tier = 100 имейла/ден
# за целия акаунт, споделен с другите ботове, ползващи същия RESEND_API_KEY) ---
# Не пращаме имейл по-често от веднъж на MIN_EMAIL_INTERVAL_SECONDS (300с = 5мин,
# значи максимум ~2 имейла на всеки 10 минути), плюс твърд дневен таван като
# резерва под истинския лимит на Resend. Алъртът винаги се вижда в Render Logs -
# само самото email изпращане се прескача, ако сме над темпото.
MIN_EMAIL_INTERVAL_SECONDS = int(os.getenv("MIN_EMAIL_INTERVAL_SECONDS", "300"))
# Memecoin ботът получава по-голямата част от общата дневна квота от 100
# (споделена с penny-stock-scanner) - там пращаме 10/ден, тук 90/ден.
MAX_EMAILS_PER_DAY = int(os.getenv("MAX_EMAILS_PER_DAY", "90"))

# --- "Тихи часове" за имейл алъртите - потребителят иска имейли САМО между
# 07:30 и 23:00 местно време (не иска да го буди бот през нощта). За разлика
# от penny-stock-scanner-а, memecoin ботът следи pump.fun 24/7 (crypto пазарът
# никога не спира) - затова тук границата реално има значение, не е просто
# резерва. Алъртите пак се логват в Render Logs денонощно - само самото
# изпращане на email се пропуска извън тези часове. ---
ALERT_QUIET_HOURS_TZ = os.getenv("ALERT_QUIET_HOURS_TZ", "Europe/Sofia")
ALERT_ACTIVE_START_HOUR = int(os.getenv("ALERT_ACTIVE_START_HOUR", "7"))
ALERT_ACTIVE_START_MINUTE = int(os.getenv("ALERT_ACTIVE_START_MINUTE", "30"))
ALERT_ACTIVE_END_HOUR = int(os.getenv("ALERT_ACTIVE_END_HOUR", "23"))
ALERT_ACTIVE_END_MINUTE = int(os.getenv("ALERT_ACTIVE_END_MINUTE", "0"))

# --- Impersonation филтър ---
# pump.fun монети, кръстени на известни хора/личности (Elon Musk, Trump и
# т.н.) practически НИКОГА не са реално създадени от въпросния човек - това
# е чест "hype" trick за rug pull. Понеже няма начин да потвърдим реална
# автентичност през безплатни API-та, монета чието име/символ съвпада с
# някое от тези ключови думи автоматично се пропуска (score=0), вместо да
# разчитаме на моментум/ликвидност да я скрият случайно.
IMPERSONATION_KEYWORDS = [
    kw.strip().lower()
    for kw in os.getenv(
        "IMPERSONATION_KEYWORDS",
        "elon,elonmusk,musk,spacex,neuralink,"
        "trump,donaldtrump,maga,"
        "biden,joebiden,kamala,kamalaharris,"
        "obama,barackobama,putin,zelensky,zelenskyy,"
        "kanye,yeezy,drake,kimkardashian,kardashian,"
        "messi,ronaldo,neymar,"
        "mrbeast,"
        "bezos,jeffbezos,zuckerberg,markzuckerberg,gates,billgates,"
        "buffett,warrenbuffett,cathiewood,sbf,sambankmanfried,"
        "taylorswift,beyonce,rihanna,kimjongun,"
        "vitalik,vitalikbuterin,buterin,satoshi,cz_binance,changpeng,zhao,"
        "justinsun,tron",
    ).split(",")
    if kw.strip()
]

# Само име на известна личност в тикъра НЕ е достатъчно, за да пропуснем
# монетата - "PAIDLON", "WIFELON", "$SUNGE" (Justin Sun Prize) са класически
# pump.fun meme/joke имена, НЕ твърдят реална връзка, и реално потребителят
# потвърди че такива монети са били добри (не instant rug, една дори +518%).
# Затова блокираме само когато ИМЕТО НА ЛИЧНОСТ + ДУМА ЗА "легитимност/
# официалност" се появят заедно - това е истинският сигнал за измама, която
# твърди реална връзка с човека (напр. "Elon Musk Official", "Real Trump
# Coin"), не обикновена meme препратка.
IMPERSONATION_LEGITIMACY_WORDS = [
    kw.strip().lower()
    for kw in os.getenv(
        "IMPERSONATION_LEGITIMACY_WORDS",
        "official,verified,genuine,authentic,foundation,realaccount,"
        "confirmed,endorsed,ceo",
    ).split(",")
    if kw.strip()
]

PORT = int(os.getenv("PORT", "10000"))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
