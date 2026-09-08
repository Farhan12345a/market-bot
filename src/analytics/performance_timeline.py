"""
Daily P&L against the settings actively being tuned, rendered as one inline
SVG timeline plus a table - added 2026-09-08 so "did this config change
help" is answered by opening the daily email, not by re-reading
config.yaml's history and daily_summary.csv by hand.

Reads logs/daily_summary.csv (one row per trading day, already written by
_write_daily_summary_csv in main.py) - no separate store, so there is only
ever one place daily numbers live. Never raises: any problem here degrades
to an empty section, never takes the whole email down with it - the same
fail-safe pattern every other optional report section in email_notifier.py
already follows (see _opening_burst_html, _after_exit_ratio_html).
"""
import csv
import html
import logging
import os

logger = logging.getLogger(__name__)

# (csv column, display label) - the settings worth watching for a change
# day-to-day, in display order. Chosen from the settings with the largest
# documented dollar swings in config.yaml's own history comments:
#   - max_positions_per_sector: the 2026-09-02 XLK cluster (-$267.94 of a
#     -$403.66 day) and the 2026-08-28 crypto-miner sweep (-$131 vs +$318
#     on the same complex the day before).
#   - reentry_cooldown_minutes: repeated same-decline re-entries losing
#     $340+$328+$238+$137+$134 across five symbols in one comment.
#   - daily_loss_limit_*: the 2026-09-02 false-early-stop (-$517.07 mark
#     vs -$76.40 actually realized).
# final_stop_loss_pct / first_scale_out_config / trailing_stop_pct /
# entry_window were already columns in daily_summary.csv before this file
# existed - not duplicated here.
TRACKED_COLUMNS = [
    ("take_profit_tiers", "Take-profit"),
    ("breakeven_tiers", "Breakeven"),
    ("opening_burst_max_positions", "Burst max pos"),
    ("opening_burst_size_multiplier", "Burst size"),
    ("num_stocks_to_trade", "Traded pool"),
    ("stream_max_subscriptions", "Stream cap"),
    ("max_positions_per_sector", "Sector cap"),
    ("reentry_cooldown_minutes", "Re-entry cooldown"),
    ("daily_loss_limit_pct_of_equity", "Daily loss limit %"),
    ("daily_loss_limit_ceiling_usd", "Daily loss ceiling $"),
]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_rows(csv_path):
    if not os.path.exists(csv_path):
        return []
    try:
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        logger.debug(f"performance timeline: could not read {csv_path}: {e}")
        return []
    # One row per DATE. A same-day re-run of finish_day (e.g. a process
    # restart mid-session) can append twice under the old code path; keep
    # the LAST one for a given date rather than double-counting or crashing
    # on it - it is the more complete write.
    by_date = {}
    for r in rows:
        d = (r.get("date") or "").strip()
        if d:
            by_date[d] = r
    return [by_date[d] for d in sorted(by_date)]


def render_performance_timeline_html(csv_path="logs/daily_summary.csv", max_days=60):
    """
    Returns an HTML section (SVG bar chart + table) of daily P&L against the
    settings in TRACKED_COLUMNS, or "" if there is not yet enough data or
    anything goes wrong reading it. Never raises.
    """
    try:
        rows = _load_rows(csv_path)
        rows = [r for r in rows if _f(r.get("total_pl")) is not None]
        if len(rows) < 2:
            return ""  # a single day is not a timeline yet
        rows = rows[-max_days:]

        pls = [_f(r["total_pl"]) for r in rows]
        pcts = [_f(r.get("total_pl_pct")) for r in rows]

        # Which days changed something worth annotating, and exactly what.
        # A missing/blank value on either side is not treated as a change -
        # it means the column did not exist yet on an older row (the tracked
        # set has grown over time), not that the setting moved.
        changes = [None] * len(rows)
        for i in range(1, len(rows)):
            diffs = []
            for col, label in TRACKED_COLUMNS:
                before, after = (rows[i - 1].get(col) or "").strip(), (rows[i].get(col) or "").strip()
                if before and after and before != after:
                    diffs.append(f"{label}: {before} -> {after}")
            if diffs:
                changes[i] = diffs

        return _render_svg(rows, pls, pcts, changes) + _render_table(rows, pls, pcts, changes)
    except Exception as e:
        logger.error(f"performance timeline render failed, omitting section: {e}")
        return ""


