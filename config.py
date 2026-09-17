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

# --- Защита срещу "купуване на върха" ---
# Потребителска обратна връзка (17.09): 3 алърта, при които монетата вече е
# била на локален връх/вече паднала точно когато алъртът е пристигнал
# ("покачи се съвсем малко за секунда и после падна", "секунди след като
# ми го прати койна беше вече мъртъв"). Причината: преди пращахме алърт
# веднага щом score-ът пресече прага на ПЪРВАТА проверка (poll #1) -
# това хваща и монети, които тъкмо в този миг вече са на върха на
# кратък spike и обръщат надолу. Двете защити по-долу изискват алъртът
# да е ПОТВЪРДЕН (не еднократен spike) и монетата да НЕ е вече паднала
# осезаемо от най-високата видяна цена, преди да пратим email:
#   1. MIN_POLLS_BEFORE_ALERT - не пращай на самия първи poll, изчакай
#      поне толкова последователни проверки над прага.
#   2. PEAK_DRAWDOWN_STOP_PCT - ако цената вече е паднала с този % от
#      пика, откакто следим монетата, приемаме че вече сме изпуснали
#      върха и НЕ пращаме (просто продължаваме да следим тихо).
# Компромис: добавя ~POLL_INTERVAL_SECONDS допълнително забавяне преди
# първия възможен алърт - по избор на потребителя ("опитвай се да не
# намираш повече такива"), приемаме да пропуснем някой и друг бърз мувър,
# за да намалим лошите/мъртви алърти.
MIN_POLLS_BEFORE_ALERT = int(os.getenv("MIN_POLLS_BEFORE_ALERT", "2"))
PEAK_DRAWDOWN_STOP_PCT = float(os.getenv("PEAK_DRAWDOWN_STOP_PCT", "10"))

# --- Защита срещу "liquidity rug" (LP не е заключен) ---
# Реален случай (17.09): монета score=76, mint authority revoke-нат, freeze
# authority revoke-нат, 0 high-severity RugCheck флага - изглеждаше чиста,
# но rug pull-na и потребителят загуби всичко. Причината, видима едва след
# ръчна проверка на пълния RugCheck доклад: монетата имаше 2 pool-а -
# единият 100% locked, НО покриваше само 2.75% от реалната ликвидност;
# другият (97.25% от резерва) беше 0% locked. RugCheck-ският "risks" масив
# изобщо НЕ флагна това (само 2 "warn" флага за друго) - трябва директно
# да проверим markets[].lp.lpLockedPct, претеглено по markets[].lp.pctReserve,
# защото ИМЕННО там е реалният rug вектор: собственикът може да изтегли
# незаключената ликвидност по всяко време. Ако претеглената % заключена
# ликвидност е под MIN_LP_LOCKED_PCT, ТВЪРДО отхвърляме монетата (score=0),
# независимо от другите фактори - виж scoring.py::_weighted_lp_locked_pct.
MIN_LP_LOCKED_PCT = float(os.getenv("MIN_LP_LOCKED_PCT", "50"))

# --- Твърд блок при explicit "Low Liquidity" RugCheck риск ---
# Реален сравнителен тест (17.09) на 5 живи изхода: 2-те rug-нали монети
# бяха ЕДИНСТВЕНИТЕ от 5-те с RugCheck-ски "Low Liquidity" риск в списъка
# ("danger" за едната, дори само "warn" за другата) - нито добрите, нито
# "лошата, но не rug" монета го имаха. Затова: наличие на този риск (на
# КАКЪВТО и да е level, дори "warn") -> твърд блок, независимо от score-а -
# виж scoring.py::score_token.
BLOCK_ON_LOW_LIQUIDITY_RISK = os.getenv("BLOCK_ON_LOW_LIQUIDITY_RISK", "true").strip().lower() in ("1", "true", "yes", "on")

# --- Твърд блок при екстремна insider концентрация ---
# Реален случай (17.09): TRUPAI ("Trump Paid") - RugCheck откри 33 insider
# wallet клъстъра (5 отделни мрежи, до 9 wallet-а в една). За сравнение SVEN
# (легитимно добра монета, +518%) имаше само 4 клъстъра - затова преди
# третирахме graphInsidersDetected само информативно (виж scoring.py). 33
# обаче е категорично различен мащаб от "няколко клъстъра" - при такава
# концентрация shte блокираме директно, независимо от другите фактори. Праг
# 10 е избран съзнателно високо над SVEN-ското 4 (за да не хващаме случайно
# легитимни монети с по няколко клъстъра), но много под TRUPAI-ското 33.
MAX_INSIDER_CLUSTERS = float(os.getenv("MAX_INSIDER_CLUSTERS", "10"))

# Вдигнато от 5000 на 15000 (17.09) - същият сравнителен тест показа ясен
# градиент: 2-те rug-нали монети имаха най-плитка обща ликвидност ($2,366 и
# $5,058), "лошата, но не rug" монета - средно ($16,360), двете добри - най-
# дълбоко ($33,075 и $92,309). Плитка ликвидност е по-лесна за източване и с
# по-малко нужен капитал за манипулация - фундаментален признак за риск, не
# просто случайност в тази извадка.
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "15000"))
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
