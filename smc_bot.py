#!/usr/bin/env python3
"""
SignalBot: scanner + PAPER trader (it never places real orders).

Sectors (top to bottom of this file):
  0  CONFIG
  1  DATA       one adapter per exchange (Binance, Bybit, OKX)
  2  STRATEGIES SMC, Supply/Demand, CRT, Price Action  (+ confluence merge)
  3  ARBITRAGE  triangular, inside one exchange
  4  PAPER      opens/tracks virtual trades, keeps win-rate and total R
  5  MAIN       loop, logging, notifications

Needs only:  pkg install python && pip install requests
Run:         termux-wake-lock && nohup python smc_bot.py > out.log 2>&1 &
"""
import json, os, sys, time, shutil, subprocess, traceback
from datetime import datetime
import requests

# ============================ 0. CONFIG ============================
CFG = {
    "exchanges": ["okx", "kucoin", "gate"],   # blocked ones just log "no data"
    "timeframes": ["15m", "1h"],       # CRT runs on "1h" only (needs daily + 1h)
    "quote": "USDT",                   # which pairs to scan for setups
    "top_n": 12,                       # top-N pairs by 24h volume, per exchange
    "rr": 3.0,                         # reward:risk (1:3). Change here.
    "min_confluence": 1,               # 2 = only when 2+ strategies agree
    "scan_every": 300,                 # seconds between scans
    "paper_fee": 0.0,                  # fee per side counted in paper R (0 = ignore)
    "crt_ratio": 1.85,                 # swept daily candle >= ratio * sweeping candle
    "arb_start": ["USDT", "BTC"],      # triangular start currencies
    "arb_fee": 0.0,                    # you handle fees: lines show profit BEFORE fees
    "arb_assets": ["USDT","USDC","BTC","ETH","BNB","SOL","XRP","DOGE","ADA","TRX","LINK","LTC","AVAX","TON"],
    "arb_min_profit": 0.002,           # 0.2% gross spread
    "arb_min_qvol": 200000,            # skip illiquid pairs (24h quote volume)
    "max_bars": 100,                   # close a paper trade after this many candles
}
STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDE", "PYUSD", "EUR", "GBP"}
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
LOG_FILE = os.path.join(HERE, "bot.log")


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def notify(title, text):
    if shutil.which("termux-notification"):
        try:
            subprocess.run(["termux-notification", "--title", title, "--content", text], timeout=10)
        except Exception:
            pass


# ============================ 1. DATA ============================
S = requests.Session()


def get(url, params=None):
    for i in range(3):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(1 + i)
    return None


def fl(x):
    try:
        return float(x)
    except Exception:
        return 0.0


# Candle format everywhere: [time_ms, open, high, low, close, volume], oldest first,
# only CLOSED candles.

class Binance:
    name = "binance"
    B = "https://api.binance.com"
    TF = {"15m": "15m", "1h": "1h", "1d": "1d"}

    def __init__(self):
        self.meta = None

    def pairs(self):
        if self.meta is None:
            d = get(self.B + "/api/v3/exchangeInfo")
            if not d:
                return {}
            self.meta = {s["symbol"]: (s["baseAsset"], s["quoteAsset"])
                         for s in d["symbols"] if s["status"] == "TRADING"}
        d = get(self.B + "/api/v3/ticker/24hr")
        out = {}
        for t in d or []:
            m = self.meta.get(t["symbol"])
            if m:
                out[t["symbol"]] = dict(base=m[0], quote=m[1], bid=fl(t["bidPrice"]),
                                        ask=fl(t["askPrice"]), qvol=fl(t["quoteVolume"]))
        return out

    def candles(self, sym, tf):
        d = get(self.B + "/api/v3/klines", {"symbol": sym, "interval": self.TF[tf], "limit": 300})
        return [[fl(x) for x in k[:6]] for k in d[:-1]] if d else []   # last = still forming


