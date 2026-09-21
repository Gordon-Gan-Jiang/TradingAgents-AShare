"""One-off probe: fetch a real daily qfq price panel for the analyzed universe.

Not part of the app. Writes a flat CSV used by probe_factors.py.
Source: Tencent ifzq qfq kline (no auth, returns forward-adjusted OHLCV).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "tradingagents.db"
OUT_DIR = ROOT / "tradingagents" / "dataflows" / "data_cache" / "_probe"
OUT = OUT_DIR / "panel.csv"

URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    "?param={code},day,,,{n},qfq"
)
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def to_code(symbol: str) -> str | None:
    s = (symbol or "").strip().upper()
    if s.endswith(".SH"):
        return "sh" + s[:-3]
    if s.endswith(".SZ"):
        return "sz" + s[:-3]
    if s.endswith(".BJ"):
        return None  # tencent coverage unreliable for BJ
    return None


def fetch(code: str, n: int = 420) -> list[list]:
    req = urllib.request.Request(URL.format(code=code, n=n), headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode("utf-8", "ignore")
    payload = json.loads(raw)
    data = (payload.get("data") or {}).get(code) or {}
    rows = data.get("qfqday") or data.get("day") or []
    return rows


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
    con = sqlite3.connect(DB)
    syms = [
        r[0]
        for r in con.execute(
            "select distinct symbol from report_t1_outcomes order by symbol"
        )
    ]
    codes = [(s, to_code(s)) for s in syms]
    codes = [(s, c) for s, c in codes if c][:limit]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    out_rows: list[str] = ["symbol,date,open,high,low,close,volume"]
    failed: list[str] = []

    for i, (sym, code) in enumerate(codes, 1):
        rows = []
        for attempt in range(3):
            try:
                rows = fetch(code)
                if rows:
                    break
            except Exception:
                time.sleep(0.4 * (attempt + 1))
        if not rows:
            fail += 1
            failed.append(sym)
        else:
            ok += 1
            for r in rows:
                # tencent qfqday: [date, open, close, high, low, volume]
                if len(r) < 6:
                    continue
                d, o, c_, h, l, v = r[0], r[1], r[2], r[3], r[4], r[5]
                out_rows.append(f"{sym},{d},{o},{h},{l},{c_},{v}")
        if i % 25 == 0:
            print(f"  {i}/{len(codes)} ok={ok} fail={fail}", flush=True)
        time.sleep(0.12)

    OUT.write_text("\n".join(out_rows), encoding="utf-8")
    print(f"DONE ok={ok} fail={fail} rows={len(out_rows)-1} -> {OUT}")
    if failed:
        print("failed:", ",".join(failed[:40]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
