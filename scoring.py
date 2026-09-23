"""
Композитен score 0-100 за graduated монета, само от публични данни
(ликвидност, обем, риск флагове, реална търговска активност) - без
"insider" информация.
"""
import logging
import re
import time
from dataclasses import dataclass, field

import config

log = logging.getLogger("scoring")

WEIGHTS = {
    "liquidity": 15,            # достатъчна ликвидност над MIN_LIQUIDITY_USD
    "volume": 15,               # силен обем спрямо ликвидността
    "momentum": 20,             # реално наблюдавана % промяна в цената откакто следим монетата
    "buy_pressure": 15,         # НОВО - дял купувания от сделките(1ч) - proxy за органичен интерес
    "early_stage_bonus": 10,    # НОВО - колко "прясна" е монетата (виж config.EARLY_STAGE_*)
    "no_mint_authority": 10,    # mint authority е revoke-нат (не може да printne повече токени)
    "no_freeze_authority": 10,  # freeze authority revoke-нат
    "low_risk_flags": 5,        # малко/никакви high-severity риск флагове от RugCheck
}


@dataclass
class MemeScoreResult:
    mint: str
    score: float
    reasons: list = field(default_factory=list)
    liquidity_usd: float = 0.0
    market_cap_usd: float = 0.0
    potential_label: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_high_potential(self) -> bool:
        return self.score >= config.HIGH_POTENTIAL_THRESHOLD

    @property
    def estimated_multiplier(self) -> str:
        # Груба, чисто индикативна етикетировка спрямо score - НЕ прогноза.
        if self.score >= 85:
            return "~3x+"
        if self.score >= 75:
            return "~2x"
        if self.score >= config.HIGH_POTENTIAL_THRESHOLD:
            return "~1.5x"
        return "низък"


# --- Точкуване - НОВА СТРАТЕГИЯ (18.09 вечерта) ---
# ИСТОРИЯ: на 18.09 сутринта пробвахме "sweet spot" точкуване (награждаваше
# здравословен ЛИКВИДНОСТ/ОБЕМ/МОМЕНТУМ среден диапазон) - часове наред
# НИТО ЕДИН алърт, твърде строго. После се върнахме на старата монотонна
# логика от 17.09 (колкото по-силен показателят, толкова повече точки).
# Сега, по изричен избор на потребителя ("остави стари стратегии, искам
# нова стратегия... искам да търсиш тренд"), правим ДЕЙСТВИТЕЛНО нова
# логика, не поредна вариация на старите тегла: добавяме две измерения,
# които преди изобщо не се пресмятаха от вече наличните безплатни данни -
# "buy pressure" (_buy_pressure_points) и "прясна монета" бонус
# (_early_stage_points) - виж config.py за пълния research-контекст и
# честните уговорки за качеството на източниците.
#
# ВАЖНО - това НЕ маха НИКОЯ от защитите срещу rug pull от 17.09/18.09
# (твърд блок при momentum>=80%, активен mint/freeze authority, "danger"
# RugCheck флаг, концентрирано "market cap per holder", LP lock<50%,
# insider клъстъри>=10, wash-trading обем/ликвидност>15x) - потребителят
# изрично поиска тези да ОСТАНАТ. Добавяме и един НОВ тесен твърд блок
# ("death spiral", виж score_token) плюс един мек (не-блокиращ) наказателен
# сигнал за подозрително едри средни сделки (wash-trading proxy).


def _liquidity_points(liquidity_usd: float) -> float:
    if liquidity_usd >= config.MIN_LIQUIDITY_USD * 4:
        return WEIGHTS["liquidity"]
    if liquidity_usd >= config.MIN_LIQUIDITY_USD * 2:
        return WEIGHTS["liquidity"] * 0.7
    if liquidity_usd >= config.MIN_LIQUIDITY_USD:
        return WEIGHTS["liquidity"] * 0.4
    return 0


def _volume_points(volume_h1: float, liquidity_usd: float) -> float:
    if not liquidity_usd:
        return 0
    ratio = volume_h1 / liquidity_usd
    if ratio >= 5:
        return WEIGHTS["volume"]
    if ratio >= 2:
        return WEIGHTS["volume"] * 0.7
    if ratio >= 1:
        return WEIGHTS["volume"] * 0.4
    return 0


def _momentum_points(momentum_pct: float) -> float:
    """momentum_pct = реално наблюдавана % промяна в цената откакто следим
    монетата (не DexScreener-ското h1/h24, което за минутна монета е шум)."""
    if momentum_pct >= 50:
        return WEIGHTS["momentum"]
    if momentum_pct >= 25:
        return WEIGHTS["momentum"] * 0.7
    if momentum_pct >= 10:
        return WEIGHTS["momentum"] * 0.4
    return 0