class Bybit:
    name = "bybit"
    B = "https://api.bybit.com"
    TF = {"15m": "15", "1h": "60", "1d": "D"}

    def __init__(self):
        self.meta = None

    def pairs(self):
        if self.meta is None:
            d = get(self.B + "/v5/market/instruments-info", {"category": "spot", "limit": 1000})
            if not d:
                return {}
            self.meta = {i["symbol"]: (i["baseCoin"], i["quoteCoin"])
                         for i in d["result"]["list"] if i.get("status") == "Trading"}
        d = get(self.B + "/v5/market/tickers", {"category": "spot"})
        out = {}
        for t in (d or {}).get("result", {}).get("list", []):
            m = self.meta.get(t["symbol"])
            if m:
                out[t["symbol"]] = dict(base=m[0], quote=m[1], bid=fl(t["bid1Price"]),
                                        ask=fl(t["ask1Price"]), qvol=fl(t["turnover24h"]))
        return out

    def candles(self, sym, tf):
        d = get(self.B + "/v5/market/kline",
                {"category": "spot", "symbol": sym, "interval": self.TF[tf], "limit": 300})
        rows = list(reversed((d or {}).get("result", {}).get("list", [])))   # newest-first -> oldest-first
        return [[fl(x) for x in r[:6]] for r in rows[:-1]]                   # drop forming candle


class OKX:
    name = "okx"
    B = "https://www.okx.com"
    TF = {"15m": "15m", "1h": "1H", "1d": "1Dutc"}

    def pairs(self):
        d = get(self.B + "/api/v5/market/tickers", {"instType": "SPOT"})
        out = {}
        for t in (d or {}).get("data", []):
            base, _, quote = t["instId"].partition("-")
            out[t["instId"]] = dict(base=base, quote=quote, bid=fl(t["bidPx"]),
                                    ask=fl(t["askPx"]), qvol=fl(t["volCcy24h"]))
        return out

    def candles(self, sym, tf):
        d = get(self.B + "/api/v5/market/candles", {"instId": sym, "bar": self.TF[tf], "limit": 300})
        rows = [r for r in reversed((d or {}).get("data", [])) if r[8] == "1"]   # confirmed only
        return [[fl(x) for x in r[:6]] for r in rows]


class KuCoin:
    name = "kucoin"
    B = "https://api.kucoin.com"
    TF = {"15m": "15min", "1h": "1hour", "1d": "1day"}

    def pairs(self):
        d = get(self.B + "/api/v1/market/allTickers")
        out = {}
        for t in ((d or {}).get("data") or {}).get("ticker", []):
            base, _, quote = t["symbol"].partition("-")
            out[t["symbol"]] = dict(base=base, quote=quote, bid=fl(t.get("buy")),
                                    ask=fl(t.get("sell")), qvol=fl(t.get("volValue")))
        return out

    def candles(self, sym, tf):
        d = get(self.B + "/api/v1/market/candles", {"symbol": sym, "type": self.TF[tf]})
        rows = list(reversed((d or {}).get("data") or []))[:-1]      # oldest-first, drop forming
        # KuCoin row: [time_s, open, close, high, low, volume, turnover]
        return [[fl(r[0]) * 1000, fl(r[1]), fl(r[3]), fl(r[4]), fl(r[2]), fl(r[5])] for r in rows]


class Gate:
    name = "gate"
    B = "https://api.gateio.ws/api/v4"
    TF = {"15m": "15m", "1h": "1h", "1d": "1d"}

    def pairs(self):
        d = get(self.B + "/spot/tickers")
        out = {}
        for t in d or []:
            base, _, quote = t["currency_pair"].partition("_")
            out[t["currency_pair"]] = dict(base=base, quote=quote, bid=fl(t.get("highest_bid")),
                                           ask=fl(t.get("lowest_ask")), qvol=fl(t.get("quote_volume")))
        return out

    def candles(self, sym, tf):
        d = get(self.B + "/spot/candlesticks",
                {"currency_pair": sym, "interval": self.TF[tf], "limit": 300})
        rows = [r for r in (d or []) if len(r) > 7 and str(r[7]).lower() == "true"]   # closed only
        # Gate row: [time_s, quote_vol, close, high, low, open, base_vol, closed]
        return [[fl(r[0]) * 1000, fl(r[5]), fl(r[3]), fl(r[4]), fl(r[2]), fl(r[6])] for r in rows]


# To add an exchange: write a class with pairs() and candles(), register it here.
EXCH = {"binance": Binance(), "bybit": Bybit(), "okx": OKX(), "kucoin": KuCoin(), "gate": Gate()}


