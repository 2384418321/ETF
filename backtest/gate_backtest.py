#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ETF 趋势闸门的账户级回测：真的按股数、现金、手续费撮合一遍。

为什么不用「净值序列相乘」
--------------------------
把两条腿的净值各自算出来再平均，隐含了「永不调仓、权重自由漂移」；
而「每天再平衡回 50/50」又是另一个极端。两者能差出 20 个百分点。
这里直接模拟一个现金 + 持仓的账户：只在信号翻转那天成交，成交按次日开盘价，
成交后自然漂移，不每天动它——和真实下单一致。

口径
----
信号   第 D 日收盘 > MA(n) × (1+band) → 第 D+1 日开盘调成目标仓位
定价   买入/卖出用 D+1 开盘价，当日收盘价做盯市
仓位   每只目标 slot（默认 40%）× 当时总资产，不足一手舍去
费用   佣金 0.025%、最低 5 元；ETF 免印花税与过户费
调仓带 目标与现有股数的差额市值 < 总资产 1% 就不动

用法
----
export ETF_DB=/path/to/market.db
python backtest/gate_backtest.py
python backtest/gate_backtest.py --grid
python backtest/gate_backtest.py --scale 0.8      # 降杠杆
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

CODES = ["510300.SH", "510500.SH"]
LAST_YEAR = ("20251121", "20261120")


def resolve_db(cli: str | None) -> Path:
    for p in (cli, os.environ.get("ETF_DB")):
        if p:
            return Path(p)
    raise SystemExit("没有数据库路径：用 --db 传入，或设置环境变量 ETF_DB")


def load(db: Path, codes=CODES) -> dict[str, pd.DataFrame]:
    con = sqlite3.connect(str(db))
    q = ("select ts_code, trade_date, open, high, low, close from etf_daily "
         "where ts_code in (%s) order by ts_code, trade_date" % ",".join("?" * len(codes)))
    d = pd.read_sql(q, con, params=codes)
    d["trade_date"] = d["trade_date"].astype(str)
    out = {}
    for c, g in d.groupby("ts_code"):
        g = g.sort_values("trade_date").set_index("trade_date")
        out[c] = g[["open", "high", "low", "close"]].astype(float)
    return out


def signals(px: pd.DataFrame, n: int, band: float) -> pd.Series:
    ma = px["close"].rolling(n).mean()
    return (px["close"] > ma * (1 + band)).fillna(False)


def align(data: dict[str, pd.DataFrame]) -> tuple[list[str], dict[str, pd.DataFrame]]:
    dates = sorted(set().union(*[set(d.index) for d in data.values()]))
    out = {}
    for c, d in data.items():
        f = d.reindex(dates)
        f["close"] = f["close"].ffill()
        out[c] = f
    return dates, out


def simulate(data: dict[str, pd.DataFrame], n: int, band: float, slot: float,
             capital: float = 100_000.0, scale: float = 1.0,
             fee_rate: float = 0.00025, fee_min: float = 5.0,
             lot: int = 100, band_trade: float = 0.01) -> pd.DataFrame:
    dates, data = align(data)
    sig = {c: signals(data[c], n, band) for c in data}
    opens = {c: data[c]["open"].to_numpy(float) for c in data}
    closes = {c: data[c]["close"].to_numpy(float) for c in data}
    bull = {c: sig[c].to_numpy(bool) for c in data}
    pos = {c: 0 for c in data}
    state = {c: False for c in data}      # 上次成交时对应的信号态
    cash = capital
    rows = []

    for i, d in enumerate(dates):
        if i > 0:
            equity = cash + sum(pos[c] * closes[c][i - 1] for c in data)
            for c in data:
                want_state = bool(bull[c][i - 1])
                if want_state == state[c]:
                    continue                       # 信号没翻转就不动，让权重自然漂
                px_open = opens[c][i]
                if not np.isfinite(px_open) or px_open <= 0:
                    continue
                want = (math.floor(equity * slot * scale / px_open / lot) * lot
                        if want_state else 0)
                delta = want - pos[c]
                state[c] = want_state
                if delta == 0 or abs(delta * px_open) < equity * band_trade:
                    continue
                fee = max(abs(delta) * px_open * fee_rate, fee_min)
                cash -= delta * px_open + fee
                pos[c] = want
        mv = sum(pos[c] * closes[c][i] for c in data)
        rows.append({"date": d, "cash": cash, "mv": mv, "equity": cash + mv})

    return pd.DataFrame(rows).set_index("date")