def _buy_pressure_points(buy_ratio):
    """НОВО (18.09 вечерта) - buy_ratio = купувания / (купувания+продажби) за
    последния 1ч, от DexScreener txns.h1. Proxy за "органичен интерес" -
    DexScreener няма безплатно поле за уникален брой holder-и/wallet-и, но
    посока/съотношение на сделките е реален, безплатен сигнал за тренд.
    None (нямаме txns данни изобщо) -> 0 точки, предпазливо (същия принцип
    като "без RugCheck доклад -> 0 риск точки" по-долу в score_token)."""
    if buy_ratio is None:
        return 0
    if buy_ratio >= config.MIN_BUY_RATIO_FOR_FULL_TREND_BONUS:
        return WEIGHTS["buy_pressure"]
    if buy_ratio >= 0.55:
        return WEIGHTS["buy_pressure"] * 0.6
    if buy_ratio >= 0.5:
        return WEIGHTS["buy_pressure"] * 0.3
    return 0


def _early_stage_points(age_hours):
    """НОВО (18.09 вечерта) - колко часа от pairCreatedAt (кога се е появил
    DEX пула). Пълен бонус до EARLY_STAGE_MAX_AGE_HOURS, после линейно
    избледнява до 0 на EARLY_STAGE_FADE_AGE_HOURS - виж config.py за
    research-контекста (единствен, нерецензиран източник - евристика, не
    доказан факт). None (нямаме pairCreatedAt) -> 0 точки, предпазливо."""
    if age_hours is None or age_hours < 0:
        return 0
    if age_hours <= config.EARLY_STAGE_MAX_AGE_HOURS:
        return WEIGHTS["early_stage_bonus"]
    if age_hours >= config.EARLY_STAGE_FADE_AGE_HOURS:
        return 0
    span = config.EARLY_STAGE_FADE_AGE_HOURS - config.EARLY_STAGE_MAX_AGE_HOURS
    if span <= 0:
        return 0
    remaining = (config.EARLY_STAGE_FADE_AGE_HOURS - age_hours) / span
    return WEIGHTS["early_stage_bonus"] * remaining


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def is_likely_impersonation(name: str, symbol: str) -> bool:
    """Блокира САМО реални твърдения за автентичност (име на личност +
    "official"/"verified"/... заедно), не обикновени meme препратки като
    "Paid Elon" или "WIFELON" - такива са класически pump.fun joke имена,
    не твърдят реална връзка, и потвърдено има добри резултати сред тях."""
    combined = _normalize(name) + _normalize(symbol)
    if not combined:
        return False
    has_celeb_name = any(kw in combined for kw in config.IMPERSONATION_KEYWORDS)
    if not has_celeb_name:
        return False
    return any(word in combined for word in config.IMPERSONATION_LEGITIMACY_WORDS)


