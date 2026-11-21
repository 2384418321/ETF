#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ETF 趋势闸门 —— 用一条均线决定持仓还是空仓。

规则
----
信号   收盘价 > MA(n) × (1 + band)  → 多头；否则空仓
执行   第 D 个交易日收盘出信号，D+1 开盘调仓
仓位   每只目标 slot（默认 40%）× 总资产；两只合计 80%，其余常备现金
整手   ETF 最小 100 份，不足一手舍去

用法
----
export ETF_DB=/path/to/market.db          # 或 --db 传入
python strategy/etf_gate.py               # 最新交易日的信号与目标股数
python strategy/etf_gate.py --date 20261120
python strategy/etf_gate.py --config strategy/config.json --capital 100000
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

DEFAULT_CONF = {
    "codes": ["510300.SH", "510500.SH"],
    "names": {"510300.SH": "沪深300ETF", "510500.SH": "中证500ETF"},
    "ma": 20,
    "band": 0.02,
    "slot": 0.40,
    "lot": 100,
    "fee_rate": 0.00025,
    "fee_min": 5.0,
}


def resolve_db(cli: str | None) -> Path:
    """找数据库：--db > 环境变量 ETF_DB > 仓库自带的 data/market.db。"""
    if cli:
        return Path(cli)
    env = os.environ.get("ETF_DB")
    if env:
        return Path(env)
    local = Path(__file__).resolve().parent.parent / "data" / "market.db"
    if local.exists():
        return local
    raise SystemExit(
        "找不到行情库。先跑 `python data/fetch_etf_daily.py` 生成 data/market.db，"
        "或用 --db 指定、或设置环境变量 ETF_DB"
    )


def connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise SystemExit(f"找不到数据库 {db}")
    return sqlite3.connect(str(db))


def load(con: sqlite3.Connection, codes: list[str]) -> pd.DataFrame:
    q = (
        "select ts_code, trade_date, open, high, low, close "
        "from etf_daily where ts_code in (%s) order by ts_code, trade_date"
        % ",".join("?" * len(codes))
    )
    d = pd.read_sql(q, con, params=codes)
    d["trade_date"] = d["trade_date"].astype(str)
    if d.empty:
        raise SystemExit("etf_daily 里没有这些代码的行情，检查表结构与代码格式")
    return d


def gate_frame(d: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """逐只算 MA、门槛、信号。每只 ETF 用自己的一条均线，不共用。"""
    out = []
    for code, g in d.groupby("ts_code", sort=False):
        g = g.sort_values("trade_date").copy()
        g["ma"] = g["close"].rolling(cfg["ma"]).mean()
        g["thr"] = g["ma"] * (1 + cfg["band"])
        g["bull"] = (g["close"] > g["thr"]).fillna(False)
        out.append(g[["ts_code", "trade_date", "close", "ma", "thr", "bull"]])
    return pd.concat(out, ignore_index=True)


def plan(frame: pd.DataFrame, asof: str, capital: float, cfg: dict) -> pd.DataFrame:
    snap = frame[frame["trade_date"] == asof].set_index("ts_code")
    if snap.empty:
        raise SystemExit(f"{asof} 没有行情")
    rows = []
    for code in cfg["codes"]:
        if code not in snap.index:
            raise SystemExit(f"{asof} 缺少 {code} 的行情")
        r = snap.loc[code]
        if r["bull"]:
            budget = capital * cfg["slot"]
            shares = math.floor(budget / r["close"] / cfg["lot"]) * cfg["lot"]
            value = shares * float(r["close"])
        else:
            shares, value = 0, 0.0
        rows.append(
            {
                "代码": code,
                "名称": cfg["names"].get(code, ""),
                "收盘": round(float(r["close"]), 4),
                "MA": round(float(r["ma"]), 4) if pd.notna(r["ma"]) else None,
                "门槛": round(float(r["thr"]), 4) if pd.notna(r["thr"]) else None,
                "距门槛%": round((float(r["close"]) / float(r["thr"]) - 1) * 100, 2)
                if pd.notna(r["thr"])
                else None,
                "信号": "多头" if r["bull"] else "空仓",
                "目标股数": shares,
                "估算市值": round(value, 2),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="sqlite 数据库路径（含 etf_daily 表）")
    ap.add_argument("--config", help="JSON 配置文件，覆盖默认参数")
    ap.add_argument("--date", help="信号日 YYYYMMDD，缺省取最新")
    ap.add_argument("--capital", type=float, default=100_000.0, help="总资产，默认 10 万")
    a = ap.parse_args()

    cfg = dict(DEFAULT_CONF)
    if a.config:
        cfg.update(json.loads(Path(a.config).read_text(encoding="utf-8")))

    con = connect(resolve_db(a.db))
    frame = gate_frame(load(con, cfg["codes"]), cfg)
    dates = sorted(frame["trade_date"].unique())
    asof = a.date or dates[-1]
    if asof not in dates:
        raise SystemExit(f"{asof} 不是交易日；最近交易日 {dates[-1]}")

    p = plan(frame, asof, a.capital, cfg)
    print(f"信号日 {asof}（下一交易日开盘执行）  总资产 {a.capital:,.2f}  每只预算 {a.capital * cfg['slot']:,.2f}")
    print(p.to_string(index=False, justify="left"))
    gross = p["估算市值"].sum()
    print(f"合计目标仓位 {gross / a.capital * 100:.1f}%   现金 {a.capital - gross:,.2f}")
    print("提示：信号日收盘算出来的是「下一交易日开盘」的动作，不是当天。", file=sys.stderr)


if __name__ == "__main__":
    main()