def stats(eq: pd.Series, capital: float, start: str | None = None) -> dict:
    e = eq.copy()
    if start:
        e = e[e.index >= start]
    peak = e.cummax()
    dd = (e / peak - 1).min()
    yr = {}
    for y, g in e.groupby([int(i) // 10000 for i in e.index]):
        before = eq[eq.index < g.index[0]]
        base = float(before.iloc[-1]) if len(before) else capital
        yr[y] = (float(g.iloc[-1]) / base - 1) * 100
    sub = e[(e.index >= LAST_YEAR[0]) & (e.index <= LAST_YEAR[1])]
    base_py = float(eq[eq.index < LAST_YEAR[0]].iloc[-1]) if (eq.index < LAST_YEAR[0]).any() else capital
    py = (float(sub.iloc[-1]) / base_py - 1) * 100 if len(sub) else np.nan
    return {
        "years": {k: round(v, 1) for k, v in yr.items()},
        "past_year": round(py, 1),
        "total": round((float(e.iloc[-1]) / float(e.iloc[0]) - 1) * 100, 1),
        "mdd": round(dd * 100, 1),
    }


def show(tag: str, r: dict) -> None:
    ys = " / ".join(f"{y}:{v:+.1f}" for y, v in r["years"].items())
    print(f"{tag:<22} {ys} | 近一年 {r['past_year']:+.1f}% | 总 {r['total']:+.1f}% | 回撤 {r['mdd']:.1f}%")


def buy_hold(data: dict[str, pd.DataFrame], slot: float, capital: float = 100_000.0) -> pd.Series:
    dates, data = align(data)
    pos, cash = {}, capital
    for c in data:
        px = float(data[c]["open"].iloc[0])
        sh = math.floor(capital * slot / px / 100) * 100
        pos[c] = sh
        cash -= sh * px
    out = [cash + sum(pos[c] * float(data[c]["close"].iloc[i]) for c in data)
           for i in range(len(dates))]
    return pd.Series(out, index=dates)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--config")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--start", default=None, help="只从这一年统计，如 20220101")
    a = ap.parse_args()

    cfg = {"ma": 20, "band": 0.02, "slot": 0.40}
    if a.config:
        cfg.update(json.loads(Path(a.config).read_text(encoding="utf-8")))

    data = load(resolve_db(a.db))
    first = min(d.index[0] for d in data.values())
    last = max(d.index[-1] for d in data.values())
    print(f"样本 {first} – {last}   " + "  ".join(data))

    eq = simulate(data, cfg["ma"], cfg["band"], cfg["slot"], scale=a.scale)["equity"]
    show(f"MA{cfg['ma']}+{cfg['band']:.1%}×{a.scale:g}", stats(eq, 100_000, a.start))
    show("等权买入持有", stats(buy_hold(data, cfg["slot"]), 100_000, a.start))

    if a.grid:
        print("\n参数网格（同一账户模拟）")
        for n in (15, 20, 25, 30, 40, 60):
            for b in (0.0, 0.01, 0.02, 0.03, 0.04):
                e = simulate(data, n, b, cfg["slot"], scale=a.scale)["equity"]
                show(f"MA{n}+{b:.0%}", stats(e, 100_000, a.start))


if __name__ == "__main__":
    main()
