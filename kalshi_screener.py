#!/usr/bin/env python3
"""
Kalshi favorite-market screener (read-only).

Operationalizes a strategy drawn from prediction-market research:
  - favorite-longshot bias: lean toward high-probability favorites (70-90c)
  - long-horizon underpricing: favorites are most underpriced > 1 month out
  - liquidity: only markets deep enough to exit early instead of holding to expiry
  - categories: Sports, Politics, Elections

This is a SCREENER, not a signal. "Passes filter" != "vetted bet". It surfaces
markets fitting a structural profile; it does NOT know which are mispriced. Trade
small, across many positions, as a maker, with a planned exit.

Uses Kalshi's public REST API. No API key, no order placement, zero trade risk.
Standard library only -- no pip install required.
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
TARGET_CATEGORIES = ["Sports", "Politics", "Elections"]
USER_AGENT = "kalshi-screener/1.0 (read-only)"


def fetch_paginated(path, params=None, page_limit=1000, delay=0.25):
    """Page through a cursor-paginated Kalshi endpoint, yielding raw page dicts."""
    params = dict(params or {})
    params.setdefault("limit", 200)
    pages = 0
    cursor = None
    while True:
        if cursor:
            params["cursor"] = cursor
        query = urllib.parse.urlencode(params)
        url = f"{BASE_URL}{path}?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 5:
                    time.sleep(2 ** attempt)  # exponential backoff on rate limit
                    continue
                raise
        yield data
        cursor = data.get("cursor")
        pages += 1
        if not cursor or pages >= page_limit:
            break
        time.sleep(delay)  # politeness; well under the ~30 req/s cap


def load_event_categories(categories):
    """Build {event_ticker: category}, keeping only the target categories."""
    wanted = set(categories)
    mapping = {}
    for page in fetch_paginated("/events", {"status": "open"}):
        for event in page.get("events", []):
            cat = event.get("category")
            if cat in wanted:
                mapping[event.get("event_ticker")] = cat
    return mapping


def load_markets(min_close_ts=None, max_close_ts=None):
    """Return open market dicts. min_close_ts/max_close_ts (unix seconds) push the
    days-to-close window to the API, which cuts the universe from ~hundreds of
    thousands of short-dated per-game markets to the set the screen actually wants."""
    params = {"status": "open", "limit": 1000}
    if min_close_ts is not None:
        params["min_close_ts"] = int(min_close_ts)
    if max_close_ts is not None:
        params["max_close_ts"] = int(max_close_ts)
    markets = []
    for page in fetch_paginated("/markets", params):
        markets.extend(page.get("markets", []))
    return markets


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value):
    if not value:
        return None
    try:
        # Kalshi returns RFC3339, e.g. "2026-06-28T09:00:00Z"
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_parlay(market):
    """Multivariate / combo markets are not single-event favorites -- exclude them."""
    if market.get("mve_selected_legs"):
        return True
    event = market.get("event_ticker") or ""
    return event.startswith("KXMVE")


def mid_price(market):
    """Mid of yes bid/ask in dollars; fall back to last price when book is one-sided."""
    bid = _to_float(market.get("yes_bid_dollars"))
    ask = _to_float(market.get("yes_ask_dollars"))
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    last = _to_float(market.get("last_price_dollars"))
    if last and last > 0:
        return last
    # one-sided book: use whichever side exists
    for side in (bid, ask):
        if side and side > 0:
            return side
    return None


def screen(markets, categories_map, args, now):
    rows = []
    for m in markets:
        if is_parlay(m):
            continue
        category = categories_map.get(m.get("event_ticker"))
        if category is None:
            continue  # not in a target category

        price = mid_price(m)
        if price is None or not (args.min_price <= price <= args.max_price):
            continue

        close = _parse_time(m.get("close_time"))
        if close is None:
            continue
        days_to_close = (close - now).total_seconds() / 86400.0
        if days_to_close < args.min_days:
            continue
        if args.max_days is not None and days_to_close > args.max_days:
            continue

        volume = _to_float(m.get("volume_fp")) or 0.0
        if volume < args.min_volume:
            continue

        bid = _to_float(m.get("yes_bid_dollars")) or 0.0
        ask = _to_float(m.get("yes_ask_dollars")) or 0.0
        spread_c = (ask - bid) * 100.0 if (bid > 0 and ask > 0) else None

        rows.append({
            "category": category,
            "ticker": m.get("ticker", ""),
            "title": m.get("title", "") or m.get("yes_sub_title", ""),
            "mid_c": price * 100.0,
            "bid_c": bid * 100.0,
            "ask_c": ask * 100.0,
            "spread_c": spread_c,
            "volume": volume,
            "days_to_close": days_to_close,
        })

    rows.sort(key=lambda r: r["volume"], reverse=True)
    if args.top:
        rows = rows[: args.top]
    return rows


def _trunc(text, width):
    text = text.replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def print_table(rows, args):
    bar = "=" * 100
    print(bar)
    print("KALSHI FAVORITE-MARKET SCREENER  (read-only)")
    print(
        f"Filters: price {args.min_price*100:.0f}-{args.max_price*100:.0f}c | "
        f"{args.min_days:g}-{args.max_days:g} days to close | " if args.max_days is not None else
        f"Filters: price {args.min_price*100:.0f}-{args.max_price*100:.0f}c | "
        f">{args.min_days:g} days to close | "
    )
    print(
        f"  volume >= {args.min_volume:,.0f} contracts | "
        f"categories: {', '.join(args.categories)}"
    )
    print(bar)

    if not rows:
        print("\nNo markets passed the screen. Try loosening --min-volume / --min-days,")
        print("or widening --min-price / --max-price.\n")
        return

    header = (
        f"{'Category':<10} {'Ticker':<26} {'Title':<34} "
        f"{'Mid':>5} {'Bid/Ask':>9} {'Spr':>5} {'Volume':>12} {'Days':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        bidask = f"{r['bid_c']:.0f}/{r['ask_c']:.0f}" if r["ask_c"] else "   -"
        spread = f"{r['spread_c']:.0f}c" if r["spread_c"] is not None else "  -"
        print(
            f"{r['category']:<10} {_trunc(r['ticker'],26):<26} {_trunc(r['title'],34):<34} "
            f"{r['mid_c']:>4.0f}c {bidask:>9} {spread:>5} {r['volume']:>12,.0f} {r['days_to_close']:>5.0f}d"
        )

    print("-" * len(header))
    print(f"{len(rows)} market(s) passed the screen.\n")
    print(bar)
    print("PASSES FILTER != VETTED BET -- this is a screener, not a signal.")
    print("It surfaces markets fitting a structural profile; it does NOT know which are mispriced.")
    print("Trade small, across many positions, and treat your whole allocation as a research budget.")
    print("")
    print("Execution reminders from the strategy:")
    print("  - Buy as a MAKER: rest a limit order a cent or two inside the spread; avoid market orders.")
    print("  - Plan your EXIT price up front; sell as it re-rates toward fair value, don't hold to expiry.")
    print("  - Prefer tighter-spread rows -- wide spreads make maker fills and early exits harder.")
    print("  - 'Volume' is cumulative contract count (a proxy for the ~$500k dollar-volume figure).")
    print(bar)


def parse_args(argv):
    p = argparse.ArgumentParser(description="Read-only Kalshi favorite-market screener.")
    p.add_argument("--min-price", type=float, default=0.70, help="Min mid price in dollars (default 0.70).")
    p.add_argument("--max-price", type=float, default=0.90, help="Max mid price in dollars (default 0.90).")
    p.add_argument("--min-days", type=float, default=30, help="Min days to close (default 30).")
    p.add_argument("--max-days", type=float, default=None, help="Max days to close (default: no cap).")
    p.add_argument("--min-volume", type=float, default=500000,
                   help="Min cumulative contract volume (default 500000).")
    p.add_argument("--top", type=int, default=None, help="Show only the top N by volume.")
    p.add_argument("--categories", nargs="+", default=TARGET_CATEGORIES,
                   choices=["Sports", "Politics", "Elections"],
                   help="Categories to include (default: all three).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    now = datetime.now(timezone.utc)
    try:
        print("Loading event categories...", file=sys.stderr)
        categories_map = load_event_categories(args.categories)
        print(f"  {len(categories_map):,} events in target categories.", file=sys.stderr)
        print("Loading open markets...", file=sys.stderr)
        min_close_ts = now.timestamp() + args.min_days * 86400
        max_close_ts = (now.timestamp() + args.max_days * 86400) if args.max_days is not None else None
        markets = load_markets(min_close_ts=min_close_ts, max_close_ts=max_close_ts)
        print(f"  {len(markets):,} open markets fetched. Screening...\n", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"Network error reaching Kalshi API: {e}", file=sys.stderr)
        return 1

    rows = screen(markets, categories_map, args, now)
    print_table(rows, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