def classify_potential(
    market_cap_usd: float,
    momentum_pct: float,
    volume_h1: float,
    liquidity_usd: float,
    buy_ratio=None,
    age_hours=None,
) -> str:
    """Едно изречение, ВИНАГИ започващо с изричен отговор от точно 4 нива -
    "ДА" / "ПО-СКОРО ДА" / "ПО-СКОРО НЕ" / "НЕ" - чисто спекулативна,
    евристична преценка, НЕ прогноза и НЕ финансов съвет. "НЕЯСНО" вече НЕ
    съществува като възможен отговор (изрично поискано от потребителя на
    17.09 - винаги трябва да накланя ясно в една посока).

    НОВО (18.09 вечерта) - buy_ratio/age_hours добавят реален "тренд" сигнал
    към преценката (виж config.py за research-контекста), не само market
    cap/momentum/volume/liquidity, както преди. И двата са по избор (None,
    ако DexScreener не е дал txns/pairCreatedAt данни за тази монета) -
    класификацията пак работи, просто без тренд-нюанса в текста.

    "Short squeeze" не съществува тук физически (graduated pump.fun монети
    се търгуват само на обикновен AMM, няма borrow/маржин механизъм за да се
    шортват - затова няма смисъл такава категория).

    ВАЖНО (18.09 вечерта, НОВА СТРАТЕГИЯ): market_cap_usd вече никога не
    може да надвиши config.MAX_MARKET_CAP_USD (твърд, единствен таван -
    score_token() блокира преди изобщо да стигнем дотук) - старата
    "разширена зона" (150k-500k, за TIGRINO-подобни случаи) вече не
    съществува, по изричен избор на потребителя за по-прост/по-строг праг.
    Тази функция вече не се разклонява по market cap над обичайния диапазон."""
    ratio = (volume_h1 / liquidity_usd) if liquidity_usd else 0
    # Две нива на "колко комфортна е ликвидността" спрямо голия минимум
    # (MIN_LIQUIDITY_USD, вдигнат на $15k след сравнителния тест от 17.09 -
    # виж config.py) - монета точно на прага е технически преминала филтъра,
    # но не е "стабилна" в същия смисъл като една с 2-3x+ повече дълбочина.
    comfortable_liquidity = liquidity_usd >= config.MIN_LIQUIDITY_USD * 1.5
    very_comfortable_liquidity = liquidity_usd >= config.MIN_LIQUIDITY_USD * 3
    strong_trend = buy_ratio is not None and buy_ratio >= config.MIN_BUY_RATIO_FOR_FULL_TREND_BONUS
    is_fresh = age_hours is not None and age_hours <= config.EARLY_STAGE_MAX_AGE_HOURS

    # --- Твърдо НЕ - силен риск сигнал, независимо от останалото ---
    # ЗАБЕЛЕЖКА (18.09, намерено при цялостен преглед на кода): момент с
    # momentum_pct>=80 вече никога не стига дотук - score_token() по-горе
    # твърдо блокира (score=0, без изобщо да вика classify_potential) на
    # точно същия праг, преди тази функция изобщо да се извика. Клонът
    # остава като защита "на всеки случай" (напр. ако някой друг код път
    # извика classify_potential() директно, без да мине през score_token),
    # без реален разход - но реално мъртъв код при нормалния поток.
    if momentum_pct >= 80:
        return "⚠️ Long runner: НЕ - вече силно изпомпана, влизаш късно с повишен риск точно сега да е dump"
    if not comfortable_liquidity:
        return (
            f"⚠️ Long runner: НЕ - ликвидността (${liquidity_usd:,.0f}) е твърде близо до минимума, "
            "не достатъчно дълбока да е стабилна (сравнителен тест 17.09: плитка ликвидност беше общото "
            "при реалните rug pull случаи)"
        )

    is_early_and_hot = bool(market_cap_usd) and market_cap_usd < 60_000 and momentum_pct >= 25 and ratio >= 1
    is_still_small = bool(market_cap_usd) and market_cap_usd < 150_000

    trend_note = ""
    if buy_ratio is not None:
        trend_note = f", {buy_ratio*100:.0f}% от сделките(1ч) са купувания"
        if is_fresh:
            trend_note += f", появила се преди <{config.EARLY_STAGE_MAX_AGE_HOURS:.0f}ч"

    # --- ДА - най-силният профил: рано, реален моментум, висок обем, силен тренд, И дълбока ликвидност ---
    if is_early_and_hot and very_comfortable_liquidity and strong_trend:
        return (
            "🚀 Long runner: ДА (спекулативно) - нисък market cap + силен ранен моментум + висок обем спрямо "
            f"ликвидност + реален купувачски тренд{trend_note} + необичайно дълбока ликвидна база, но "
            "повечето такива монети пак отиват на 0"
        )
    if is_early_and_hot and very_comfortable_liquidity:
        return ("🚀 Long runner: ДА (спекулативно) - нисък market cap + силен ранен моментум + висок обем спрямо "
                "ликвидност + необичайно дълбока ликвидна база, но повечето такива монети пак отиват на 0")
    if is_early_and_hot and strong_trend:
        return (
            "🚀 Long runner: ПО-СКОРО ДА (спекулативно) - нисък market cap + силен ранен моментум + реален "
            f"купувачски тренд{trend_note}, ликвидността е стабилна (не необичайно дълбока), пак висок rug риск"
        )
    if is_early_and_hot:
        return ("🚀 Long runner: ПО-СКОРО ДА (спекулативно) - нисък market cap + силен ранен моментум + висок обем "
                "спрямо ликвидност, ликвидността е стабилна (не необичайно дълбока), пак висок rug риск")
    if is_still_small and very_comfortable_liquidity and strong_trend:
        return f"🌱 Long runner: ПО-СКОРО ДА - все още малка по market cap, дълбока ликвидност за размера ѝ, и реален купувачски тренд{trend_note}"
    if is_still_small and very_comfortable_liquidity:
        return "🌱 Long runner: ПО-СКОРО ДА - все още малка по market cap, ликвидността е необичайно дълбока за размера ѝ"
    if is_still_small and strong_trend:
        return f"🌱 Long runner: ПО-СКОРО ДА - все още малка по market cap, и има реален купувачски тренд{trend_note}, но ликвидността не е необичайно дълбока"
    if is_still_small:
        return "🌱 Long runner: ПО-СКОРО НЕ - все още малка по market cap, но профилът (моментум/обем/тренд) не е особено убедителен"

    return "➖ Long runner: НЕ - вече не е 'ранно' влизане, моментумът изглежда до голяма степен изразходван"


def _weighted_lp_locked_pct(rugcheck_report: dict):
    """Претеглена % заключена ликвидност (LP lock) - претеглена по реалния
    дял (pctReserve) на всеки market/pool от общата ликвидност, НЕ проста
    средна стойност. Виж config.MIN_LP_LOCKED_PCT за защо - монета с 2
    pool-а (единият 100% locked но 3% от резерва, другият 0% locked но 97%
    от резерва) НЕ е "50% locked" - реално е ~3% заключена ликвидност, и
    точно това пропуска RugCheck-ският собствен 'risks' масив.
    Връща None ако нямаме markets/lp данни (нов доклад, все още неиндексиран
    market и т.н.) - в такъв случай НЕ блокираме тук (недостатъчно данни),
    другите защити (liquidity floor, no report предупреждение) си остават."""
    markets = rugcheck_report.get("markets") or []
    total_weight = 0.0
    weighted_locked = 0.0
    for market in markets:
        lp = (market or {}).get("lp") or {}
        pct_reserve = lp.get("pctReserve")
        locked_pct = lp.get("lpLockedPct")
        if pct_reserve is None or locked_pct is None:
            continue
        total_weight += pct_reserve
        weighted_locked += pct_reserve * locked_pct
    if total_weight <= 0:
        return None
    return weighted_locked / total_weight


