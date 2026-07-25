"""
depth_snapshot.py - record Polymarket order-book DEPTH for upcoming LoL maps.

WHY THIS EXISTS
---------------
Price can be recovered later. Depth cannot: /book returns 404 once a market
settles. Every hour this is not running is a measurement nobody can ever take
again.

WHAT IT DOES
------------
1. Asks Gamma which LoL markets (tag 65) are open.
2. Keeps the ones starting within --window minutes.
3. Asks CLOB for the order book of each side.
4. Writes one row per token per run to a CSV. Append only, never edits.

USAGE
-----
    python depth_snapshot.py --self-test          # no network, checks the maths
    python depth_snapshot.py --dry-run            # network, prints, writes nothing
    python depth_snapshot.py --out data\\depth.csv  # the real thing

Run it on a schedule, every 15 minutes. Windows Task Scheduler is fine.

Standard library only. No pip install needed.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
LOL_TAG_ID = 65

FIELDS = [
    "ts_utc",           # when this snapshot was taken
    "event_id",
    "market_id",
    "question",         # e.g. "T1 vs GEN: Game 2 Winner"
    "market_kind",      # "map" or "series". Nothing else is stored.
    "game_no",          # 1, 2, 3 for map markets, blank for series
    "start_time",       # market's own start timestamp
    "mins_to_start",
    "outcome",          # which side this token pays out on
    "token_id",         # 77 digits. ALWAYS text, never a number.
    "best_bid",
    "best_ask",
    "mid",
    "spread",
    "bid_depth_1c",     # dollars resting within 1c of the best bid
    "bid_depth_3c",
    "bid_depth_5c",
    "ask_cost_1c",      # dollars needed to sweep asks within 1c of the best ask
    "ask_cost_3c",
    "ask_cost_5c",
    "n_bid_levels",
    "n_ask_levels",
    "volume",           # lifetime turnover, for reference only
]


# ---------------------------------------------------------------- http

def get_json(url, retries=3, pause=1.5):
    """GET a URL and parse JSON. Returns None instead of raising, so one dead
    market never kills a whole run."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "lol-depth-snapshot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries - 1:
                print(f"  http {e.code} on {url[:90]}", file=sys.stderr)
                return None
        except Exception as e:
            if attempt == retries - 1:
                print(f"  failed {type(e).__name__} on {url[:90]}", file=sys.stderr)
                return None
        time.sleep(pause * (attempt + 1))
    return None


# ---------------------------------------------------------------- maths
# These functions are pure. The self-test checks them without network.

