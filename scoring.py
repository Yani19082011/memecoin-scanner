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
    if ratio >= 2:
        return WEIGHTS["volume"]
    if ratio >= 1:
        return WEIGHTS["volume"] * 0.6
    if ratio >= 0.3:
        return WEIGHTS["volume"] * 0.3
    return 0


def _momentum_points(momentum_pct: float) -> float:
    """momentum_pct = реално наблюдавана % промяна в цената откакто следим
    монетата (не DexScreener-ското h1/h24, което за минутна монета е шум)."""
    if momentum_pct >= 100:
        return WEIGHTS["momentum"]
    if momentum_pct >= 50:
        return WEIGHTS["momentum"] * 0.7
    if momentum_pct >= 20:
        return WEIGHTS["momentum"] * 0.4
    if momentum_pct >= 5:
        return WEIGHTS["momentum"] * 0.15
    return 0


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
    """Едно изречение, ВИНАГИ започващо с изричен "long runner: ДА/НЕ/НЕЯСНО"
    отговор - чисто спекулативна, евристична преценка, НЕ прогноза и НЕ
    финансов съвет. "Short squeeze" не съществува тук физически (graduated
    pump.fun монети се търгуват само на обикновен AMM, няма borrow/маржин
    механизъм за да се шортват - затова няма смисъл такава категория)."""
    ratio = (volume_h1 / liquidity_usd) if liquidity_usd else 0

    if market_cap_usd and market_cap_usd < 50_000 and momentum_pct >= 30 and ratio >= 1:
        return ("🚀 Long runner: ДА (спекулативно) - нисък market cap + силен ранен моментум + висок обем спрямо "
                "ликвидност, но повечето такива монети пак отиват на 0")
    if momentum_pct >= 100:
        return "⚠️ Long runner: НЕ - вече силно изпомпана, влизаш късно с повишен риск точно сега да е dump"
    if market_cap_usd and market_cap_usd < 100_000:
        return "🌱 Long runner: НЕЯСНО - все още малка по market cap с потенциал, но и стандартно висок rug риск"
    return "➖ Long runner: НЕ - умерен/неясен потенциал по наличните данни"


def score_token(mint: str, best_pair: dict, rugcheck_report: dict, momentum_pct: float = 0.0) -> MemeScoreResult:
    if not best_pair:
        return MemeScoreResult(mint=mint, score=0, reasons=["няма DexScreener pair - вероятно още не е индексиран"])

    liquidity_usd = (best_pair.get("liquidity") or {}).get("usd", 0) or 0
    market_cap_usd = best_pair.get("marketCap") or best_pair.get("fdv") or 0

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

    # Горен таван на market cap - целта е да хващаме монети РАНО, докато са
    # все още малки, не след като вече са набъбнали значително. market_cap_usd
    # може да е 0/непознат за съвсем нови монети (DexScreener още не го е
    # изчислил) - в такъв случай НЕ филтрираме тук (нямаме основание да
    # отхвърлим заради непозната стойност), но филтрираме твърдо, ако имаме
    # реална стойност над прага.
    if market_cap_usd and market_cap_usd > config.MAX_MARKET_CAP_USD:
        return MemeScoreResult(
            mint=mint,
            score=0,
            reasons=[f"market cap твърде висок (${market_cap_usd:,.0f}) - над лимита ${config.MAX_MARKET_CAP_USD:,.0f}, вече не е 'ранно' влизане"],
            liquidity_usd=liquidity_usd,
            market_cap_usd=market_cap_usd,
            raw={"pair": best_pair, "rugcheck": rugcheck_report},
        )

    reasons = []
    points = 0.0

    volume_h1 = (best_pair.get("volume") or {}).get("h1", 0) or 0

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

        if not any("mint" in n and "authority" in n for n in risk_names):
            points += WEIGHTS["no_mint_authority"]
            reasons.append("mint authority изглежда revoke-нат")
        else:
            reasons.append("⚠️ mint authority все още активен (може да се printne още токени)")

        if not any("freeze" in n for n in risk_names):
            points += WEIGHTS["no_freeze_authority"]
        else:
            reasons.append("⚠️ freeze authority активен")

        high_severity = [r for r in risks if isinstance(r, dict) and str(r.get("level", "")).lower() in ("danger", "high")]
        if len(high_severity) == 0:
            points += WEIGHTS["low_risk_flags"]
        else:
            reasons.append(f"⚠️ {len(high_severity)} high-severity риск флага от RugCheck")

    return MemeScoreResult(
        mint=mint,
        score=round(points, 1),
        reasons=reasons,
        liquidity_usd=liquidity_usd,
        market_cap_usd=market_cap_usd,
        potential_label=classify_potential(market_cap_usd, momentum_pct, volume_h1, liquidity_usd),
        raw={"pair": best_pair, "rugcheck": rugcheck_report},
    )
