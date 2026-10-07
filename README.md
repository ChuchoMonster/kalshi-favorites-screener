# Kalshi Favorites Screener

A read-only command-line screener that scans open [Kalshi](https://kalshi.com) prediction markets
for **long-dated, liquid favorites** — the profile that research on the favorite-longshot bias
suggests is most often underpriced.

It uses only Kalshi's public market-data API: no account, no API key, no order placement.

> This is a screener, not a signal. "Passes the filter" means a market fits a structural profile,
> not that it is mispriced. Nothing here is financial advice.

## The idea

- **Favorite-longshot bias**: in betting and prediction markets, high-probability outcomes tend
  to be slightly underpriced and longshots overpriced. The screen looks at contracts priced
  70–90c.
- **Time horizon**: the effect is strongest well before resolution, so markets closing in under
  30 days are excluded by default.
- **Liquidity**: only markets with enough volume that you could exit early rather than hold to
  expiry.
- **Categories**: Sports, Politics and Elections.

## How it works

1. Pages through `GET /events?status=open` and maps each event ticker to its category, keeping
   only target categories.
2. Pages through `GET /markets?status=open`, pushing the days-to-close window to the API via
   `min_close_ts` / `max_close_ts` so it doesn't download hundreds of thousands of short-dated
   per-game markets.
3. For each market:
   - drops multivariate/parlay markets;
   - computes a mid price from the yes bid/ask, falling back to last trade or a one-sided book;
   - applies price, days-to-close and volume filters.
4. Sorts survivors by volume and prints a table with mid, bid/ask, spread, volume and days to
   close, followed by execution reminders (rest maker orders inside the spread, plan an exit).

Cursor pagination, exponential backoff on HTTP 429, and a small delay between pages keep it well
under Kalshi's rate limits.

## Stack

Python 3.9+ standard library only (`urllib`, `argparse`, `json`). Nothing to install.

## Usage

```bash
python3 kalshi_screener.py                      # defaults: 70-90c, >=30 days, volume >= 500k
python3 kalshi_screener.py --top 25
python3 kalshi_screener.py --min-volume 100000 --min-days 14 --max-days 180
python3 kalshi_screener.py --categories Politics Elections --min-price 0.75
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--min-price` / `--max-price` | 0.70 / 0.90 | Mid-price band, in dollars |
| `--min-days` / `--max-days` | 30 / none | Days until the market closes |
| `--min-volume` | 500000 | Minimum cumulative contracts traded |
| `--top` | all | Show only the top N by volume |
| `--categories` | all three | Any of `Sports`, `Politics`, `Elections` |

Progress messages go to stderr and the table to stdout, so `> results.txt` captures just the
results.

## Environment variables

None. The API base URL is `https://api.elections.kalshi.com/trade-api/v2`.