def _render_svg(rows, pls, pcts, changes):
    n = len(rows)
    W = max(600, min(1100, 70 + n * 26))
    H = 220
    pad_l, pad_r, pad_t, pad_b = 50, 20, 20, 30
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    step = plot_w / n
    bar_w = max(4.0, step * 0.6)

    hi, lo = max(pls + [0.0]), min(pls + [0.0])
    span = (hi - lo) or 1.0

    def y_of(v):
        return pad_t + plot_h * (1 - (v - lo) / span)

    zero_y = y_of(0.0)
    parts = [
        f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{W - pad_r}" y2="{zero_y:.1f}" '
        f'stroke="#9ca3af" stroke-width="1"/>'
    ]

    for i, pl in enumerate(pls):
        x = pad_l + i * step + (step - bar_w) / 2
        top = min(zero_y, y_of(pl))
        h = abs(zero_y - y_of(pl)) or 1.0
        color = "#10b981" if pl >= 0 else "#ef4444"
        date = html.escape(rows[i].get("date", ""))
        pct = pcts[i]
        pct_txt = f", {pct:+.2f}%" if pct is not None else ""
        title = html.escape(f"{date}: ${pl:+,.2f}{pct_txt}")
        parts.append(
            f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'fill="{color}"><title>{title}</title></rect>'
        )
        if changes[i]:
            cx = pad_l + i * step + step / 2
            marker_title = html.escape(f"{date} - settings changed: " + "; ".join(changes[i]))
            parts.append(
                f'<line x1="{cx:.1f}" y1="{pad_t}" x2="{cx:.1f}" y2="{pad_t + plot_h}" '
                f'stroke="#6366f1" stroke-width="1.5" stroke-dasharray="3,3">'
                f'<title>{marker_title}</title></line>'
                f'<circle cx="{cx:.1f}" cy="{pad_t - 6}" r="4" fill="#6366f1">'
                f'<title>{marker_title}</title></circle>'
            )

    first_date = html.escape(rows[0].get("date", ""))
    last_date = html.escape(rows[-1].get("date", ""))
    parts.append(f'<text x="{pad_l}" y="{H - 6}" font-size="10" fill="#9ca3af">{first_date}</text>')
    parts.append(
        f'<text x="{W - pad_r}" y="{H - 6}" font-size="10" fill="#9ca3af" '
        f'text-anchor="end">{last_date}</text>'
    )

    return (
        '<h2 style="margin-top:30px;border-bottom:2px solid #6366f1;padding-bottom:10px;">'
        'Daily P&amp;L vs. Active Settings</h2>'
        '<div style="font-size:11px;color:#6b7280;margin-bottom:8px;">'
        "Each bar is one trading day's realized P&amp;L. A blue dashed marker means "
        "at least one tracked setting changed that day - hover it (or a bar) for "
        "detail if your mail client renders it, or see the table below either way."
        '</div>'
        f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" '
        f'style="background:#fafafa;border-radius:8px;" xmlns="http://www.w3.org/2000/svg">'
        + "".join(parts) +
        '</svg>'
    )


def _render_table(rows, pls, pcts, changes):
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in TRACKED_COLUMNS)
    body = []
    for i, row in enumerate(rows):
        pl, pct = pls[i], pcts[i]
        c = "#10b981" if pl >= 0 else "#ef4444"
        pct_txt = f"{pct:+.2f}%" if pct is not None else "n/a"
        row_style = ' style="background:#eef2ff;"' if changes[i] else ""
        cells = "".join(
            f"<td>{html.escape(str(row.get(col) or 'n/a'))}</td>"
            for col, _ in TRACKED_COLUMNS
        )
        body.append(
            f'<tr{row_style}><td>{html.escape(row.get("date", ""))}</td>'
            f'<td style="color:{c};font-weight:600;">${pl:+,.2f}</td>'
            f'<td style="color:{c};">{pct_txt}</td>{cells}</tr>'
        )
    return (
        '<div style="overflow-x:auto;margin-top:10px;">'
        '<table class="trades-table" style="font-size:11px;"><thead><tr>'
        '<th>Date</th><th>P&amp;L</th><th>P&amp;L %</th>' + head +
        '</tr></thead><tbody>' + "".join(body) + '</tbody></table>'
        '<div style="font-size:10px;color:#9ca3af;margin-top:4px;">'
        'Highlighted rows are days where at least one tracked setting changed '
        'from the day before.</div></div>'
    )
