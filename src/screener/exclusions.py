"""
Symbols this strategy should never trade, however well they score.

PENDING_WORK.md item 0d, third bullet - open since 2026-08-21 as a
theoretical concern, evidenced on 2026-09-01: SOXL and TQQQ were both on the
watchlist, and SOXL was the FIRST opening-burst entry of the day (39 shares
@ 106.50, +0.510% from the open), part of a 4-trade 0W/4L opening block.

Two separate reasons, and they are not the same argument:

1. AN INDEX FUND CANNOT BURST THE WAY A SINGLE NAME CAN. The whole entry
   thesis is a stock reacting to something specific to it - a catalyst that
   has not been fully priced in. A basket's move is the average of its
   holdings, so the thing being bet on is diluted by construction. It also
   defeats the sector cap and every correlation guard: SOXL is not "one
   name", it is the whole semiconductor complex, and holding it alongside
   INTC/NVDA/KLAC (all three watched on 2026-09-01) is the same bet counted
   as four.

2. LEVERAGE BREAKS EVERY PERCENTAGE THRESHOLD IN THE CONFIG. Every stop,
   tier and trigger here is a percent. A 3x fund moves ~3x the underlying,
   so a -0.5% first exit on SOXL is reached by roughly a -0.17% move in
   semis - noise, not a decision. The same distortion the sub-$10 price
   floor exists to prevent (a fixed cost that explodes as a percentage),
   arriving from the opposite direction: an amplified move that trips
   thresholds calibrated for unamplified ones. It also cuts the other way -
   min_move_pct and the burst floor are cleared trivially by a leveraged
   fund, so it will keep qualifying and keep being bought.

`max_stock_price: 300` already blocks QQQ at ~$711 BY ACCIDENT. An ETF at
$80 sails straight through, which is exactly how SOXL ($106) and TQQQ got in.

Deliberately a LIST plus a suffix heuristic, not a data lookup: Alpaca's
asset model does expose a class, but the distinction that matters here
("leveraged basket" vs "single name") is not one field, and a wrong network
call at 09:05 is worse than a maintained list. Unknown symbols are KEPT -
same rule as every other filter in this codebase, absence of evidence is not
evidence.
"""

# Leveraged and inverse ETFs/ETNs. Not exhaustive and not meant to be - it
# covers what this bot's universe actually surfaces. Add as they appear.
LEVERAGED_ETFS = {
    # Broad index, leveraged long
    "TQQQ", "QLD", "UPRO", "SSO", "UDOW", "DDM", "SPXL", "TNA", "URTY", "MIDU",
    # Broad index, inverse / leveraged short
    "SQQQ", "QID", "PSQ", "SPXS", "SPXU", "SDS", "SH", "SDOW", "DOG",
    "TZA", "SRTY", "RWM",
    # Sector, leveraged
    "SOXL", "SOXS", "TECL", "TECS", "WEBL", "WEBS", "FAS", "FAZ",
    "LABU", "LABD", "CURE", "DRN", "DRV", "ERX", "ERY", "NAIL", "PILL",
    "RETL", "UTSL", "DPST", "WANT", "HIBL", "HIBS",
    # Commodity / miners / single-country, leveraged
    "NUGT", "DUST", "JNUG", "JDST", "BOIL", "KOLD", "UCO", "SCO", "UGL",
    "GLL", "AGQ", "ZSL", "YINN", "YANG", "KORU", "EDC", "EDZ", "BRZU",
    # Rates
    "TMF", "TMV", "TYD", "TYO", "UBT", "TBT", "TBF",
    # Volatility - these are their own category of dangerous
    "UVXY", "SVXY", "VXX", "VIXY", "UVIX", "SVIX",
    # Single-stock leveraged (a growing category, all of it unsuitable)
    "TSLL", "TSLQ", "TSLS", "NVDL", "NVDS", "NVDU", "NVD", "AAPU", "AAPD",
    "MSFU", "MSFD", "AMZU", "AMZD", "GGLL", "GGLS", "METU", "METD",
    "MSTU", "MSTZ", "MSTX", "CONL", "AMDL", "AMUU", "PLTU",
}

# Unleveraged but still baskets: an index or sector fund is not a single-name
# catalyst trade. Kept SEPARATE from the leveraged set because the argument
# is only reason 1, not reason 2 - a config may reasonably want to exclude
# leveraged funds while still allowing a plain sector ETF.
BASKET_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IVV", "RSP", "MDY",
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLC", "XLRE",
    "SMH", "SOXX", "IGV", "XBI", "IBB", "ARKK", "ARKG", "ARKW", "ARKQ", "ARKF",
    "KRE", "XOP", "OIH", "GDX", "GDXJ", "XME", "XRT", "ITB", "JETS", "TAN",
    "WGMI", "BITO", "BITQ", "HACK", "BOTZ", "ROBO", "LIT", "URA", "COPX",
    "EEM", "EFA", "FXI", "KWEB", "EWZ", "EWJ", "INDA", "TLT", "IEF", "SHY",
    "HYG", "LQD", "GLD", "SLV", "USO", "UNG", "DBC",
}


def is_excluded(symbol, config):
    """
    (True, reason) if this symbol must not be traded, else (False, None).

    `config` is the trading config block. Three independent switches, checked
    cheapest-first:

      exclude_symbols          explicit list, always honoured
      exclude_leveraged_etfs   the LEVERAGED_ETFS set + a suffix heuristic
      exclude_basket_etfs      the BASKET_ETFS set

    Note the benchmark ETFs (SPY and the sector funds) live in BASKET_ETFS
    and are ALSO excluded by it - which is correct and already how they are
    treated: _benchmark_symbols subscribes them for measurement and they were
    never tradeable candidates. Excluding them here just makes that explicit
    rather than incidental.
    """
    if not symbol:
        return False, None
    sym = symbol.upper().strip()
    cfg = config or {}

    explicit = {s.upper() for s in (cfg.get("exclude_symbols") or [])}
    if sym in explicit:
        return True, "in exclude_symbols"

    if cfg.get("exclude_leveraged_etfs", True):
        if sym in LEVERAGED_ETFS:
            return True, "leveraged/inverse ETF - every % threshold here is calibrated for unamplified moves"

    if cfg.get("exclude_basket_etfs", True):
        if sym in BASKET_ETFS:
            return True, "index/sector basket - the entry thesis is a single-name catalyst"

    return False, None


def filter_symbols(symbols, config):
    """
    (kept, [(symbol, reason), ...]) for a list of candidates.

    Never empties the list to nothing: if EVERY candidate is excluded, the
    original list is returned untouched, mirroring _filter_watchlist_by_price's
    same guard. Watching nothing guarantees a blank day, and a filter that
    can silently produce one is more dangerous than the thing it filters.
    """
    kept, dropped = [], []
    for sym in symbols:
        excluded, reason = is_excluded(sym, config)
        if excluded:
            dropped.append((sym, reason))
        else:
            kept.append(sym)
    if not kept:
        return list(symbols), []
    return kept, dropped