# ============================ 2. STRATEGIES ============================
def atr(c, n=14):
    trs = [max(x[2] - x[3], abs(x[2] - c[i][4]), abs(x[3] - c[i][4])) for i, x in enumerate(c[1:])]
    return sum(trs[-n:]) / max(1, len(trs[-n:]))


def ema(vals, n):
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def swings(c, n=3):
    hi, lo = [], []
    for i in range(n, len(c) - n):
        w = range(i - n, i + n + 1)
        if all(c[i][2] > c[j][2] for j in w if j != i):
            hi.append((i, c[i][2]))
        if all(c[i][3] < c[j][3] for j in w if j != i):
            lo.append((i, c[i][3]))
    return hi, lo


def mk(name, side, entry, sl, a, max_atr=3.0):
    risk = abs(entry - sl)
    if not (0.15 * a <= risk <= max_atr * a):
        return None
    tp = entry + CFG["rr"] * risk if side == "long" else entry - CFG["rr"] * risk
    return dict(strat=name, side=side, entry=entry, sl=sl, tp=tp)


def smc_core(c, a, min_i=0, want=None, max_age=40):
    """Structure + order block. Returns dict(d, kind, zl, zh) when the LAST candle
    is tapping an unmitigated order block that caused a BOS/CHoCH, else None.
    min_i: ignore breaks before this candle index. want: 1 (bull) / -1 (bear) / None."""
    n = 3
    if len(c) < 60:
        return None
    hi, lo = swings(c, n)
    broken, trend, evs = set(), 0, []
    for i in range(10, len(c)):
        kh = [x for x in hi if x[0] <= i - n]
        kl = [x for x in lo if x[0] <= i - n]
        if kh and ("h", kh[-1][0]) not in broken and c[i][4] > kh[-1][1]:
            broken.add(("h", kh[-1][0]))
            evs.append((i, 1, "CHoCH" if trend == -1 else "BOS"))
            trend = 1
        elif kl and ("l", kl[-1][0]) not in broken and c[i][4] < kl[-1][1]:
            broken.add(("l", kl[-1][0]))
            evs.append((i, -1, "CHoCH" if trend == 1 else "BOS"))
            trend = -1
    evs = [e for e in evs if e[0] >= min_i and (want is None or e[1] == want)]
    if not evs:
        return None
    i, d, kind = evs[-1]
    last = len(c) - 1
    if i >= last or last - i > max_age:
        return None
    zl = zh = None
    for j in range(i - 1, max(i - 15, 0), -1):          # last opposite candle before the impulse
        bearish = c[j][4] < c[j][1]
        bullish = c[j][4] > c[j][1]
        if (d == 1 and bearish) or (d == -1 and bullish):
            zl, zh = c[j][3], c[j][2]
            break
    if zl is None:
        return None
    for k in range(i + 1, last):                         # mitigated if price already came back
        if d == 1 and c[k][3] <= zh:
            return None
        if d == -1 and c[k][2] >= zl:
            return None
    x = c[-1]
    if d == 1 and x[3] <= zh and x[4] > zl:
        return dict(d=d, kind=kind, zl=zl, zh=zh)
    if d == -1 and x[2] >= zl and x[4] < zh:
        return dict(d=d, kind=kind, zl=zl, zh=zh)
    return None


def strat_smc(c, a, ctx):
    z = smc_core(c, a)
    if not z:
        return None
    x = c[-1]
    if z["d"] == 1:
        return mk("SMC-" + z["kind"], "long", x[4], z["zl"] - 0.1 * a, a)
    return mk("SMC-" + z["kind"], "short", x[4], z["zh"] + 0.1 * a, a)


def strat_sd(c, a, ctx):
    """Fresh supply/demand: small base candle -> big impulse candle, zone untouched since."""
    last = len(c) - 1
    for i in range(last - 1, max(last - 80, 3), -1):
        if abs(c[i][4] - c[i][1]) < 1.8 * a:
            continue
        d = 1 if c[i][4] > c[i][1] else -1
        b = c[i - 1]
        if abs(b[4] - b[1]) > 0.6 * a:
            continue
        zl, zh = b[3], b[2]
        if (d == 1 and c[i][4] <= zh) or (d == -1 and c[i][4] >= zl):
            continue
        if not all((c[k][3] > zh) if d == 1 else (c[k][2] < zl) for k in range(i + 1, last)):
            continue
        x = c[-1]
        if d == 1 and x[3] <= zh and x[4] > zl:
            return mk("DEMAND", "long", x[4], zl - 0.1 * a, a)
        if d == -1 and x[2] >= zl and x[4] < zh:
            return mk("SUPPLY", "short", x[4], zh + 0.1 * a, a)
    return None


