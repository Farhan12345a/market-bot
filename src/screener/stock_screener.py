import bisect
import pandas as pd
import logging
from datetime import datetime, timedelta
import pytz
import yfinance as yf
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)

# Below this many measurable candidates, a "percentile" is a rank among
# almost nothing - fall back to the fixed ATR bands instead of pretending a
# 3-name distribution has quartiles.
MIN_CANDIDATES_FOR_TRUE_PERCENTILE = 5

class StockScreener:
    """Daily pre-market screener to identify high-volatility candidates"""

    def __init__(self, broker, candidates_file="candidates.txt", extra_candidates=(), config=None):
        self.broker = broker
        # Trading config, for the tunables the newer scoring components read.
        # Defaults to {} so the screener stays constructible standalone.
        self.config = (config or {}).get("trading", config or {})
        self.candidates = self._load_candidates(candidates_file, extra_candidates)
        self.et = pytz.timezone("America/New_York")
        self.last_scores = {}
        self.last_details = {}

    def _load_candidates(self, file, extra=()) -> List[str]:
        """
        Candidate pool: candidates_file, plus `extra`.

        `extra` carries stock_universe when merge_default_universe is off. Those
        names then COMPETE for a place on score instead of being appended to the
        watchlist unconditionally - which is the whole point of turning the merge
        off. Skipping this step would silently drop 50 perfectly good candidates
        from consideration rather than promoting them to earn their spot.
        """
        symbols = []
        try:
            with open(file) as f:
                symbols = [line.strip().upper() for line in f if line.strip()]
        except FileNotFoundError:
            logger.error(f"Candidates file {file} not found")

        extra = [str(x).strip().upper() for x in (extra or []) if str(x).strip()]
        pool = list(dict.fromkeys(symbols + extra))

        if extra:
            added = len(pool) - len(dict.fromkeys(symbols))
            logger.info(
                f"Loaded {len(pool)} candidate symbols "
                f"({len(dict.fromkeys(symbols))} from {file} + {added} new from stock_universe)"
            )
        else:
            logger.info(f"Loaded {len(pool)} candidate symbols")
        return pool

    def _get_recent_gap(self, symbol) -> float:
        """
        Gap from yesterday's close to where the stock is trading NOW (%).

        Deliberately measured against the current/pre-market price rather than
        today's official opening print. The screener runs at 09:05 ET (see
        screener_start_time), and today's DAILY bar does not exist yet at that
        hour - so the previous implementation, which required two daily bars
        and returned 0 when it could not get both, scored a 0.0% gap for every
        single symbol. On 2026-08-20 that made MRVL, CADL and CMG tie at an
        identical 44.0 and cut the selection to 3 names; the accidental
        post-open re-run the same morning scored the same universe 79.0 / 66.7
        / 58.1 with real gaps (COIN 7.8%, HOOD 5.3%), which is what the
        component is worth when it works.

        Measuring against the live price is also the better signal: it is
        where the stock is actually changing hands right now, not one opening
        print, and it is available before the bell when the decision is made.

        Falls back to the old today's-open path when there is no current
        price (thin or absent pre-market prints on the free IEX feed), so a
        post-open run behaves exactly as before.
        """
        try:
            today = datetime.now(self.et).date()
            start = today - timedelta(days=7)  # buffer for weekends/holidays

            bars = self.broker.get_historical_bars(
                symbol, start, today + timedelta(days=1), "1Day"
            )
            if symbol not in bars or bars[symbol].empty:
                return 0.0

            df = bars[symbol].sort_values("timestamp")

            # Rows dated today are today's own (partial) daily bar - exclude
            # them so "yesterday's close" is genuinely the prior session.
            df["_date"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(self.et).dt.date
            prior = df[df["_date"] < today]
            if prior.empty:
                return 0.0
            yesterday_close = float(prior.iloc[-1]["close"])
            if yesterday_close <= 0:
                return 0.0

            current = self._get_current_price(symbol)
            if not current:
                # No live print - fall back to today's official open if the
                # daily bar has appeared (i.e. we are running after the bell).
                today_rows = df[df["_date"] == today]
                if today_rows.empty:
                    return 0.0
                current = float(today_rows.iloc[-1]["open"])

            return abs((current - yesterday_close) / yesterday_close * 100)

        except Exception as e:
            logger.debug(f"Error calculating gap for {symbol}: {e}")
            return 0.0

    def _get_current_price(self, symbol) -> float:
        """
        Latest traded price, including pre-market. Returns 0 when unavailable.

        Tries the most recent 1-minute bar first (covers extended hours on the
        IEX feed) and falls back to the latest quote midpoint. Both are
        best-effort: a thinly traded name may have neither before the bell,
        which the caller handles.
        """
        try:
            now = datetime.now(self.et)
            bars = self.broker.get_historical_bars(
                symbol, now - timedelta(hours=12), now, "1Min"
            )
            if symbol in bars and not bars[symbol].empty:
                price = float(bars[symbol].sort_values("timestamp").iloc[-1]["close"])
                if price > 0:
                    return price
        except Exception as e:
            logger.debug(f"No recent minute bar for {symbol}: {e}")

        try:
            quote = self.broker.get_latest_quote(symbol)
            if quote and quote.get("bid") and quote.get("ask"):
                return (float(quote["bid"]) + float(quote["ask"])) / 2
        except Exception as e:
            logger.debug(f"No quote for {symbol}: {e}")

        return 0.0

    def _get_5day_return(self, symbol) -> float:
        """Calculate 5-day return (%)"""
        try:
            end = datetime.now(self.et).date()
            start = end - timedelta(days=7)

            bars = self.broker.get_historical_bars(symbol, start, end, "1Day")

            if symbol not in bars or len(bars[symbol]) < 2:
                return 0

            df = bars[symbol].sort_values("timestamp")
            close_5d_ago = df.iloc[0]["close"]
            latest_close = df.iloc[-1]["close"]

            return ((latest_close - close_5d_ago) / close_5d_ago * 100)

        except Exception as e:
            logger.debug(f"Error calculating 5-day return for {symbol}: {e}")
            return 0

    def _get_volume_ratio(self, symbol) -> float:
        """
        Compare today's volume-so-far to the average volume accumulated by
        this same time of day over the past 10 trading days.

        The same-time-of-day comparison is what makes this work at any hour,
        including pre-market: screen() now runs before the open (see
        screener_start_time), so "today so far" is today's pre-market volume
        and each prior day is measured to the same pre-market cutoff. Heavy
        pre-market volume relative to the same stock's own norm is one of the
        few genuinely forward-looking signals available before the bell.

        Returns a neutral 1.0 when there isn't enough data to judge - thin or
        absent pre-market prints on the free IEX feed are common for less
        liquid names, and a missing measurement should not be scored as a
        volume surge.
        """
        try:
            now = datetime.now(self.et)
            today = now.date()
            cutoff_time = now.time()

            lookback_days = 10
            start = today - timedelta(days=lookback_days * 2)  # buffer for weekends/holidays

            # `end` must be a DATETIME, not a date. A bare date is coerced to
            # midnight, so passing `today` asked for bars strictly BEFORE
            # today - today's own bars were never returned, today_vol was
            # always 0, and the function returned its neutral 1.0 fallback
            # every single time. That is why every candidate scored an
            # identical "Vol Ratio: 1.00x" on 2026-08-19: volume was
            # contributing nothing to the ranking at all.
            bars = self.broker.get_historical_bars(symbol, start, now, "5Min")

            if symbol not in bars or bars[symbol].empty:
                return 1.0

            df = bars[symbol].copy()
            df["ts_et"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(self.et)
            df["date"] = df["ts_et"].dt.date
            df["time"] = df["ts_et"].dt.time

            today_vol = df.loc[(df["date"] == today) & (df["time"] <= cutoff_time), "volume"].sum()
            if today_vol == 0:
                return 1.0

            prior_days = sorted(d for d in df["date"].unique() if d < today)[-lookback_days:]

            prior_sums = []
            for d in prior_days:
                day_vol = df.loc[(df["date"] == d) & (df["time"] <= cutoff_time), "volume"].sum()
                if day_vol > 0:
                    prior_sums.append(day_vol)

            if not prior_sums:
                return 1.0

            avg_prior = sum(prior_sums) / len(prior_sums)
            return today_vol / avg_prior if avg_prior > 0 else 1.0

        except Exception as e:
            logger.debug(f"Error calculating volume ratio for {symbol}: {e}")
            return 1.0

    def _get_volatility_percentile(self, symbol) -> float:
        """
        This stock's volatility as a 0-100 figure. Uses ATR (Average True
        Range) as the volatility metric.

        NOTE ON WHAT THIS RETURNS. Per symbol, in isolation, the only honest
        answer is a BAND - a percentile needs a distribution, and this call
        sees one stock. So this returns the fixed-band value below, and
        records the raw atr_pct in self._atr_pct for screen() to convert into
        a TRUE percentile across the day's candidate set once every candidate
        has been measured (see _rank_volatility_percentiles).

        That two-step exists because the bands alone were the whole story
        until 2026-09-02, and they discriminate far less than the name
        "percentile" suggests: five hardcoded outputs (10/30/50/75/95) meant
        three earnings candidates on 2026-08-21 all scored an identical 26.2
        on the heaviest-weighted term, so the ranking was effectively decided
        by the gap term alone (PENDING_WORK.md item 0d).
        """
        try:
            end = datetime.now(self.et).date()
            start = end - timedelta(days=30)

            bars = self.broker.get_historical_bars(symbol, start, end, "1Day")

            if symbol not in bars or len(bars[symbol]) < 2:
                return 50

            df = bars[symbol].sort_values("timestamp")

            # Calculate ATR (simplified: average of true ranges)
            df["tr"] = pd.concat([
                df["high"] - df["low"],
                abs(df["high"] - df["close"].shift()),
                abs(df["low"] - df["close"].shift())
            ], axis=1).max(axis=1)

            atr = df["tr"].tail(14).mean()
            atr_pct = (atr / df.iloc[-1]["close"]) * 100

            # The real number, kept rather than thrown away. screen() ranks
            # these against each other afterwards; without this the actual
            # ATR% was computed here and then immediately discarded into one
            # of five buckets.
            if not hasattr(self, "_atr_pct"):
                self._atr_pct = {}
            self._atr_pct[symbol] = float(atr_pct)

            # Fallback bands, used when there aren't enough candidates to rank
            # against (and by any caller that scores a single symbol).
            if atr_pct < 0.5:
                return 10
            elif atr_pct < 1.0:
                return 30
            elif atr_pct < 1.5:
                return 50
            elif atr_pct < 2.5:
                return 75
            else:
                return 95

        except Exception as e:
            logger.debug(f"Error calculating volatility for {symbol}: {e}")
            return 50

    def get_opening_move_stats(self, symbol):
        """
        How this symbol has actually behaved in the first N minutes of recent
        sessions - the single most direct measure of "does this name do the
        thing my entry logic is looking for".

        Returns a dict:
            sessions     how many past opens were measurable
            hit_rate     fraction of them that reached opening_move_target_pct
            avg_max_gain mean best gain from the opening price, in %
            best         largest single-session gain seen

        Everything else in score_stock is a PROXY for this - gap says a catalyst
        exists, volatility says the name can move, volume says people are
        present. This measures the outcome those proxies are trying to predict.

        One request per symbol covers the whole lookback, because the minute
        bars for several days come back together. Bars are grouped by ET
        calendar date and each session is measured from its own 09:30 open, so a
        holiday or half day simply contributes fewer sessions rather than
        corrupting the average.

        CAUTION, and it is the reason this is configurable and separately
        weighted: "patterns repeat" is a hypothesis, not a fact. Five
        observations per symbol is thin, and a name that popped four mornings
        running may reflect a hot sector that week rather than anything durable.
        Gap looked compelling on the same reasoning and turned out to be
        NEGATIVE at the tail (MRVL: largest gap of 2026-08-19, second-worst
        symbol). The stats are logged to the signal journal whether or not they
        are scored, so the claim can be tested against forward returns rather
        than assumed.
        """
        empty = {"sessions": 0, "hit_rate": 0.0, "avg_max_gain": 0.0, "best": 0.0,
                 "opening_efficiency": None, "opening_directional": None,
                 "opening_eff_sessions": 0}
        try:
            lookback = self.config.get("opening_move_lookback_days", 5)
            window = self.config.get("opening_move_window_minutes", 30)
            target = self.config.get("opening_move_target_pct", 1.0)

            end = datetime.now(self.et).date()
            # Calendar days, widened for weekends/holidays so `lookback`
            # TRADING sessions actually come back.
            start = end - timedelta(days=max(lookback * 2 + 5, 10))

            bars = self.broker.get_historical_bars(symbol, start, end, "1Min")
            if symbol not in bars or bars[symbol].empty:
                return empty

            df = bars[symbol].copy().sort_values("timestamp")
            ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df = df[ts.notna()]
            if df.empty:
                return empty
            df["_et"] = ts[ts.notna()].dt.tz_convert(self.et)
            df["_date"] = df["_et"].dt.date
            df["_mins"] = df["_et"].dt.hour * 60 + df["_et"].dt.minute

            open_min = 9 * 60 + 30
            eff_window = self.config.get("opening_efficiency_minutes", 5)
            gains, effs, dcs = [], [], []
            for day, chunk in df.groupby("_date"):
                # Regular session only: pre- and post-market prints would
                # otherwise supply the "opening" price.
                sess = chunk[(chunk["_mins"] >= open_min) &
                             (chunk["_mins"] < open_min + window)]
                if sess.empty:
                    continue
                first = float(sess.iloc[0]["open"])
                if first <= 0:
                    continue
                peak = float(sess["high"].max())
                gains.append((peak - first) / first * 100)

                # Opening EFFICIENCY: how much of the first few minutes' total
                # travel went toward the net move, rather than being retraced.
                #
                #     efficiency = |P_end - P_start| / sum(|each 1-min step|)
                #
                # 100 -> 101 -> 102 -> 103 -> 104 scores 1.0. The same net move
                # via 100 -> 105 -> 101 -> 104 -> 99 -> 105 scores 0.22. Both
                # end +5%; only one is tradeable by a strategy that buys a
                # direction and holds it.
                #
                # Distinct from hit_rate above, which asks only whether the
                # symbol reached a target - a name that zig-zags to +1% counts
                # as a hit while being exactly the shape this strategy loses on.
                early = sess[sess["_mins"] < open_min + eff_window]
                closes = [float(x) for x in early["close"].tolist()]
                if len(closes) >= 3:
                    steps = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
                    travel = sum(abs(x) for x in steps)
                    if travel > 0:
                        effs.append(abs(closes[-1] - closes[0]) / travel)
                        # Directional consistency: share of 1-min steps that went
                        # the same way as the net move.
                        net = closes[-1] - closes[0]
                        if net != 0:
                            agree = sum(1 for x in steps if (x > 0) == (net > 0))
                            dcs.append(agree / len(steps))

            gains = gains[-lookback:]          # most recent N sessions
            if not gains:
                return empty

            effs, dcs = effs[-lookback:], dcs[-lookback:]
            hits = sum(1 for g in gains if g >= target)
            return {
                "sessions": len(gains),
                "hit_rate": hits / len(gains),
                "avg_max_gain": sum(gains) / len(gains),
                "best": max(gains),
                # Recorded, never scored. Same discipline every factor got: it
                # goes to the journal first and only earns a weight once the
                # forward returns say it predicts something.
                "opening_efficiency": (sum(effs) / len(effs)) if effs else None,
                "opening_directional": (sum(dcs) / len(dcs)) if dcs else None,
                "opening_eff_sessions": len(effs),
            }
        except Exception as e:
            logger.debug(f"Opening-move stats failed for {symbol}: {e}")
            return empty

    def _has_earnings_nearby(self, symbol) -> bool:
        """Check if stock has earnings in the next 5 days"""
        try:
            # Use yfinance to get earnings dates
            ticker = yf.Ticker(symbol)
            calendar = ticker.quarterly_financials

            if calendar is None or calendar.empty:
                return False

            # This is a simplified check - yfinance's earnings data can be delayed
            return True  # Conservative: assume we can't reliably get earnings

        except Exception as e:
            logger.debug(f"Error checking earnings for {symbol}: {e}")
            return False

    def _get_price(self, symbol) -> float:
        """Get latest closing price"""
        try:
            end = datetime.now(self.et).date()
            start = end - timedelta(days=1)

            bars = self.broker.get_historical_bars(symbol, start, end, "1Day")

            if symbol not in bars or bars[symbol].empty:
                return 0

            df = bars[symbol].sort_values("timestamp")
            return float(df.iloc[-1]["close"])

        except Exception as e:
            logger.debug(f"Error getting price for {symbol}: {e}")
            return 0

    def score_stock(self, symbol) -> Tuple[float, Dict]:
        """
        Score a stock 0-100 for likelihood of volatility in first 30 min.
        Returns (score, details_dict)
        """
        score = 0
        details = {"symbol": symbol}

        try:
            # Recent gap (25 points max) - strong predictor of open volatility
            gap = self._get_recent_gap(symbol)
            gap_score = min(25, gap * 5)  # 5% gap = 25 points
            score += gap_score
            details["gap_pct"] = gap
            details["gap_score"] = gap_score

            # Momentum (25 points max) - continuation likely
            momentum = self._get_5day_return(symbol)
            if momentum > 5:
                momentum_score = 25
            elif momentum > 2:
                momentum_score = 15
            elif momentum > 0:
                momentum_score = 10
            else:
                momentum_score = max(0, 10 + momentum * 2)  # Penalize losses

            score += momentum_score
            details["5day_return_pct"] = momentum
            details["momentum_score"] = momentum_score

            # Volume surge (20 points max)
            vol_ratio = self._get_volume_ratio(symbol)
            if vol_ratio > 1.8:
                vol_score = 20
            elif vol_ratio > 1.5:
                vol_score = 15
            elif vol_ratio > 1.2:
                vol_score = 10
            else:
                vol_score = 0

            score += vol_score
            details["volume_ratio"] = vol_ratio
            details["volume_score"] = vol_score

            # Volatility rank (20 points max)
            vol_percentile = self._get_volatility_percentile(symbol)
            vol_rank_score = vol_percentile * 0.2
            score += vol_rank_score
            details["volatility_percentile"] = vol_percentile
            details["volatility_score"] = vol_rank_score
            # The raw measurement behind that band, so screen() can rank it
            # against the rest of today's candidates. None when the ATR call
            # failed - "not measurable" stays distinct from "not volatile".
            details["atr_pct"] = getattr(self, "_atr_pct", {}).get(symbol)

            # Opening-move history (configurable points, default 15).
            # Off by default in code; config turns it on. Scored SEPARATELY from
            # the four proxies above rather than replacing any of them, so its
            # contribution is measurable in isolation - and so it can be zeroed
            # without disturbing the rest if the pattern does not hold up.
            om = self.get_opening_move_stats(symbol)
            details["opening_sessions"] = om["sessions"]
            details["opening_hit_rate"] = om["hit_rate"]
            details["opening_avg_gain"] = om["avg_max_gain"]
            details["opening_best"] = om["best"]
            details["opening_efficiency"] = om.get("opening_efficiency")
            details["opening_directional"] = om.get("opening_directional")

            opening_score = 0
            if self.config.get("use_opening_move_score", False) and om["sessions"]:
                max_pts = self.config.get("opening_move_points", 15)
                # Hit rate carries two thirds, average size one third. Frequency
                # matters more than magnitude here: the strategy needs the move
                # to HAPPEN inside a 20-minute entry window, and one huge day
                # among four flat ones is not a tradeable pattern.
                target = self.config.get("opening_move_target_pct", 1.0)
                size_component = min(1.0, om["avg_max_gain"] / target) if target else 0
                opening_score = max_pts * (om["hit_rate"] * 0.67 + size_component * 0.33)

                # Thin evidence is discounted rather than trusted: with fewer
                # sessions than asked for, the estimate is noisier and gets
                # proportionally less weight.
                wanted = self.config.get("opening_move_lookback_days", 5)
                if om["sessions"] < wanted:
                    opening_score *= om["sessions"] / wanted

            score += opening_score
            details["opening_score"] = opening_score

            # Price check (filter only)
            price = self._get_price(symbol)
            details["price"] = price

            details["score"] = score
            return score, details

        except Exception as e:
            logger.error(f"Error scoring {symbol}: {e}")
            return 0, details

    def _rank_volatility_percentiles(self, scores, details_dict):
        """
        Turn the volatility term into a real percentile across today's
        candidates, in place, after every candidate has been scored.

        Fixes PENDING_WORK.md item 0d. The term is worth up to 20 points -
        joint-heaviest in score_stock - and until now it took one of five
        values, so it separated candidates into five piles rather than
        ranking them. On 2026-08-21 three earnings names landed on an
        IDENTICAL 26.2 for this term, which meant the gap term silently
        decided the whole ranking. The ATR% behind those bands was already
        being computed and thrown away.

        Each symbol's score is adjusted by the DIFFERENCE between its band
        score and its percentile score, so nothing else in the scoring is
        disturbed. Symbols whose ATR could not be measured keep their band
        value untouched - "not measurable" is not "not volatile", the same
        rule the continuation score uses for missing factors.

        Ranked across every scored candidate, including ones the price band
        drops afterwards: the question this answers is "how volatile is this
        name relative to what was available today", and that set is the
        honest denominator.
        """
        measured = {
            sym: (details_dict.get(sym) or {}).get("atr_pct")
            for sym in scores
        }
        measured = {s: a for s, a in measured.items() if a is not None}
        if len(measured) < MIN_CANDIDATES_FOR_TRUE_PERCENTILE:
            if measured:
                logger.info(
                    f"Volatility percentile: only {len(measured)} candidate(s) had a "
                    f"measurable ATR (need {MIN_CANDIDATES_FOR_TRUE_PERCENTILE}) - "
                    f"keeping the fixed ATR bands for this run"
                )
            return

        ordered = sorted(measured.values())
        n = len(ordered)
        for sym, atr in measured.items():
            # Share of the candidate set at or below this ATR, 0-100. Ties get
            # the same rank because bisect_right counts all equal values.
            pctile = 100.0 * bisect.bisect_right(ordered, atr) / n
            detail = details_dict.get(sym) or {}
            old_score = detail.get("volatility_score") or 0.0
            new_score = pctile * 0.2
            scores[sym] = scores[sym] - old_score + new_score
            detail["volatility_percentile"] = round(pctile, 1)
            detail["volatility_score"] = new_score
            detail["volatility_ranked"] = True

        spread = ordered[-1] - ordered[0]
        logger.info(
            f"Volatility percentile: ranked {n} candidates by true ATR% "
            f"(min {ordered[0]:.2f}%, median {ordered[n // 2]:.2f}%, "
            f"max {ordered[-1]:.2f}%, spread {spread:.2f}pp) - "
            f"replaces the five fixed bands"
        )

    def screen(self, top_n: int = 25, min_score: float = 40) -> List[str]:
        """
        Run daily screen on all candidates.
        Returns top N stocks by score that meet minimum threshold.
        """
        if not self.candidates:
            logger.error("No candidates loaded")
            return []

        logger.info(f"Screening {len(self.candidates)} stocks...")

        scores = {}
        details_dict = {}

        for i, symbol in enumerate(self.candidates):
            if i % 20 == 0:
                logger.info(f"  Progress: {i}/{len(self.candidates)}")

            try:
                score, details = self.score_stock(symbol)
                scores[symbol] = score
                details_dict[symbol] = details
            except Exception as e:
                logger.warning(f"Error screening {symbol}: {e}")
                continue

        # Drop symbols this strategy must never trade BEFORE ranking, for the
        # same reason the price band is applied here rather than at entry: a
        # symbol that can never be bought should not occupy a top-N place, a
        # stream subscription, or a poll cycle. On 2026-09-01 SOXL (a 3x
        # leveraged semis ETF) was the first opening-burst entry of the day.
        from src.screener.exclusions import is_excluded
        excluded_out = []
        for sym in list(scores):
            excluded, reason = is_excluded(sym, self.config)
            if excluded:
                excluded_out.append(f"{sym} ({reason})")
                scores.pop(sym, None)
        if excluded_out:
            logger.info(
                f"Excluded {len(excluded_out)} untradeable symbol(s) before ranking: "
                + ", ".join(excluded_out)
            )

        # Replace the fixed ATR bands with a TRUE percentile across today's
        # candidate set, now that every candidate has been measured.
        self._rank_volatility_percentiles(scores, details_dict)

        # Sort by score
        sorted_stocks = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        # Kept so callers can order by merit rather than by list position -
        # notably which symbols get the scarce WebSocket slots.
        self.last_scores = dict(sorted_stocks)
        self.last_details = details_dict

        # Drop out-of-band prices BEFORE ranking, so they can never occupy a
        # top-N slot, a stream subscription, or a poll cycle. On 2026-08-24 AMC
        # signalled ten times at ~$2.70 and was refused ten times, and each of
        # those refusals still counted toward the burst width that throttles
        # genuine entries. A symbol the bot can never buy should not be
        # considered for anything.
        min_price = self.config.get("min_stock_price") or 0
        max_price = self.config.get("max_stock_price") or 0
        if min_price or max_price:
            priced_out = []
            for sym in list(scores):
                px = (details_dict.get(sym) or {}).get("price") or 0
                if not px:
                    continue  # unknown price is not evidence of a bad one
                if (min_price and px < min_price) or (max_price and px > max_price):
                    priced_out.append(f"{sym} (${px:.2f})")
                    scores.pop(sym, None)
            if priced_out:
                logger.info(
                    f"Excluded {len(priced_out)} symbol(s) outside "
                    f"${min_price}-${max_price} before ranking: {', '.join(priced_out)}"
                )
            sorted_stocks = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            self.last_scores = dict(sorted_stocks)

        # Filter by minimum score and take top N
        selected = [
            sym for sym, score in sorted_stocks
            if score >= min_score
        ][:top_n]

        # Log results
        logger.info(f"=" * 60)
        logger.info(f"STOCK SCREENER RESULTS - {datetime.now(self.et).strftime('%Y-%m-%d')}")
        logger.info(f"=" * 60)

        for symbol in selected:
            details = details_dict[symbol]
            logger.info(
                f"{symbol:6} | Score: {scores[symbol]:6.1f} | "
                f"Gap: {details['gap_pct']:5.1f}% | "
                f"5d Return: {details['5day_return_pct']:6.1f}% | "
                f"Vol Ratio: {details['volume_ratio']:4.2f}x | "
                f"Open30: {details.get('opening_hit_rate', 0) * 100:3.0f}% hit / "
                f"{details.get('opening_avg_gain', 0):+4.2f}% avg "
                f"({details.get('opening_sessions', 0)}d) | "
                f"Eff: {(details.get('opening_efficiency') or 0):.2f} / "
                f"DC {(details.get('opening_directional') or 0) * 100:3.0f}% | "
                f"Price: ${details['price']:7.2f}"
            )

        logger.info(f"=" * 60)
        logger.info(f"Selected {len(selected)} stocks for trading today")
        logger.info(f"Stocks: {', '.join(selected)}")
        logger.info(f"=" * 60)

        return selected
