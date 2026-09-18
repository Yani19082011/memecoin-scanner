"""
Композитен score 0-100 за graduated монета, само от публични данни
(ликвидност, обем, риск флагове) - без "insider" информация.
"""
import re
from dataclasses import dataclass, field

import config

WEIGHTS = {
    "liquidity": 20,       # достатъчна ликвидност над MIN_LIQUIDITY_USD
    "volume": 20,          # силен обем спрямо ликвидността
    "momentum": 30,        # реално наблюдавана % промяна в цената откакто следим монетата
    "no_mint_authority": 10,   # mint authority е revoke-нат (не може да printne повече токени)
    "no_freeze_authority": 10,  # freeze authority revoke-нат
    "low_risk_flags": 10,   # малко/никакви high-severity риск флагове от RugCheck
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


# --- "Sweet spot" точкуване (18.09, нова стратегия по избор на потребителя,
# за тестване - "дай ми твоя стратегия, ще я тествам") ---
# ЗАЩО: старото точкуване по-долу беше МОНОТОННО - "колкото повече, толкова
# по-добре", чак до самия ръб на твърдите блокове (MAX_VOLUME_TO_LIQUIDITY_
# RATIO, momentum>=80%). Това създаваше извратен стимул: монета точно ПРЕДИ
# ръба (напр. momentum=75%, обем/ликвидност=14x) получаваше НАЙ-МНОГО точки -
# същия клас проблем, който вече доведе до реален загубен алърт (FKhooZdA...
# pump, +98.3% моментум, все още score-нат високо преди да сложим твърдия
# блок на 80%). Новата логика възнаграждава "здравословния среден диапазон"
# (органичен, устойчив растеж), не екстремумите точно до ръба на риска -
# монета точно преди твърд блок вече не получава максимални точки, а
# намаляващи, защото статистически е по-близо до rug/exhaustion профила.
# Всички твърди блокове (score_token по-долу) остават НЕПРОМЕНЕНИ - те са
# изградени от реални rug случаи и не са предмет на този експеримент.


def _liquidity_points(liquidity_usd: float, market_cap_usd: float) -> float:
    """Вместо плосък праг само на абсолютната ликвидност, гледаме
    СЪОТНОШЕНИЕТО ликвидност/market cap - $30k ликвидност е много по-
    здравословна при $40k market cap (75% от mc), отколкото при $400k market
    cap (7.5% от mc), дори числото $30k да е същото. По-дълбока относителна
    ликвидност = по-трудна за source, по-малък slippage, по-малко вероятно
    да е "тънка обвивка" около малка реална стойност. Ако market cap е
    непознат (съвсем нова монета, DexScreener още не го е изчислил), падаме
    обратно на старата абсолютна логика - нямаме основание за съотношение."""
    if not market_cap_usd:
        if liquidity_usd >= config.MIN_LIQUIDITY_USD * 4:
            return WEIGHTS["liquidity"]
        if liquidity_usd >= config.MIN_LIQUIDITY_USD * 2:
            return WEIGHTS["liquidity"] * 0.7
        if liquidity_usd >= config.MIN_LIQUIDITY_USD:
            return WEIGHTS["liquidity"] * 0.4
        return 0
    ratio = liquidity_usd / market_cap_usd
    if ratio >= 0.5:
        return WEIGHTS["liquidity"]
    if ratio >= 0.3:
        return WEIGHTS["liquidity"] * 0.7
    if ratio >= 0.15:
        return WEIGHTS["liquidity"] * 0.4
    return 0


def _volume_points(volume_h1: float, liquidity_usd: float) -> float:
    """Sweet spot вместо монотонно нарастване: съотношение 1x-5x е
    "здравословен" органичен интерес - над 5x (но все още под твърдия
    wash-trading блок config.MAX_VOLUME_TO_LIQUIDITY_RATIO) вече започва да
    прилича на изкуствено надуван обем, дори да не е достатъчно екстремно за
    твърдия блок, затова точките започват да намаляват, не да продължават
    да растат."""
    if not liquidity_usd:
        return 0
    ratio = volume_h1 / liquidity_usd
    if ratio < 0.3:
        return 0
    if ratio < 1:
        return WEIGHTS["volume"] * 0.3
    if ratio <= 5:
        return WEIGHTS["volume"]
    if ratio < config.MAX_VOLUME_TO_LIQUIDITY_RATIO:
        return WEIGHTS["volume"] * 0.4
    return 0  # практически недостижимо тук - твърдият блок вече би отхвърлил монетата преди score_token да стигне дотук


def _momentum_points(momentum_pct: float) -> float:
    """momentum_pct = реално наблюдавана % промяна в цената откакто следим
    монетата (не DexScreener-ското h1/h24, което за минутна монета е шум).

    Sweet spot 15%-60%: достатъчно движение, за да е реален сигнал (не шум),
    но все още далеч от твърдия блок на 80% (вече изпомпана). Под 15% - все
    още твърде рано да се различи от случаен шум. Между 60-80% - вече близо
    до зоната, в която реално се rug-ва/dump-ва (виж твърдия блок по-долу),
    затова точките намаляват, вместо да продължават да растат както преди."""
    if momentum_pct < 5:
        return 0
    if momentum_pct < 15:
        return WEIGHTS["momentum"] * 0.35
    if momentum_pct <= 60:
        return WEIGHTS["momentum"]
    if momentum_pct < 80:
        return WEIGHTS["momentum"] * 0.5
    return 0  # практически недостижимо тук - твърдият momentum>=80% блок вече би отхвърлил монетата преди score_token да стигне дотук


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


def classify_potential(market_cap_usd: float, momentum_pct: float, volume_h1: float, liquidity_usd: float) -> str:
    """Едно изречение, ВИНАГИ започващо с изричен отговор от точно 4 нива -
    "ДА" / "ПО-СКОРО ДА" / "ПО-СКОРО НЕ" / "НЕ" - чисто спекулативна,
    евристична преценка, НЕ прогноза и НЕ финансов съвет. "НЕЯСНО" вече НЕ
    съществува като възможен отговор (изрично поискано от потребителя на
    17.09 - винаги трябва да накланя ясно в една посока).

    "Short squeeze" не съществува тук физически (graduated pump.fun монети
    се търгуват само на обикновен AMM, няма borrow/маржин механизъм за да се
    шортват - затова няма смисъл такава категория)."""
    ratio = (volume_h1 / liquidity_usd) if liquidity_usd else 0
    # Две нива на "колко комфортна е ликвидността" спрямо голия минимум
    # (MIN_LIQUIDITY_USD, вдигнат на $15k след сравнителния тест от 17.09 -
    # виж config.py) - монета точно на прага е технически преминала филтъра,
    # но не е "стабилна" в същия смисъл като една с 2-3x+ повече дълбочина.
    comfortable_liquidity = liquidity_usd >= config.MIN_LIQUIDITY_USD * 1.5
    very_comfortable_liquidity = liquidity_usd >= config.MIN_LIQUIDITY_USD * 3

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

    # --- ДА - най-силният профил: рано, реален моментум, висок обем, И дълбока ликвидност ---
    if is_early_and_hot and very_comfortable_liquidity:
        return ("🚀 Long runner: ДА (спекулативно) - нисък market cap + силен ранен моментум + висок обем спрямо "
                "ликвидност + необичайно дълбока ликвидна база, но повечето такива монети пак отиват на 0")
    if is_early_and_hot:
        return ("🚀 Long runner: ПО-СКОРО ДА (спекулативно) - нисък market cap + силен ранен моментум + висок обем "
                "спрямо ликвидност, ликвидността е стабилна (не необичайно дълбока), пак висок rug риск")
    if is_still_small and very_comfortable_liquidity:
        return "🌱 Long runner: ПО-СКОРО ДА - все още малка по market cap, ликвидността е необичайно дълбока за размера ѝ"
    if is_still_small:
        return "🌱 Long runner: ПО-СКОРО НЕ - все още малка по market cap, но профилът (моментум/обем) не е особено убедителен"

    # --- "Разширена зона" (150k - EXTENDED_MAX_MARKET_CAP_USD, по подразбиране
    # 500k) - ВАЖНО (18.09, реален случай: TIGRINO, 91ryaC...Lgpump - влязохме
    # на $170.5K MC, продадохме на $220.1K MC за +29%, но монетата продължи
    # ДО $1.5M MC (+52,081% от старта) - продадохме твърде рано, защото ниkъде
    # не пишеше "Long runner: ДА" на този mc диапазон, старата функция тук
    # връщаше директно "НЕ" над 150k. Ако монетата стигне дотук (score_token
    # вече изисква чист RugCheck доклад + дълбока ликвидност за да мине
    # изобщо score-ването отвъд 110k - виж config.EXTENDED_MAX_MARKET_CAP_USD),
    # значи вече е доказала, че не е бърз pump-and-dump - затова etikетираме
    # като потенциален траен растеж, не "изразходван моментум".
    if bool(market_cap_usd) and market_cap_usd <= config.EXTENDED_MAX_MARKET_CAP_USD:
        return (
            "🔥 Long runner: ДА (по-рядко, но виж TIGRINO 18.09) - над обичайния 'ранен' диапазон, но мина "
            "по-строгите проверки за 'разширена зона' (чист RugCheck доклад, дълбока ликвидност) - изглежда "
            "като траен растеж, не бърз pump-and-dump. Не продавай автоматично само защото mc вече не е малък."
        )
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
    if market_cap_usd and market_cap_usd > config.EXTENDED_MAX_MARKET_CAP_USD:
        return MemeScoreResult(
            mint=mint,
            score=0,
            reasons=[
                f"market cap твърде висок (${market_cap_usd:,.0f}) - над твърдия таван "
                f"${config.EXTENDED_MAX_MARKET_CAP_USD:,.0f}, никога не продължаваме да следим над това."
            ],
            liquidity_usd=liquidity_usd,
            market_cap_usd=market_cap_usd,
            raw={"pair": best_pair, "rugcheck": rugcheck_report},
        )

    if market_cap_usd and market_cap_usd > config.MAX_MARKET_CAP_USD:
        # "Разширена зона" (config.MAX_MARKET_CAP_USD до EXTENDED_MAX_MARKET_CAP_USD,
        # по подразбиране 110k-500k) - ВАЖНО (18.09, по изричен избор на
        # потребителя: "ако е свързано с нещо тренд ново... long runner
        # примерно от 30k mc до 500k mc"). Нямаме надежден начин да познаем
        # автоматично "нов тренд" по тема/име, затова вместо да гадаем,
        # изискваме монетата да е АБСОЛЮТНО чиста по обективни признаци, за
        # да продължи да се следи отвъд обичайния 110k праг:
        #   - RugCheck доклад НАЛИЧЕН и с НУЛА риск флага (не просто под
        #     'danger'/'high' - буквално чист доклад)
        #   - ликвидност над EXTENDED_ZONE_MIN_LIQUIDITY_USD (по-висок под от
        #     нормалния - по-голям market cap изисква пропорционално по-
        #     дълбока ликвидност, иначе е точно профилът на wash trading)
        # Волюм/ликвидност съотношението вече е проверено твърдо по-горе
        # (важи за всички market cap-ове, не само тук).
        # Ако дори едно от тези не е изпълнено - отхвърля се на обичайния
        # 110k таван, както преди тази промяна.
        rugcheck_clean = bool(rugcheck_report) and not (rugcheck_report.get("risks") or [])
        liquidity_deep_enough = liquidity_usd >= config.EXTENDED_ZONE_MIN_LIQUIDITY_USD
        if not (rugcheck_clean and liquidity_deep_enough):
            return MemeScoreResult(
                mint=mint,
                score=0,
                reasons=[
                    f"market cap ${market_cap_usd:,.0f} е над обичайния 'ранен' таван ${config.MAX_MARKET_CAP_USD:,.0f}, "
                    f"и не минава по-строгите изисквания за 'разширена зона' (чист RugCheck доклад: "
                    f"{'да' if rugcheck_clean else 'НЕ'}, ликвидност ≥${config.EXTENDED_ZONE_MIN_LIQUIDITY_USD:,.0f}: "
                    f"{'да' if liquidity_deep_enough else 'НЕ'}) - пропускам."
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

    liq_pts = _liquidity_points(liquidity_usd, market_cap_usd)
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
        potential_label=classify_potential(market_cap_usd, momentum_pct, volume_h1, liquidity_usd),
        raw={"pair": best_pair, "rugcheck": rugcheck_report},
    )