def _levels(raw):
    """Turn [{'price': '0.44', 'size': '120'}, ...] into [(float, float), ...].
    Drops anything unparseable rather than guessing."""
    out = []
    for lvl in raw or []:
        try:
            out.append((float(lvl["price"]), float(lvl["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def depth_within(levels, best, band, side):
    """Dollars resting within `band` of `best`.

    side='bid': levels at or above best - band. This is what you could SELL into.
    side='ask': levels at or below best + band. This is what you could BUY.

    Returns dollars (price * size summed), not share count, because dollars is
    the number you actually care about when asking 'can I get $500 down'.
    """
    if best is None:
        return 0.0
    total = 0.0
    for price, size in levels:
        if side == "bid" and price >= best - band - 1e-9:
            total += price * size
        elif side == "ask" and price <= best + band + 1e-9:
            total += price * size
    return round(total, 2)


def summarise_book(book):
    """Reduce a raw /book response to the numbers we store."""
    bids = sorted(_levels(book.get("bids")), key=lambda x: -x[0])
    asks = sorted(_levels(book.get("asks")), key=lambda x: x[0])

    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = round((best_bid + best_ask) / 2, 4) if (best_bid and best_ask) else None
    spread = round(best_ask - best_bid, 4) if (best_bid and best_ask) else None

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "bid_depth_1c": depth_within(bids, best_bid, 0.01, "bid"),
        "bid_depth_3c": depth_within(bids, best_bid, 0.03, "bid"),
        "bid_depth_5c": depth_within(bids, best_bid, 0.05, "bid"),
        "ask_cost_1c": depth_within(asks, best_ask, 0.01, "ask"),
        "ask_cost_3c": depth_within(asks, best_ask, 0.03, "ask"),
        "ask_cost_5c": depth_within(asks, best_ask, 0.05, "ask"),
        "n_bid_levels": len(bids),
        "n_ask_levels": len(asks),
    }


def classify(question):
    """Label a market. Returns (kind, game_no). Nothing is dropped.

    Everything Polymarket lists gets stored, because order-book depth 404s
    after settlement and cannot be collected later. The label is here so that
    filtering is a one-line pandas query at analysis time, which is a decision
    you can undo, rather than a collection-time decision you cannot.

    Only "map" and "series" are covered by the frozen rule. The rest is stored
    and left alone.

        "T1 vs GEN - Game 2 Winner"          -> ("map", 2)
        "T1 vs GEN (BO3) - LCK Regular"      -> ("series", "")
        "Games Total: O/U 2.5"               -> ("total_games", "")
        "Game Handicap: FUR (-1.5)"          -> ("handicap", "")
        "Total Kills Over/Under 27.5 in Game 1?" -> ("total_kills", 1)
        "First Blood in Game 1?"             -> ("first_blood", 1)
        "Game 2: Both Teams Slay a Dragon?"  -> ("prop", 2)
    """
    if not question:
        return "other", ""
    low = question.lower()

    m = re.search(r"game\s+(\d+)", low)
    game_no = int(m.group(1)) if m else ""

    if re.search(r"game\s+\d+\s+winner", low):
        return "map", game_no
    if re.search(r"\(bo\d\)", low):
        return "series", ""
    if "handicap" in low:
        return "handicap", game_no
    if "odd/even" in low or "odd or even" in low:
        return "odd_even", game_no
    if "games total" in low:
        return "total_games", game_no
    if "total kills" in low and ("over/under" in low or "o/u" in low):
        return "total_kills", game_no
    if "first blood" in low:
        return "first_blood", game_no
    if game_no != "":
        return "prop", game_no
    return "other", ""


def parse_start(market, event):
    """Best available start timestamp, as an aware UTC datetime or None."""
    for src in (market, event):
        for key in ("gameStartTime", "startDate", "startTime", "eventStartTime"):
            raw = (src or {}).get(key)
            if not raw:
                continue
            try:
                return datetime.fromisoformat(
                    str(raw).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except ValueError:
                continue
    return None


def token_ids(market):
    """clobTokenIds arrives as a JSON STRING holding a list of 77-digit ids.
    Keep them as text. A float would silently destroy the last 60 digits."""
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [str(t) for t in (raw or [])]


def outcomes(market):
    raw = market.get("outcomes")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [str(o) for o in (raw or [])]


# ---------------------------------------------------------------- collect

def fetch_open_events(limit=500):
    """All open LoL events. Paginates until Gamma stops giving us new ones."""
    events, offset = [], 0
    while True:
        url = (f"{GAMMA}/events?tag_id={LOL_TAG_ID}&closed=false"
               f"&limit=100&offset={offset}")
        batch = get_json(url)
        if not batch:
            break
        events.extend(batch)
        if len(batch) < 100 or len(events) >= limit:
            break
        offset += 100
        time.sleep(0.3)
    return events


def collect(window_minutes, verbose=True, core_only=False):
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=window_minutes)
    rows, skipped = [], 0

    events = fetch_open_events()
    if verbose:
        print(f"{now:%Y-%m-%d %H:%M} UTC  open LoL events: {len(events)}")

    for event in events:
        for market in event.get("markets", []):
            start = parse_start(market, event)
            if start is None or not (now - timedelta(minutes=30) <= start <= horizon):
                continue

            kind, game_no = classify(market.get("question", ""))
            if core_only and kind not in ("map", "series"):
                skipped += 1
                continue

            ids, names = token_ids(market), outcomes(market)
            if not ids:
                continue

            for i, tok in enumerate(ids):
                book = get_json(f"{CLOB}/book?token_id={tok}")
                time.sleep(0.25)
                if not book:
                    continue

                row = {
                    "ts_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "event_id": event.get("id", ""),
                    "market_id": market.get("id", ""),
                    "question": market.get("question", ""),
                    "market_kind": kind or "",
                    "game_no": game_no,
                    "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "mins_to_start": round((start - now).total_seconds() / 60, 1),
                    "outcome": names[i] if i < len(names) else "",
                    "token_id": tok,
                    "volume": market.get("volumeNum", market.get("volume", "")),
                }
                row.update(summarise_book(book))
                rows.append(row)

                # Print only what the rule can bet on. Printing 58 lines per
                # match makes the Actions log unreadable.
                if verbose and kind in ("map", "series"):
                    print(f"  [{row['mins_to_start']:>6.1f}m] "
                          f"{row['question'][:44]:<44} {row['outcome'][:14]:<14} "
                          f"bid {row['best_bid']} ask {row['best_ask']}  "
                          f"buyable@3c ${row['ask_cost_3c']:,.0f}")

    if verbose:
        kinds = {}
        for r in rows:
            kinds[r["market_kind"]] = kinds.get(r["market_kind"], 0) + 1
        if kinds:
            print("  stored: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
        if skipped:
            print(f"  skipped {skipped} non-core markets (--core-only is on)")
    return rows


def append_csv(outdir, rows):
    """One file per UTC day: depth/2026-07-25.csv

    Not one big file. A single growing CSV committed every 15 minutes makes git
    store a fresh copy of the whole thing each time, so by month twelve every
    commit rewrites half a gigabyte. Daily files mean a commit touches one small
    file and yesterday's file is never written again.

    Rows are grouped by their own timestamp rather than by 'today', so a run
    that straddles midnight files each row in the right day.
    """
    if not rows:
        return []
    os.makedirs(outdir, exist_ok=True)
    written = []
    by_day = {}
    for r in rows:
        by_day.setdefault(r["ts_utc"][:10], []).append(r)

    for day, day_rows in sorted(by_day.items()):
        path = os.path.join(outdir, f"{day}.csv")
        fresh = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if fresh:
                w.writeheader()
            w.writerows(day_rows)
        written.append((path, len(day_rows)))
    return written


# ---------------------------------------------------------------- self-test

def self_test():
    """Known-answer test. Every number below was worked out by hand first.
    If this fails, do not trust anything the script writes."""
    book = {
        "bids": [{"price": "0.44", "size": "100"},   # 44.00
                 {"price": "0.43", "size": "200"},   # 86.00  -> within 1c
                 {"price": "0.41", "size": "500"},   # 205.00 -> within 3c
                 {"price": "0.30", "size": "1000"}], # 300.00 -> outside 5c
        "asks": [{"price": "0.46", "size": "50"},    # 23.00
                 {"price": "0.47", "size": "100"},   # 47.00  -> within 1c
                 {"price": "0.49", "size": "200"},   # 98.00  -> within 3c
                 {"price": "0.60", "size": "900"}],  # outside 5c
    }
    s = summarise_book(book)
    checks = [
        ("best_bid", s["best_bid"], 0.44),
        ("best_ask", s["best_ask"], 0.46),
        ("mid", s["mid"], 0.45),
        ("spread", s["spread"], 0.02),
        ("bid_depth_1c", s["bid_depth_1c"], 130.00),
        ("bid_depth_3c", s["bid_depth_3c"], 335.00),
        ("bid_depth_5c", s["bid_depth_5c"], 335.00),
        ("ask_cost_1c", s["ask_cost_1c"], 70.00),
        ("ask_cost_3c", s["ask_cost_3c"], 168.00),
        ("ask_cost_5c", s["ask_cost_5c"], 168.00),
    ]

    empty = summarise_book({"bids": [], "asks": []})
    checks.append(("empty_book_bid", empty["best_bid"], None))
    checks.append(("empty_book_depth", empty["bid_depth_3c"], 0.0))

    one_sided = summarise_book({"bids": [{"price": "0.44", "size": "10"}], "asks": []})
    checks.append(("one_sided_mid", one_sided["mid"], None))

    # Every title below is a real one, copied from a live run on 2026-07-25.
    labels = [
        ("LoL: FURIA Esports vs LOS - Game 1 Winner", ("map", 1)),
        ("LoL: FURIA Esports vs LOS - Game 2 Winner", ("map", 2)),
        ("LoL: FURIA Esports vs LOS (BO3) - CBLOL Regular Season", ("series", "")),
        ("Games Total: O/U 2.5", ("total_games", "")),
        ("Game Handicap: FUR (-1.5) vs LOS (+1.5)", ("handicap", "")),
        ("Total Kills Over/Under 27.5 in Game 1?", ("total_kills", 1)),
        ("First Blood in Game 2?", ("first_blood", 2)),
        ("Game 1: Both Teams Slay Baron Nashor?", ("prop", 1)),
        ("Game 3: Odd/Even Total Kills?", ("odd_even", 3)),
        ("Game 2: Any Player Penta Kill?", ("prop", 2)),
    ]
    for title, want in labels:
        checks.append((title[:30], classify(title), want))

    big = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
    checks.append(("token_stays_text",
                   token_ids({"clobTokenIds": json.dumps([big, "123"])})[0], big))

    # Daily split: two rows on either side of midnight must land in two files,
    # and a second write to the same day must not repeat the header.
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        append_csv(tmp, [{"ts_utc": "2026-07-25 23:55:00", "token_id": "a"},
                         {"ts_utc": "2026-07-26 00:05:00", "token_id": "b"}])
        append_csv(tmp, [{"ts_utc": "2026-07-25 23:58:00", "token_id": "c"}])
        files = sorted(os.listdir(tmp))
        with open(os.path.join(tmp, "2026-07-25.csv"), encoding="utf-8") as f:
            lines_25 = [l for l in f.read().splitlines() if l.strip()]
        checks.append(("daily_files", files, ["2026-07-25.csv", "2026-07-26.csv"]))
        checks.append(("rows_in_day", len(lines_25), 3))          # header + 2
        checks.append(("one_header", lines_25[0].startswith("ts_utc"), True))
        checks.append(("no_repeat_header",
                       sum(l.startswith("ts_utc") for l in lines_25), 1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = 0
    for name, got, want in checks:
        ok = got == want
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {name:<18} got {got!r:<20} want {want!r}")
    print("\nall passed" if bad == 0 else f"\n{bad} FAILED - do not use this script")
    return bad == 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outdir", default="depth",
                    help="folder for the daily CSVs")
    ap.add_argument("--window", type=int, default=90,
                    help="snapshot markets starting within this many minutes")
    ap.add_argument("--core-only", action="store_true",
                    help="store only map and series markets. OFF by default: "
                         "depth cannot be collected retroactively, so we keep "
                         "everything and filter at analysis time instead.")
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    ap.add_argument("--self-test", action="store_true", help="no network, check maths")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    rows = collect(args.window, core_only=args.core_only)
    if not rows:
        print("nothing starting in the window. normal off-hours.")
        return
    if args.dry_run:
        print(f"\ndry run: {len(rows)} rows not written")
        return
    for path, n in append_csv(args.outdir, rows):
        print(f"\nwrote {n} rows -> {path}")


if __name__ == "__main__":
    main()
