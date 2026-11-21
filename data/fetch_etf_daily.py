#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""拉 ETF 日线，建成策略要用的 sqlite 库。不需要任何 token。

数据源：东方财富 push2his 公开 K 线接口，一次返回全历史。
产出：data/market.db，表 etf_daily(ts_code, trade_date, open, high, low, close, vol, amount)。

用法
----
python data/fetch_etf_daily.py                       # 默认两只 ETF，全历史，写 data/market.db
python data/fetch_etf_daily.py --end 20261120        # 指定结束日
python data/fetch_etf_daily.py --codes 510300.SH 518880.SH
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

DEFAULT_CODES = ["510300.SH", "510500.SH"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
DDL = """
create table if not exists etf_daily(
    ts_code    text not null,
    trade_date text not null,
    open real, high real, low real, close real,
    vol real, amount real,
    primary key (ts_code, trade_date)
)
"""


def secid(code: str) -> str:
    """600xxx/5xxxxx 属于上交所（1.），其余深交所（0.）。"""
    num = code.split(".")[0]
    market = "1" if num[0] in "56" else "0"
    return f"{market}.{num}"


def fetch(code: str, begin: str, end: str, retries: int = 3) -> list[tuple]:
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid(code)}&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57"
        f"&klt=101&fqt=0&beg={begin}&end={end}"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
    )
    last = None
    for i in range(retries):
        try:
            raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
            data = json.loads(raw).get("data") or {}
            klines = data.get("klines") or []
            if not klines:
                raise RuntimeError(f"{code} 返回空数据（代码写错？停牌？）")
            out = []
            for line in klines:
                p = line.split(",")
                # 日期,开,收,高,低,成交量,成交额
                out.append(
                    (code, p[0].replace("-", ""), float(p[1]), float(p[3]),
                     float(p[4]), float(p[2]), float(p[5]), float(p[6]))
                )
            return out
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as e:
            last = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise SystemExit(f"拉 {code} 失败：{last}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="+", default=DEFAULT_CODES)
    ap.add_argument("--begin", default="20150101")
    ap.add_argument("--end", default=time.strftime("%Y%m%d"))
    ap.add_argument("--out", default=str(ROOT / "data" / "market.db"))
    a = ap.parse_args()

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(out))
    con.execute(DDL)
    total = 0
    for code in a.codes:
        rows = fetch(code, a.begin, a.end)
        con.executemany(
            "insert or replace into etf_daily"
            "(ts_code,trade_date,open,high,low,close,vol,amount) values(?,?,?,?,?,?,?,?)",
            rows,
        )
        con.commit()
        total += len(rows)
        print(f"{code}  {rows[0][1]} → {rows[-1][1]}  {len(rows)} 行  收盘 {rows[-1][5]}")
    con.close()
    print(f"合计 {total} 行 → {out}")


if __name__ == "__main__":
    main()