def strat_crt(c, a, ctx):
    """Your CRT rules: daily candle sweeps previous day's high/low and closes back inside,
    swept candle >= crt_ratio x sweeping candle; then on 1h a body-close CHoCH/BOS in the
    trade direction, entry when price retraces into the order block, stop beyond the sweep."""
    cd = ctx.get("daily") or []
    if ctx["tf"] != "1h" or len(cd) < 3:
        return None
    d1, d2 = cd[-2], cd[-1]
    if (d1[2] - d1[3]) < CFG["crt_ratio"] * (d2[2] - d2[3]):
        return None
    if d2[3] < d1[3] and d2[4] > d1[3]:
        want = 1
    elif d2[2] > d1[2] and d2[4] < d1[2]:
        want = -1
    else:
        return None
    min_i = next((i for i, x in enumerate(c) if x[0] >= d2[0]), None)
    if min_i is None:
        return None
    z = smc_core(c, a, min_i=min_i, want=want, max_age=60)
    if not z:
        return None
    x = c[-1]
    if want == 1:
        sl = min(k[3] for k in c[min_i:]) - 0.1 * a
        return mk("CRT", "long", x[4], sl, a, max_atr=6)
    sl = max(k[2] for k in c[min_i:]) + 0.1 * a
    return mk("CRT", "short", x[4], sl, a, max_atr=6)


def strat_pa(c, a, ctx):
    """Engulfing / pin bar at a recent swing level, with EMA50 trend filter."""
    x, p = c[-1], c[-2]
    rng = x[2] - x[3]
    if rng <= 0:
        return None
    e = ema([k[4] for k in c[-150:]], 50)
    hi, lo = swings(c[:-1], 3)
    cut = len(c) - 80
    sup = [v for k, v in lo if k > cut]
    res = [v for k, v in hi if k > cut]
    body = abs(x[4] - x[1])
    lw = min(x[1], x[4]) - x[3]
    uw = x[2] - max(x[1], x[4])
    bull = (p[4] < p[1] and x[4] > x[1] and x[4] >= p[1] and x[1] <= p[4]) or \
           (lw >= 2 * body and lw >= 0.6 * rng and x[4] >= x[3] + 0.66 * rng)
    bear = (p[4] > p[1] and x[4] < x[1] and x[4] <= p[1] and x[1] >= p[4]) or \
           (uw >= 2 * body and uw >= 0.6 * rng and x[4] <= x[3] + 0.34 * rng)
    if bull and x[4] > e and any(abs(x[3] - v) <= 0.5 * a for v in sup):
        return mk("PA", "long", x[4], x[3] - 0.1 * a, a)
    if bear and x[4] < e and any(abs(x[2] - v) <= 0.5 * a for v in res):
        return mk("PA", "short", x[4], x[2] + 0.1 * a, a)
    return None


STRATS = [strat_smc, strat_sd, strat_crt, strat_pa]   # order = priority for SL/TP choice


def analyse(c, ctx):
    """Run every strategy, merge agreeing ones into one signal (confluence score)."""
    if len(c) < 60:
        return None
    a = atr(c)
    if a <= 0:
        return None
    found = []
    for f in STRATS:
        try:
            s = f(c, a, ctx)
        except Exception:
            s = None
        if s:
            found.append(s)
    longs = [s for s in found if s["side"] == "long"]
    shorts = [s for s in found if s["side"] == "short"]
    if (longs and shorts) or not found:
        return None                                   # conflict or nothing
    grp = longs or shorts
    if len(grp) < CFG["min_confluence"]:
        return None
    s = dict(grp[0])
    s["strat"] = "+".join(g["strat"] for g in grp)
    s["score"] = len(grp)
    s["t"] = c[-1][0]
    return s