def score_token(mint: str, best_pair: dict, rugcheck_report: dict, momentum_pct: float = 0.0) -> MemeScoreResult:
    if not best_pair:
        return MemeScoreResult(mint=mint, score=0, reasons=["няма DexScreener pair - вероятно още не е индексиран"])

    liquidity_usd = (best_pair.get("liquidity") or {}).get("usd", 0) or 0
    market_cap_usd = best_pair.get("marketCap") or best_pair.get("fdv") or 0
    volume_h1 = (best_pair.get("volume") or {}).get("h1", 0) or 0

    base_token = best_pair.get("baseToken") or {}
    token_name = base_token.get("name", "")
    token_symbol = base_token.get("symbol", "")

    # --- НОВИ полета за "тренд" стратегията (18.09 вечерта) - извлечени от
    # DexScreener-ски txns/priceChange/pairCreatedAt, преди изобщо НЕ се
    # ползваха от кода, въпреки че идват безплатно с всяка pair заявка. Виж
    # config.py за пълния research-контекст. Всичко .get() defensively -
    # липсващо поле НИКОГА не гърми, просто води до "нямаме данни" (None/0)
    # надолу по веригата, третирано предпазливо (0 точки, не блок), освен
    # explicit death-spiral проверката веднага по-долу.
    txns = best_pair.get("txns") or {}
    h1_txns = txns.get("h1") or {}
    m5_txns = txns.get("m5") or {}
    buys_h1 = h1_txns.get("buys") or 0
    sells_h1 = h1_txns.get("sells") or 0
    buys_m5 = m5_txns.get("buys") or 0
    sells_m5 = m5_txns.get("sells") or 0
    price_change_h1 = (best_pair.get("priceChange") or {}).get("h1")
    pair_created_at_ms = best_pair.get("pairCreatedAt")
    age_hours = None
    if pair_created_at_ms:
        age_hours = max(0.0, (time.time() * 1000 - pair_created_at_ms) / 3_600_000)

    total_txns_h1 = buys_h1 + sells_h1
    buy_ratio = (buys_h1 / total_txns_h1) if total_txns_h1 > 0 else None
    avg_trade_size_usd = (volume_h1 / total_txns_h1) if total_txns_h1 > 0 else None

    # Impersonation филтър - виж config.IMPERSONATION_KEYWORDS. Проверяваме
    # първо, преди всичко останало - няма смисъл да score-ваме монета, която
    # най-вероятно е "hype" измама с чуждо име.
    if is_likely_impersonation(token_name, token_symbol):
        return MemeScoreResult(
            mint=mint,
            score=0,
            reasons=[
                f"'{token_name or token_symbol}' твърди реална връзка с публична личност (official/verified/...) - "
                "пропускам, защото не можем безплатно да потвърдим автентичност (чест scam vector)"
            ],
            liquidity_usd=liquidity_usd,
            market_cap_usd=market_cap_usd,
            raw={"pair": best_pair, "rugcheck": rugcheck_report},
        )

    # НОВ твърд блок (18.09 вечерта, НОВА СТРАТЕГИЯ) - "death spiral": ако в
    # последните 5 минути има 0 купувания, но поне
    # config.DEATH_SPIRAL_MIN_M5_SELLS продажби, И цената вече е паднала с
    # поне config.DEATH_SPIRAL_H1_DROP_PCT% през последния час - активен
    # dump в момента, не просто "слаб момент". Умишлено ТЕСЕН (изисква И
    # трите условия едновременно, включително минимален брой продажби, не
    # просто "0 купувания" - иначе съвсем тиха, но здрава монета би могла
    # случайно да засегне 0-buys прозорец) - виж config.py за защо е тесен
    # (да не пресъздаде "sweet spot" часове-без-алърт проблема).
    if (
        buys_m5 == 0
        and sells_m5 >= config.DEATH_SPIRAL_MIN_M5_SELLS
        and price_change_h1 is not None
        and price_change_h1 <= -config.DEATH_SPIRAL_H1_DROP_PCT
    ):
        return MemeScoreResult(
            mint=mint,
            score=0,
            reasons=[
                f"⚠️ 'death spiral': 0 купувания срещу {sells_m5} продажби в последните 5 мин, цената вече "
                f"пада {price_change_h1:.1f}% за последния час - активен dump в момента, пропускам."
            ],
            liquidity_usd=liquidity_usd,
            market_cap_usd=market_cap_usd,
            raw={"pair": best_pair, "rugcheck": rugcheck_report},
        )

    # Твърд минимален праг за ликвидност - НЕ просто точки от скоринга.
    # Без това, монета с $3 ликвидност може да "спечели" почти пълни точки за
    # обем (обем/ликвидност съотношението избухва при нищожен знаменател) и
    # да мине прага само заради моментум - въпреки че реално няма никаква
    # ликвидност, в която да влезеш/излезеш. Затова просто отхвърляме такива
    # монети още тук, независимо от score-а на другите фактори.
    if liquidity_usd < config.MIN_LIQUIDITY_USD:
        return MemeScoreResult(
            mint=mint,
            score=0,
            reasons=[f"ликвидност твърде ниска (${liquidity_usd:,.2f}) - под минимума ${config.MIN_LIQUIDITY_USD:,.0f}"],
            liquidity_usd=liquidity_usd,
            market_cap_usd=market_cap_usd,
            raw={"pair": best_pair, "rugcheck": rugcheck_report},
        )

    # Твърд блок при съмнително високо съотношение обем(1ч)/ликвидност - виж
    # config.MAX_VOLUME_TO_LIQUIDITY_RATIO за реалния случай (18.09,
    # 6jUqDqQid...pump - ~57x точно преди rug pull). Такова съотношение почти
    # никога не е органично - или wash trading (изкуствено надуван обем, за
    # да изглежда монетата "гореща"), или изключително тънка ликвидност,
    # която дори малка продажба може да срине рязко. И двете са rug сигнали,
    # независимо от другите фактори.
    volume_to_liquidity_ratio = (volume_h1 / liquidity_usd) if liquidity_usd else 0
    if volume_to_liquidity_ratio > config.MAX_VOLUME_TO_LIQUIDITY_RATIO:
        return MemeScoreResult(
            mint=mint,
            score=0,
            reasons=[
                f"⚠️ съотношение обем(1ч)/ликвидност твърде високо ({volume_to_liquidity_ratio:.1f}x - "
                f"${volume_h1:,.0f} обем срещу ${liquidity_usd:,.0f} ликвидност) - над прага "
                f"{config.MAX_VOLUME_TO_LIQUIDITY_RATIO:.0f}x, вероятно wash trading, пропускам."
            ],
            liquidity_usd=liquidity_usd,
            market_cap_usd=market_cap_usd,
            raw={"pair": best_pair, "rugcheck": rugcheck_report},
        )

    # Горен таван на market cap - целта е да хващаме монети РАНО, докато са
    # все още малки, не след като вече са набъбнали значително. market_cap_usd
    # може да е 0/непознат за съвсем нови монети (DexScreener още не го е
    # изчислил) - в такъв случай НЕ филтрираме тук (нямаме основание да
    # отхвърлим заради непозната стойност), но филтрираме твърдо, ако имаме
    # реална стойност над прага.
    #
    # ПРОМЯНА (18.09 вечерта, НОВА СТРАТЕГИЯ) - махнахме старата двустепенна
    # "разширена зона" (110k-500k за монети с чист RugCheck доклад) - по
    # изричен избор на потребителя: "да не е над 120k mc", без изключения.
    # Виж config.MAX_MARKET_CAP_USD за честната бележка за TIGRINO-компромиса.
    if market_cap_usd and market_cap_usd > config.MAX_MARKET_CAP_USD:
        return MemeScoreResult(
            mint=mint,
            score=0,
            reasons=[
                f"market cap твърде висок (${market_cap_usd:,.0f}) - над твърдия таван "
                f"${config.MAX_MARKET_CAP_USD:,.0f}, никога не продължаваме да следим над това."
            ],
            liquidity_usd=liquidity_usd,
            market_cap_usd=market_cap_usd,
            raw={"pair": best_pair, "rugcheck": rugcheck_report},
        )

    # Твърд блок при вече прекалено висок моментум - ВАЖНО (18.09, реален
    # случай: FKhooZdA...pump, score 79, market cap $104,981, momentum
    # +98.3% - алъртът излезе с текстово предупреждение "⚠️ Long runner: НЕ -
    # вече силно изпомпана", но самият score ВСЕ ПАК го пресметна достатъчно
    # високо, за да мине прага, защото _momentum_points() дава НАЙ-МНОГО точки
    # точно на момента >=100% - т.е. архитектурно противоречие: класификацията
    # казваше "рисково", докато score-ът го възнаграждаваше. Потребителят
    # последва алърта и загуби пари. Прагът 80% съвпада с този в
    # classify_potential() по-долу - монета толкова изпомпана вече е по-скоро
    # на път да dump-не, отколкото да продължи нагоре, затова вече изобщо НЕ
    # пращаме email за нея, вместо просто да предупредим в текста.
    if momentum_pct >= 80:
        return MemeScoreResult(
            mint=mint,
            score=0,
            reasons=[
                f"моментум твърде висок (+{momentum_pct:.0f}%) - монетата вероятно вече е силно изпомпана и по-скоро "
                "на път да dump-не, отколкото да продължи нагоре - пропускам, независимо от другите фактори"
            ],
            liquidity_usd=liquidity_usd,
            market_cap_usd=market_cap_usd,
            raw={"pair": best_pair, "rugcheck": rugcheck_report},
        )

    # Твърд филтър за незаключена ликвидност ("liquidity rug") - виж
    # config.MIN_LP_LOCKED_PCT за реалния случай, който доведе до това.
    # Проверяваме САМО ако имаме markets/lp данни - липса на данни тук НЕ
    # означава "безопасно", просто нямаме основание да блокираме конкретно
    # заради това (другите защити си остават).
    lp_locked_pct = _weighted_lp_locked_pct(rugcheck_report) if rugcheck_report else None
    if lp_locked_pct is not None and lp_locked_pct < config.MIN_LP_LOCKED_PCT:
        # ВРЕМЕННО диагностично логване (18.09, вечерта - потребителят докладва
        # "часове наред нито един алърт"). Живи логове показаха монета с $2M+
        # РЕАЛНА ликвидност блокирана точно тук с изчислени ~0% LP lock - силно
        # подозрително за толкова голяма/установена монета. Хипотеза: pump.fun
        # migration-ите обичайно ИЗГАРЯТ (burn) LP токените при graduation
        # (стандартен механизъм, дори по-сигурен от time-lock, защото никога не
        # може да се изтегли), а RugCheck-ският lpLockedPct може да не брои
        # "изгорено" като "заключено" в схемата си - т.е. ПОЧТИ ВСЯКА graduated
        # pump.fun монета може систематично да пада тук, независимо колко е
        # безопасна реално. Не пипаме самия праг/логика, докато нямаме суровите
        # markets/lp данни на живо, за да потвърдим или отхвърлим хипотезата -
        # логваме ги тук на INFO ниво (вижда се директно в Render Logs) точно
        # когато блокът се задейства, вместо да гадаем.
        log.info(
            "LP lock блок за %s (ликвидност $%.0f): изчислено ~%.1f%% locked (праг %.0f%%) - "
            "суров markets масив от RugCheck: %s",
            mint, liquidity_usd, lp_locked_pct, config.MIN_LP_LOCKED_PCT,
            rugcheck_report.get("markets"),
        )
        return MemeScoreResult(
            mint=mint,
            score=0,
            reasons=[
                f"⚠️ само ~{lp_locked_pct:.0f}% от ликвидността е заключена (LP lock), претеглено по реалния "
                f"дял на всеки pool - под минимума {config.MIN_LP_LOCKED_PCT:.0f}%. Собственикът може да изтегли "
                "незаключената ликвидност по всяко време ('liquidity rug') - пропускам, независимо от другите фактори."
            ],
            liquidity_usd=liquidity_usd,
            market_cap_usd=market_cap_usd,
            raw={"pair": best_pair, "rugcheck": rugcheck_report},
        )

    # Твърд блок при explicit "Low Liquidity" RugCheck риск (на КАКЪВТО и да
    # е level) - виж config.BLOCK_ON_LOW_LIQUIDITY_RISK за сравнителния тест
    # от 17.09, който показа че точно този флаг беше единственото нещо общо
    # между двете рeaлни rug-нали монети, липсващо при добрите. RugCheck-ският
    # собствен алгоритъм явно вижда нещо в комбинацията от фактори (текуща
    # ликвидност, дълбочина на пула и т.н.), което ние не преизчисляваме сами -
    # затова просто му се доверяваме тук директно, вместо да пресмятаме
    # собствен праг.
    if config.BLOCK_ON_LOW_LIQUIDITY_RISK and rugcheck_report:
        risks_list = rugcheck_report.get("risks") or []
        low_liquidity_risk = next(
            (r for r in risks_list if isinstance(r, dict) and "low liquidity" in str(r.get("name", "")).lower()),
            None,
        )
        if low_liquidity_risk:
            return MemeScoreResult(
                mint=mint,
                score=0,
                reasons=[
                    f"⚠️ RugCheck директно флагна '{low_liquidity_risk.get('name')}' "
                    f"(level={low_liquidity_risk.get('level')}) - и при двете rug-нали монети в сравнителния "
                    "тест от 17.09 точно този флаг беше налице, при нито една от добрите - пропускам."
                ],
                liquidity_usd=liquidity_usd,
                market_cap_usd=market_cap_usd,
                raw={"pair": best_pair, "rugcheck": rugcheck_report},
            )

    # Твърд блок при екстремна insider концентрация - виж config.MAX_INSIDER_CLUSTERS.
    # Реален случай (17.09): TRUPAI ("Trump Paid") - 33 insider клъстъра (5
    # отделни мрежи, до 9 wallet-а в една) - несравнимо повече от SVEN
    # (легитимно добра монета, +518%, само 4 клъстъра/4.55% от supply), затова
    # преди не пипахме прага (виж по-долу - под прага си остава чисто
    # информативно). TRUPAI вече се блокираше и на ликвидност/Low-Liquidity
    # флага по-горе, но добавяме тази защита директно, за бъдещи случаи, при
    # които ликвидността може да изглежда ОК, а insider концентрацията пак е
    # екстремна - 33 е категорично различен мащаб от "няколко клъстъра", не
    # просто малко над средното.
    insiders_detected_check = (rugcheck_report.get("graphInsidersDetected") or 0) if rugcheck_report else 0
    if insiders_detected_check >= config.MAX_INSIDER_CLUSTERS:
        return MemeScoreResult(
            mint=mint,
            score=0,
            reasons=[
                f"⚠️ RugCheck откри {insiders_detected_check} insider wallet клъстъра - над прага "
                f"{config.MAX_INSIDER_CLUSTERS:.0f} (за сравнение: SVEN, легитимно добра монета, имаше само 4) - "
                "твърде концентрирано разпределение, пропускам независимо от другите фактори."
            ],
            liquidity_usd=liquidity_usd,
            market_cap_usd=market_cap_usd,
            raw={"pair": best_pair, "rugcheck": rugcheck_report},
        )

    # Твърд блок при "danger" ниво риск флаг ОТ КАКЪВТО И ДА Е ТИП (не само
    # Low Liquidity), както и при все още активен mint/freeze authority -
    # СТРУКТУРЕН ПРОБЛЕМ, намерен при преглед на кода на 18.09 след доклад,
    # че "всички монети днеска бяха ruggpulnati" въпреки предишните защити:
    # ликвидност (20т) + обем (20т) + моментум (30т) сами по себе си дават ДО
    # 70 ТОЧКИ - над HIGH_POTENTIAL_THRESHOLD (65) - т.е. монета с изкуствено
    # изпомпан обем/моментум можеше да мине прага дори със ЗАПАЗЕН mint
    # authority, активен freeze authority И множество "danger" риск флага от
    # RugCheck, защото тези неща досега носеха само точки (общо 30т от 100) -
    # лесно компенсирани от самия pump. А силен изкуствен pump точно преди
    # rug pull е класическата схема, не изключение - затова точно тези неща
    # не бива да могат да се "компенсират" с добри пазарни числа. Сега
    # спират монетата твърдо, независимо колко силен изглежда pump-ът.
    if rugcheck_report:
        risks_list_hard = rugcheck_report.get("risks") or []
        danger_risk = next(
            (r for r in risks_list_hard if isinstance(r, dict) and str(r.get("level", "")).lower() == "danger"),
            None,
        )
        if danger_risk:
            return MemeScoreResult(
                mint=mint,
                score=0,
                reasons=[
                    f"⚠️ RugCheck флагна '{danger_risk.get('name')}' на ниво 'danger' - твърд блок, "
                    "независимо от ликвидност/обем/моментум."
                ],
                liquidity_usd=liquidity_usd,
                market_cap_usd=market_cap_usd,
                raw={"pair": best_pair, "rugcheck": rugcheck_report},
            )
        risk_names_hard = {str(r.get("name", "")).lower() for r in risks_list_hard if isinstance(r, dict)}
        if any("mint" in n and "authority" in n for n in risk_names_hard):
            return MemeScoreResult(
                mint=mint,
                score=0,
                reasons=["⚠️ mint authority все още активен - собственикът може да отпечата нови токени по всяко време - твърд блок."],
                liquidity_usd=liquidity_usd,
                market_cap_usd=market_cap_usd,
                raw={"pair": best_pair, "rugcheck": rugcheck_report},
            )
        if any("freeze" in n for n in risk_names_hard):
            return MemeScoreResult(
                mint=mint,
                score=0,
                reasons=["⚠️ freeze authority все още активен - собственикът може да замрази wallet-и на държатели - твърд блок."],
                liquidity_usd=liquidity_usd,
                market_cap_usd=market_cap_usd,
                raw={"pair": best_pair, "rugcheck": rugcheck_report},
            )
        # Твърд блок при "market cap per holder" риск ОТ КАКЪВТО И ДА Е level -
        # ВАЖНО (18.09, реален случай: 6jUqDqQid...pump, RugCheck флагна точно
        # това на ниво 'warn' - под 'danger', значи не спираше преди - и
        # монетата рухна). Концентрирано разпределение (малко хора държат
        # непропорционално голяма част от market cap-а спрямо броя държатели)
        # е структурен риск сам по себе си - шепа wallet-и могат да съборят
        # цената - независимо какво ниво на severity му е дало RugCheck.
        if any("market cap" in n and "holder" in n for n in risk_names_hard):
            return MemeScoreResult(
                mint=mint,
                score=0,
                reasons=[
                    "⚠️ RugCheck флагна 'high market cap per holder' - концентрирано разпределение, "
                    "шепа wallet-и могат да съборят цената - твърд блок, независимо от level."
                ],
                liquidity_usd=liquidity_usd,
                market_cap_usd=market_cap_usd,
                raw={"pair": best_pair, "rugcheck": rugcheck_report},
            )

    reasons = []
    points = 0.0

    liq_pts = _liquidity_points(liquidity_usd)
    if liq_pts:
        points += liq_pts
        reasons.append(f"ликвидност ${liquidity_usd:,.0f}")

    vol_pts = _volume_points(volume_h1, liquidity_usd)
    if vol_pts:
        points += vol_pts
        reasons.append(f"обем/1ч спрямо ликвидност силен (${volume_h1:,.0f})")

    mom_pts = _momentum_points(momentum_pct)
    if mom_pts:
        points += mom_pts
        reasons.append(f"реален моментум +{momentum_pct:.1f}% откакто следим монетата")

    # --- НОВИ "тренд" измерения (18.09 вечерта) ---
    buy_pts = _buy_pressure_points(buy_ratio)
    if buy_pts:
        points += buy_pts
        reasons.append(f"силен купувачски тренд ({buy_ratio*100:.0f}% от сделките(1ч) са купувания)")
    elif buy_ratio is None:
        reasons.append("ℹ️ няма данни за брой сделки(1ч) - 'buy pressure' точки НЕ се дават предпазливо")

    early_pts = _early_stage_points(age_hours)
    if early_pts:
        points += early_pts
        reasons.append(f"прясна монета (~{age_hours:.1f}ч от появата на пула)")

    # Мек (не-блокиращ) наказателен сигнал за подозрително едър среден размер
    # на сделка (wash-trading proxy) - виж config.SUSPICIOUS_AVG_TRADE_SIZE_USD.
    # Различен от твърдия MAX_VOLUME_TO_LIQUIDITY_RATIO блок по-горе - тук
    # само отнема точки, не отхвърля монетата directно.
    if avg_trade_size_usd is not None and avg_trade_size_usd > config.SUSPICIOUS_AVG_TRADE_SIZE_USD:
        points = max(0.0, points - config.WASH_TRADE_SIZE_PENALTY_POINTS)
        reasons.append(
            f"⚠️ необичайно едър среден размер на сделка (${avg_trade_size_usd:,.0f}/сделка за последния час) - "
            f"възможен wash-trading признак, -{config.WASH_TRADE_SIZE_PENALTY_POINTS:.0f} точки (не твърд блок)"
        )

    # RugCheck risk флагове - defensively, различни възможни имена на полета.
    #
    # ВАЖНО: ако rugcheck_report е празен (RugCheck още не е индексирал
    # монетата - много чест случай секунди след graduation), ТОВА НЕ Е
    # доказателство, че монетата е безопасна - просто нямаме данни. Преди
    # тук се третираше "няма флагове" (защото няма доклад изобщо) като
    # "чисто" и се даваха всичките 30 точки за риск - точно това пропускаше
    # rug pull-ове, направени секунди след graduation, преди RugCheck да
    # смогне да индексира монетата. Затова: без доклад -> 0 точки за риск,
    # изрично предупреждение, вместо мълчаливо да приемем "безопасно".
    if not rugcheck_report:
        reasons.append("⚠️ RugCheck още няма доклад за тази монета (твърде нова) - риск точки НЕ се дават предпазливо")
    else:
        risks = rugcheck_report.get("risks") or []
        risk_names = {str(r.get("name", "")).lower() for r in risks if isinstance(r, dict)}

        # mint authority / freeze authority вече са ТВЪРД БЛОК по-горе - ако
        # някое от двете беше активно, изобщо нямаше да стигнем дотук.
        # Точките тук вече са гарантирани - добавяме ги само за прозрачност
        # в reasons (вижда се в имейла защо score-ът е такъв).
        points += WEIGHTS["no_mint_authority"]
        reasons.append("mint authority revoke-нат")
        points += WEIGHTS["no_freeze_authority"]
        reasons.append("freeze authority revoke-нат")

        # "danger" ниво вече е ТВЪРД БЛОК по-горе - тук остава само "high"
        # ниво (по-леко от danger, но пак си струва да се знае - само отнема
        # точки, не блокира).
        high_severity = [r for r in risks if isinstance(r, dict) and str(r.get("level", "")).lower() == "high"]
        if len(high_severity) == 0:
            points += WEIGHTS["low_risk_flags"]
        else:
            reasons.append(f"⚠️ {len(high_severity)} 'high'-severity риск флага от RugCheck (под 'danger', не блокира сам по себе си)")

        # Само ИНФОРМАТИВНО (не пипа score-а) - под прага MAX_INSIDER_CLUSTERS
        # (виж твърдия блок по-горе за над-прага случая). Сравнителен тест
        # (17.09) показа, че "graphInsidersDetected > 0" присъстваше при rug/
        # decline случаите, НО и при поне един легитимно добър случай (SVEN,
        # +518%, 4 клъстъра/4.55% от supply) - под прага не е достатъчно чист
        # сигнал за наказание в score-а, само за прозрачност в имейла.
        if insiders_detected_check:
            reasons.append(f"ℹ️ RugCheck откри {insiders_detected_check} insider wallet клъстър(а) - под прага, информативно")

    return MemeScoreResult(
        mint=mint,
        score=round(points, 1),
        reasons=reasons,
        liquidity_usd=liquidity_usd,
        market_cap_usd=market_cap_usd,
        potential_label=classify_potential(
            market_cap_usd, momentum_pct, volume_h1, liquidity_usd, buy_ratio=buy_ratio, age_hours=age_hours
        ),
        raw={"pair": best_pair, "rugcheck": rugcheck_report},
    )
