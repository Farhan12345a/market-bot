"""
Sector relative strength: is this stock moving, or is its whole sector moving?

Why it matters here
-------------------
`relative_strength` compares a symbol to SPY, which answers "is this stronger
than the market". It cannot tell a stock running on its own news from one being
carried by its sector - and those are very different bets. On 2026-08-26 the
watchlist held MARA, RIOT, CLSK, CIFR and WULF simultaneously: five crypto
miners, which is one position in bitcoin held five times. Every one of them lost
money that session. Sector context is what makes that visible before the fact
rather than after.

REST only, on purpose
---------------------
The sector ETFs could be streamed, but the WebSocket budget is
stream_max_subscriptions (28) against a watchlist that already wants every slot,
and the subscription-counting question is still open (PENDING_WORK item 1-MON).
Daily and minute bars over REST cost no stream slots at all, and the factor
needs weeks of journal data before its weight matters - by which time the
subscription question will have been settled independently.

The map is deliberately small
-----------------------------
Alpaca does not expose sector classification, so this is a hand-maintained map
of the sectors this strategy actually trades. A symbol with no mapping gets a
sector factor of None, which continuation_score DROPS and renormalises around -
"not measurable" is not "measurably bad", and inventing a sector for an unknown
symbol would be worse than admitting ignorance.
"""

import logging

logger = logging.getLogger(__name__)

# symbol -> sector ETF. Grouped by the theme that actually moves them together,
# which is not always GICS: the miners track bitcoin, not "Financials".
SECTOR_ETF = {}

def _add(etf, symbols):
    for s in symbols.split():
        SECTOR_ETF[s] = etf

# Crypto miners and crypto-adjacent - these move with bitcoin, not with tech.
_add("WGMI", "MARA RIOT CLSK HUT CIFR WULF BITF BTBT IREN CORZ HIVE GREE CAN")
# Crypto exchanges/brokers track the same underlying but with equity beta.
_add("WGMI", "COIN HOOD BKKT MSTR")
# Semiconductors
_add("SMH", "NVDA AMD INTC MU AVGO QCOM TXN ADI MRVL ON SWKS MCHP LRCX AMAT KLAC ASML TSM ARM SMCI")
# Software / large-cap tech
_add("XLK", "MSFT AAPL ADBE CRM ORCL NOW INTU PANW SNOW DDOG NET CRWD ZS MDB TEAM WDAY ADSK")
# Consumer discretionary
_add("XLY", "AMZN TSLA HD MCD NKE SBUX LOW TJX BKNG ABNB DASH CMG LULU RCL CCL DKNG CVNA CHWY W ETSY GME AMC")
# Communication services / internet
_add("XLC", "GOOGL GOOG META NFLX DIS TMUS T VZ SNAP PINS RBLX U MTCH BMBL SPOT ROKU FUBO TTD")
# Financials
_add("XLF", "JPM BAC WFC GS MS C SCHW AXP BLK SPGI V MA PYPL SOFI AFRM UPST LC NU")
# Healthcare / biotech
_add("XLV", "JNJ UNH PFE ABBV MRK LLY TMO ABT DHR BMY AMGN GILD MRNA BIIB VRTX REGN")
# Energy
_add("XLE", "XOM CVX COP SLB EOG PSX VLO MPC OXY HAL DVN FANG")
# Industrials / transport
_add("XLI", "BA CAT DE UPS FDX LMT RTX HON GE MMM UBER LYFT")
# EV and clean energy
_add("XLY", "RIVN LCID NIO XPEV LI FSR")
_add("ICLN", "PLUG FCEL RUN ENPH SEDG FSLR NOVA")
# Quantum / speculative growth
_add("ARKK", "IONQ RGTI QBTS AI SOUN PLTR ASTS RKLB OPEN CART GRAB PATH")
# Cannabis
_add("MSOS", "TLRY CGC ACB CRON SNDL")

# Every ETF referenced above, for the caller that needs to fetch them.
SECTOR_ETFS = sorted(set(SECTOR_ETF.values()))


def sector_for(symbol):
    """The sector ETF for a symbol, or None when unmapped."""
    return SECTOR_ETF.get((symbol or "").upper())


def sectors_for(symbols):
    """The distinct sector ETFs needed to cover `symbols`."""
    out = {sector_for(s) for s in symbols or ()}
    out.discard(None)
    return sorted(out)


def sector_concentration(symbols):
    """
    {etf: [symbols]} for any sector holding more than one name.

    A watchlist is not diversified because it holds fifteen tickers. On
    2026-08-26 five of them were crypto miners, and a burst across those five is
    one bet held five times - exactly the correlation the burst throttle exists
    to limit, arriving through the watchlist instead of through a poll.
    """
    groups = {}
    for s in symbols or ():
        etf = sector_for(s)
        if etf:
            groups.setdefault(etf, []).append(s)
    return {k: v for k, v in sorted(groups.items()) if len(v) > 1}


def relative_to_sector(symbol_pct, sector_pct):
    """
    Excess return over the symbol's own sector, mapped to 0-100.

    Same scaling as continuation.relative_strength so the two sit on a
    comparable axis: 50 is "moving with its sector", above is leading it.

    The distinction this adds over relative_strength: a miner up 3% on a day the
    whole mining complex is up 3% has shown nothing about itself, and scores 50
    here while scoring highly against SPY.
    """
    if symbol_pct is None or sector_pct is None:
        return None
    excess = symbol_pct - sector_pct
    return max(0.0, min(100.0, 50 + excess * 25))


def sector_strength(symbol, symbol_pct, sector_returns):
    """
    The continuation factor. None when the symbol is unmapped or its sector
    return is unavailable - dropped and renormalised by continuation_score
    rather than scored zero.

    sector_returns: {etf: pct_change over the same window as symbol_pct}
    """
    etf = sector_for(symbol)
    if not etf:
        return None
    return relative_to_sector(symbol_pct, (sector_returns or {}).get(etf))