# ============================ 3. ARBITRAGE ============================
def triangular(pairs, start):
    """start -> A -> B -> start using best bid/ask. Theoretical: top of book only."""
    px = {}
    for p in pairs.values():
        if p["base"] not in CFG["arb_assets"] or p["quote"] not in CFG["arb_assets"]:
            continue
        if p["bid"] > 0 and p["ask"] > 0 and p["qvol"] >= CFG["arb_min_qvol"] and p["ask"] < p["bid"] * 1.02:
            px[(p["base"], p["quote"])] = (p["bid"], p["ask"])
    nb = {}
    for b, q in px:
        nb.setdefault(b, set()).add(q)
        nb.setdefault(q, set()).add(b)
    fee = CFG["arb_fee"]

    def step(x, a, b):
        if (b, a) in px:                              # buy b paying a, at ask
            return x / px[(b, a)][1] * (1 - fee)
        if (a, b) in px:                              # sell a for b, at bid
            return x * px[(a, b)][0] * (1 - fee)
        return None

    res = []
    for a in nb.get(start, ()):
        for b in nb.get(a, ()):
            if b in (start, a) or start not in nb.get(b, ()):
                continue
            x = 1.0
            for u, v in ((start, a), (a, b), (b, start)):
                x = step(x, u, v)
                if x is None:
                    break
            if x and x - 1 >= CFG["arb_min_profit"]:
                res.append((x - 1, (start, a, b)))
    return sorted(res, reverse=True)[:5]


# ============================ 4. PAPER TRADER ============================
def load():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {"open": [], "closed": [], "seen": []}


def save(st):
    st["seen"] = st["seen"][-2000:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, STATE_FILE)


def coin(sym):
    return sym.replace("-", "").replace("_", "").removesuffix(CFG["quote"])


def open_trade(st, ex, sym, tf, s):
    key = f"{coin(sym)}|{tf}|{s['side']}|{s['t']}"          # same coin on other exchanges = same setup
    if key in st["seen"]:
        return
    st["seen"].append(key)
    if any(coin(t["sym"]) == coin(sym) and t["side"] == s["side"] for t in st["open"]):
        return
    t = dict(ex=ex, sym=sym, tf=tf, side=s["side"], entry=s["entry"], sl=s["sl"], tp=s["tp"],
             strat=s["strat"], score=s["score"], t=s["t"])
    st["open"].append(t)
    msg = (f"{'A+ ' if s['score'] >= 2 else ''}{s['side'].upper()} {sym} {ex} {tf} [{s['strat']}] "
           f"entry {s['entry']:.6g} SL {s['sl']:.6g} TP {s['tp']:.6g}")
    log("SIGNAL " + msg)
    notify("Setup", msg)


def update_trades(st):
    still = []
    for t in st["open"]:
        c = EXCH[t["ex"]].candles(t["sym"], t["tf"])
        newer = [x for x in c if x[0] > t["t"]]
        risk = abs(t["entry"] - t["sl"])
        r = None
        for x in newer:
            if t["side"] == "long":
                hit_sl, hit_tp = x[3] <= t["sl"], x[2] >= t["tp"]
            else:
                hit_sl, hit_tp = x[2] >= t["sl"], x[3] <= t["tp"]
            if hit_sl:                                # SL first if both in one candle (conservative)
                r = -1.0
                break
            if hit_tp:
                r = CFG["rr"]
                break
        if r is None and len(newer) >= CFG["max_bars"]:
            sign = 1 if t["side"] == "long" else -1
            r = sign * (newer[-1][4] - t["entry"]) / risk
        if r is None:
            still.append(t)
            continue
        r -= 2 * CFG["paper_fee"] * t["entry"] / risk
        t["r"] = round(r, 3)
        st["closed"].append(t)
        log(f"CLOSED {t['side']} {t['sym']} {t['ex']} [{t['strat']}] R={t['r']:+}")
    st["open"] = still


def stats(st):
    cl = st["closed"]
    if not cl:
        return f"paper: 0 closed, {len(st['open'])} open"
    w = sum(1 for t in cl if t["r"] > 0)
    tot = sum(t["r"] for t in cl)
    a2 = [t for t in cl if t["score"] >= 2]
    extra = ""
    if a2:
        extra = f" | A+ only: {len(a2)} trades, {sum(t['r'] for t in a2):+.1f}R"
    return (f"paper: {len(cl)} closed, win {100 * w / len(cl):.0f}%, total {tot:+.1f}R, "
            f"{len(st['open'])} open{extra}")


