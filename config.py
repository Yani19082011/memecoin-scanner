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
MIN_POLLS_BEFORE_ALERT = int(os.getenv("MIN_POLLS_BEFORE_ALERT", "3"))
PEAK_DRAWDOWN_STOP_PCT = float(os.getenv("PEAK_DRAWDOWN_STOP_PCT", "10"))
# (17.09) Потребителят първо поиска по-чест поток (~1/5 мин), после уточни
# приоритета: по-важно е монетата да НЕ е вече мъртва, когато я отвори,
# отколкото честотата - "може и 1 на 7-8 минути, просто да не е ръгпълната
# в следващите 10 секунди". Затова оставаме на 3 последователни потвърждения
# (не 2) - плюс виж FINAL_CHECK_BEFORE_SEND по-долу и коментара в
# main.py::monitor_token за нова защита точно в последния момент преди
# изпращане.

# На всеки колко проверки да опресняваме RugCheck доклада наново (не само
# докато е бил празен) - виж коментара в main.py::monitor_token за реалния
# rug pull случай (17.09), който показа че стар/остарял RugCheck доклад
# може да продължи да изглежда "чист" дълго след като монетата реално се е
# влошила (нова Low Liquidity/insider флагове, паднал LP lock %).
RUGCHECK_REFRESH_EVERY_N_POLLS = int(os.getenv("RUGCHECK_REFRESH_EVERY_N_POLLS", "2"))

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

# --- Финална проверка "в последната секунда" точно преди изпращане ---
# (17.09) Потребителят иска преди всичко монетата да НЕ е вече мъртва, когато
# я отвори - дори секунди преди/след изпращането на алърта. Дотук решението
# "да пратим" се вземаше от данните на САМИЯ poll, който го задейства - но
# между този poll и реалното изпращане може вече да е минал момент. Затова
# main.py::monitor_token сега прави ОЩЕ ЕДНА, съвсем свежа live проверка
# (директна DexScreener заявка, не кеша) точно преди send_alert() - ако
# цената вече е паднала над този праг спрямо пика МЕЖДУ последното
# потвърждение и този момент, алъртът се отменя в последния момент, вместо
# да пратим нещо вече мъртво.
FINAL_CHECK_MAX_DRAWDOWN_PCT = float(os.getenv("FINAL_CHECK_MAX_DRAWDOWN_PCT", "8"))

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
#
# ВАЖНО (17.09) - таван на Resend безплатния план: 300с = точно "1 имейл на
# 5 мин" темпо, каквото поиска потребителят - това само по себе си НЕ е
# проблем. Реалният таван е MAX_EMAILS_PER_DAY по-долу + споделеният
# Resend лимит от 100 имейла/ден за ЦЕЛИЯ акаунт (двата бота заедно). Ако
# се пращаше буквално 1 имейл на 5 мин през целия активен прозорец
# (07:30-23:00 = 15.5ч), това би било ~186 имейла САМО за memecoin бота -
# почти 2x над целия безплатен Resend лимит. Тоест "1 на 5 мин" е горен
# ТЕМП (максимална честота), не гарантирана устойчива честота цял ден -
# реалната честота зависи и от това колко монети реално минават филтрите.
# Ако искаш буквално устойчиво 1 на 5 мин по цял ден, ще трябва платен
# Resend план (безплатният физически не го побира) - кажи, ако искаш да
# видим цената.
MIN_EMAIL_INTERVAL_SECONDS = int(os.getenv("MIN_EMAIL_INTERVAL_SECONDS", "300"))
# Дневната квота (100 общо за акаунта) е разпределена 70/30 между двата бота
# по избор на потребителя (18.09: "70 memecoins 30 pennystocks") - тук 70,
# penny-stock-scanner държи 30 (виж неговия config.py).
MAX_EMAILS_PER_DAY = int(os.getenv("MAX_EMAILS_PER_DAY", "70"))

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