def write_report(st):
    """Writes index.html (mobile-friendly results page) next to the script."""
    from html import escape
    cl = st["closed"]
    by = {}
    for t in cl:
        for name in t["strat"].split("+"):
            g = by.setdefault(name, [0, 0, 0.0])
            g[0] += 1
            g[1] += 1 if t["r"] > 0 else 0
            g[2] += t["r"]
    srows = "".join(f"<tr><td>{escape(n)}</td><td>{g[0]}</td><td>{100 * g[1] / g[0]:.0f}%</td>"
                    f"<td>{g[2]:+.1f}R</td></tr>" for n, g in sorted(by.items()))
    orows = "".join(f"<tr><td>{escape(t['side'])}</td><td>{escape(t['sym'])}</td><td>{escape(t['ex'])}</td>"
                    f"<td>{escape(t['tf'])}</td><td>{escape(t['strat'])}</td><td>{t['entry']:.6g}</td></tr>"
                    for t in st["open"])
    crows = "".join(f"<tr><td>{escape(t['side'])}</td><td>{escape(t['sym'])}</td><td>{escape(t['ex'])}</td>"
                    f"<td>{escape(t['strat'])}</td><td>{t['r']:+}</td></tr>" for t in reversed(cl[-40:]))
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="300">
<title>SignalBot</title><style>body{{font-family:sans-serif;background:#111;color:#eee;margin:12px}}
table{{border-collapse:collapse;width:100%;margin-bottom:18px;font-size:14px}}
td,th{{border-bottom:1px solid #333;padding:5px;text-align:left}}h2{{margin:14px 0 6px}}</style></head><body>
<h1>SignalBot (paper)</h1><p>{escape(stats(st))}</p><p>Updated {datetime.now():%Y-%m-%d %H:%M}</p>
<h2>By strategy</h2><table><tr><th>Strategy</th><th>Trades</th><th>Win</th><th>Total</th></tr>{srows}</table>
<h2>Open</h2><table><tr><th>Side</th><th>Pair</th><th>Ex</th><th>TF</th><th>Strat</th><th>Entry</th></tr>{orows}</table>
<h2>Last closed</h2><table><tr><th>Side</th><th>Pair</th><th>Ex</th><th>Strat</th><th>R</th></tr>{crows}</table>
</body></html>"""
    with open(os.path.join(HERE, "index.html"), "w") as fh:
        fh.write(page)


# ============================ 5. MAIN ============================
def scan_exchange(name, st):
    ex = EXCH[name]
    pairs = ex.pairs()
    if not pairs:
        log(f"[{name}] no data (network/geo-block?)")
        return
    for start in CFG["arb_start"]:
        for prof, path in triangular(pairs, start):
            log(f"ARB {name} {' > '.join(path)} > {path[0]}  +{prof * 100:.3f}% (gross, top of book)")
    cand = [(p["qvol"], s) for s, p in pairs.items()
            if p["quote"] == CFG["quote"] and p["base"] not in STABLES]
    top = [s for _, s in sorted(cand, reverse=True)[:CFG["top_n"]]]
    for sym in top:
        daily = None
        for tf in CFG["timeframes"]:
            c = ex.candles(sym, tf)
            time.sleep(0.1)
            if tf == "1h" and daily is None:
                daily = ex.candles(sym, "1d")
                time.sleep(0.1)
            s = analyse(c, {"tf": tf, "daily": daily})
            if s:
                open_trade(st, name, sym, tf, s)


def cycle(st):
    for name in CFG["exchanges"]:
        try:
            scan_exchange(name, st)
        except Exception:
            log(f"[{name}] error:\n{traceback.format_exc()}")
    try:
        update_trades(st)
    except Exception:
        log(f"update error:\n{traceback.format_exc()}")
    log(stats(st))


def main():
    st = load()
    log("bot started (paper mode, no real orders)")
    while True:
        try:
            cycle(st)
        except Exception:
            log(traceback.format_exc())
        save(st)
        try:
            write_report(st)
        except Exception:
            log(traceback.format_exc())
        if "--once" in sys.argv:
            break
        time.sleep(CFG["scan_every"])


if __name__ == "__main__":
    main()
