#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""策略自进化循环 —— 把「该不该改」变成可以逐条核对的门。

每天复盘跑一次。它做三件事：
  1. 算事实：从台账/回测里读出每个策略的实际表现
  2. 下诊断：命中哪几条现象，每条现象对应哪些候选改法
  3. 过闸门：这条改动现在允不允许动，卡在哪一条

它默认**不改任何东西**。只有 `--apply "<参数>=<新值>"` 且九道闸全过，才写回
strategy_registry.json —— 并且要求人当场写下「为什么改」和「什么算改错」。

用法：
    python tools/evolve_cycle.py                      # 只体检
    python tools/evolve_cycle.py --detail             # 带每期明细
    python tools/evolve_cycle.py --apply "band=0.025" \\
        --why "2026Q3 震荡期反复被打脸，缓冲不足" \\
        --wrong-if "20 个交易日后累计跑输基线 3 个百分点" \\
        --ack-forbidden
    python tools/evolve_cycle.py --rollback etf_gate  # 回滚上次变更

九道闸门（对应 README）
    G1 冷却期       距上次变更 ≥ cooldown_days 个交易日
    G2 同参数频次   60 个交易日内同一参数改动 < max_changes_per_60d 次
    G3 候选网格     参数已登记，新值在 grid 内
    G4 确有变化     新值不等于现值
    G5 冻结项       参数名不出现在 frozen 条文里
    G6 禁止方向     不落在 forbidden_values，且人显式确认未触犯文本禁令
    G7 改动理由     必须填 --why 与 --wrong-if，不接受模板生成
    G8 观察期       上一处改动的 observe_until 未过不许再动
    G9 样本外复核   最近 250 个交易日上，新参数不劣化（收益不低于基线、
                    回撤恶化不超过 oos_max_mdd_worse_pp）

交易日口径
    优先用 trade_cal 表；表缺失时退回 etf_daily 的 distinct trade_date。
    两条都拿不到时（比如空库），冷却与频次会 fail-closed，而不是当成 0 放过。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REGISTRY = ROOT / "strategy_registry.json"
DB = ROOT / "data" / "market.db"
LOG = ROOT / "reflections" / "evolve_cycle.jsonl"
OUT_DIR = ROOT / "reflections"

TZ = timezone(timedelta(hours=8))
OOS_DAYS = 250          # 样本外复核窗口：最近 250 个交易日
MIN_TEXT = 12           # --why / --wrong-if 的最短长度，防「随便写两个字」


def today() -> str:
    return datetime.now(TZ).strftime("%Y%m%d")


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 交易日历
def _ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def calendar_source() -> str:
    """告诉人今天是按哪张表算的交易日。看报告第一眼就该知道。"""
    if not DB.exists():
        return "无数据库"
    try:
        con = _ro()
        n = con.execute("select count(*) from trade_cal where is_open=1").fetchone()[0]
        con.close()
        if n:
            return f"trade_cal（{n} 天）"
    except sqlite3.Error:
        pass
    try:
        con = _ro()
        n = con.execute("select count(distinct trade_date) from etf_daily").fetchone()[0]
        con.close()
        if n:
            return f"etf_daily 兜底（{n} 天，trade_cal 缺失）"
    except sqlite3.Error:
        pass
    return "不可用"


def open_days(a: str, b: str) -> int:
    """a 之后到 b 为止（含 b、不含 a）的交易日数。拿不到日历返回 -1，由调用方 fail-closed。"""
    if not DB.exists():
        return -1
    try:
        con = _ro()
        try:
            n = con.execute(
                "select count(*) from trade_cal "
                "where cal_date > ? and cal_date <= ? and is_open=1", (a, b)
            ).fetchone()[0]
        except sqlite3.Error:
            n = con.execute(
                "select count(distinct trade_date) from etf_daily "
                "where trade_date > ? and trade_date <= ?", (a, b)
            ).fetchone()[0]
        con.close()
        return int(n or 0)
    except sqlite3.Error:
        return -1


def plus_trading_days(n: int) -> str:
    """从今天往后数 n 个交易日。日历里没有未来日期时按工作日近似。"""
    if DB.exists():
        try:
            con = _ro()
            try:
                rows = con.execute(
                    "select cal_date from trade_cal where cal_date > ? and is_open=1 "
                    "order by cal_date limit ?", (today(), n)
                ).fetchall()
            except sqlite3.Error:
                rows = con.execute(
                    "select distinct trade_date from etf_daily where trade_date > ? "
                    "order by trade_date limit ?", (today(), n)
                ).fetchall()
            con.close()
            if len(rows) >= n:
                return rows[-1][0]
        except sqlite3.Error:
            pass
    # 兜底：按工作日推，忽略节假日。宁可多锁几天，不可漏锁。
    d = datetime.strptime(today(), "%Y%m%d")
    left = n
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d.strftime("%Y%m%d")


def last_n_start(n: int) -> str | None:
    """最近 n 个交易日的起点（含）。用于样本外复核窗口。"""
    if not DB.exists():
        return None
    try:
        con = _ro()
        try:
            rows = con.execute(
                "select cal_date from trade_cal where is_open=1 "
                "order by cal_date desc limit ?", (n,)
            ).fetchall()
        except sqlite3.Error:
            rows = con.execute(
                "select distinct trade_date from etf_daily "
                "order by trade_date desc limit ?", (n,)
            ).fetchall()
        con.close()
        return rows[-1][0] if len(rows) >= n else (rows[-1][0] if rows else None)
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------- 读台账
def read_nav_book(path: Path, sheet: str) -> tuple[list[tuple[str, float]], str]:
    """返回 [(日期YYYYMMDD, 总资产)] 与口径说明。列按名字认，不按位置。"""
    from openpyxl import load_workbook

    if not path.exists():
        return [], f"找不到台账 {path.name}"
    wb = load_workbook(path, read_only=True, data_only=True)
    if sheet not in wb.sheetnames:
        return [], f"{path.name} 没有 sheet「{sheet}」"
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], "sheet 是空的"

    head = [str(c).strip() if c is not None else "" for c in rows[0]]

    def find(*keys: str) -> int:
        for i, h in enumerate(head):
            for k in keys:
                if k in h:
                    return i
        return -1

    ci_date = find("日期", "信号日")
    ci_total = find("总资产", "组合", "净值金额")
    ci_nav = find("累计净值", "净值")
    if ci_date < 0:
        return [], "表头里找不到日期列"

    out: list[tuple[str, float]] = []
    for r in rows[1:]:
        if not r or r[ci_date] is None:
            continue
        d = str(r[ci_date]).strip().replace("-", "")[:8]
        if not d.isdigit():
            continue
        v = None
        if ci_total >= 0 and r[ci_total] not in (None, ""):
            try:
                v = float(r[ci_total])
            except (TypeError, ValueError):
                v = None
        if v is None and ci_nav >= 0 and r[ci_nav] not in (None, ""):
            try:
                v = float(r[ci_nav])
            except (TypeError, ValueError):
                v = None
        if v is None:
            continue
        out.append((d, v))
    out.sort(key=lambda x: x[0])
    return out, f"读 {path.name}/{sheet} {len(out)} 行"


def nav_series(navs: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """把「总资产」或「净值」都归一到 1 起点，便于统一算收益率。"""
    if not navs:
        return []
    base = navs[0][1]
    if base <= 0:
        return []
    return [(d, v / base) for d, v in navs]


def period_rets(series: list[tuple[str, float]]) -> list[tuple[str, float]]:
    out = []
    for i in range(1, len(series)):
        prev = series[i - 1][1]
        if prev > 0:
            out.append((series[i][0], series[i][1] / prev - 1.0))
    return out


def metrics(series: list[tuple[str, float]], rets: list[tuple[str, float]]) -> dict:
    m: dict = {}
    m["n_periods"] = len(rets)
    if not rets:
        return m
    m["total"] = series[-1][1] - 1.0
    peak, mdd = -1e9, 0.0
    for _, v in series:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    m["mdd"] = mdd
    tail = [r for _, r in rets[-20:]]
    m["mean20"] = sum(tail) / len(tail)
    m["win20"] = sum(1 for r in tail if r > 0) / len(tail)
    # 连续同向（尾部）
    streak, sign = 0, None
    for _, r in reversed(rets):
        s = 1 if r > 0 else (-1 if r < 0 else 0)
        if s == 0:
            break
        if sign is None:
            sign, streak = s, 1
        elif s == sign:
            streak += 1
        else:
            break
    m["tail_streak"] = streak * (sign or 0)
    return m


# ---------------------------------------------------------------- 基准与影子曲线
def benchmark_5050(start: str, end: str) -> float | None:
    """510300 + 510500 等权买入持有，同区间总收益。"""
    if not DB.exists():
        return None
    try:
        con = _ro()
        cur = con.execute(
            "select ts_code, trade_date, close from etf_daily "
            "where ts_code in ('510300.SH','510500.SH') and trade_date between ? and ? "
            "order by trade_date", (start, end))
        rows = cur.fetchall()
        con.close()
    except sqlite3.Error:
        return None
    by: dict[str, dict[str, float]] = {}
    for code, d, c in rows:
        if c:
            by.setdefault(code, {})[d] = float(c)
    if len(by) < 2:
        return None
    tot = 0.0
    for code, px in by.items():
        ds = sorted(px)
        if len(ds) < 2:
            return None
        tot += px[ds[-1]] / px[ds[0]] - 1.0
    return tot / 2


def _load_backtest():
    """动态加载回测模块：evolve 本身不强制依赖 pandas，用到时才拉。"""
    p = ROOT / "backtest" / "gate_backtest.py"
    spec = importlib.util.spec_from_file_location("gate_backtest", p)
    if spec is None or spec.loader is None:
        raise ImportError(f"加载不了 {p}")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def replay(params: dict, start: str | None) -> dict:
    """用给定参数重跑账户级回测，返回区间统计。缺依赖会抛 ImportError。"""
    bt = _load_backtest()
    data = bt.load(DB)
    eq = bt.simulate(data, int(params["ma"]), float(params["band"]),
                     float(params["slot"]))["equity"]
    return bt.stats(eq, 100_000.0, start)


def shadow_series(cfg: dict) -> tuple[list[tuple[str, float]], int, str]:
    """无台账时，用回测曲线当替身：让 diagnose 有东西可看。

    返回 (净值序列, 连续空仓交易日, 口径说明)。缺 pandas/numpy 时返回空。
    """
    try:
        bt = _load_backtest()
        data = bt.load(DB)
        sim = bt.simulate(data, int(cfg["ma"]), float(cfg["band"]), float(cfg["slot"]))
    except Exception as e:  # 缺依赖、缺数据、脚本改动都算
        return [], 0, f"影子回测不可用（{type(e).__name__}: {e}）"
    sim = sim.astype(float)
    series = [(str(d), float(v)) for d, v in sim["equity"].items()]
    base = series[0][1] if series else 0.0
    series = [(d, v / base) for d, v in series] if base > 0 else []
    idle = 0
    for v in reversed(sim["mv"].tolist()):
        if v == 0:
            idle += 1
        else:
            break
    return series, idle, f"影子回测 {len(series)} 个交易日（参数取自登记表，非台账）"


# ---------------------------------------------------------------- 诊断
FIX_BOOK = {
    "streak_loss": {
        "title": "连续同向亏损",
        "look": "最近连续 3 期以上同向为负",
        "candidates": "①只在 3 期以上才考虑动参数；②先查是不是基准本来就在跌（超额为正就别动）",
        "judge": "同一段区间上，把候选参数重跑一遍，看是不是只改了亏损期、没改盈利期；"
                 "只有全部区间都不劣化才算改对",
    },
    "mdd_breach": {
        "title": "回撤越过容忍线",
        "look": "最大回撤比登记的 max_dd_tolerance 更深",
        "candidates": "①降 slot（等比降杠杆）②放宽 band（少交易）③缩 ma（更快离场）",
        "judge": "重跑后回撤必须回到容忍线以内，且总收益下降不超过 1/3；"
                 "只降回撤不看收益等于把策略阉了",
    },
    "idle_too_long": {
        "title": "长期空仓",
        "look": "连续 20 个交易日目标仓位为 0",
        "candidates": "①缩 ma（更快重新入场）②降 band（降低入场门槛）",
        "judge": "重跑五年，看空仓率是否下降、同时总收益没有变差；"
                 "只提高交易次数不算改进",
    },
    "underperform": {
        "title": "跑输基准",
        "look": "同区间总收益比基准低 5 个百分点以上",
        "candidates": "先确认基准是谁；若是买入持有，跑输本身就是这套规则的设计代价，不改",
        "judge": "只有在「回撤也同时更差」时才允许动；单纯跑输不构成改动理由",
    },
}


def diagnose(metrics: dict, reg: dict, extra: dict, gate_cfg: dict) -> list[dict]:
    hits: list[dict] = []
    streak = metrics.get("tail_streak", 0)
    if streak <= -gate_cfg["min_streak"]:
        hits.append({"key": "streak_loss", "evidence": f"连续 {abs(streak)} 期为负"})

    mdd = metrics.get("mdd")
    tol = reg.get("max_dd_tolerance")
    if mdd is not None and tol is not None and mdd < tol:
        hits.append({"key": "mdd_breach",
                     "evidence": f"回撤 {mdd:.2%} 深于容忍线 {tol:.2%}"})

    idle = extra.get("idle_days", 0)
    if idle >= 20:
        hits.append({"key": "idle_too_long", "evidence": f"连续空仓 {idle} 个交易日"})

    bench = extra.get("bench_total")
    tot = metrics.get("total")
    if bench is not None and tot is not None and (tot - bench) <= -0.05:
        hits.append({"key": "underperform",
                     "evidence": f"同期 {tot:.2%} vs 基准 {bench:.2%}"})
    return hits


# ---------------------------------------------------------------- 闸门
def load_log() -> list[dict]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def gate(strategy: dict, log: list[dict], param: str, new_value,
         gate_cfg: dict, why: str = "", wrong_if: str = "",
         ack_forbidden: bool = False) -> list[tuple[str, bool, str]]:
    """返回 [(闸门编号, 是否通过, 说明)]。全通过才允许落地。"""
    sid = strategy["id"]
    mine = [r for r in log if r.get("strategy") == sid and r.get("action") == "apply"]
    checks: list[tuple[str, bool, str]] = []

    # G1 冷却（日历不可用时 fail-closed）
    if mine:
        gap = open_days(mine[-1]["date"], today())
        if gap < 0:
            checks.append(("G1 冷却期", False,
                           "算不出交易日间隔（trade_cal 与 etf_daily 都不可用），拒绝改动"))
        else:
            ok = gap >= gate_cfg["cooldown_days"]
            checks.append(("G1 冷却期", ok,
                           f"距上次变更 {gap} 个交易日（要求 ≥{gate_cfg['cooldown_days']}）"))
    else:
        checks.append(("G1 冷却期", True, "从未变更过，不受冷却期限制"))

    # G2 同参数 60 个交易日内的次数
    recent = []
    unknown = False
    for r in mine:
        gap = open_days(r["date"], today())
        if gap < 0:
            unknown = True
            continue
        if gap <= 60:
            recent.append(r)
    same = [r for r in recent if r.get("param") == param]
    if unknown:
        checks.append(("G2 同参数频次", False,
                       "有变更记录算不出间隔，无法确认频次，拒绝改动"))
    else:
        checks.append(("G2 同参数频次", len(same) < gate_cfg["max_changes_per_60d"],
                       f"60 个交易日内该参数改过 {len(same)} 次"
                       f"（上限 {gate_cfg['max_changes_per_60d']}）"))

    # G3 参数已登记且在候选网格内
    spec = strategy.get("params", {}).get(param)
    if spec is None:
        checks.append(("G3 候选网格", False, f"「{param}」不在登记表里，视为冻结参数"))
        return checks
    in_grid = new_value in spec["grid"]
    checks.append(("G3 候选网格", in_grid,
                   f"新值 {new_value} {'在' if in_grid else '不在'}候选网格 {spec['grid']}"))

    # G4 确有变化
    checks.append(("G4 确有变化", new_value != spec["value"],
                   f"现值 {spec['value']} → 目标 {new_value}"))

    # G5 冻结项：参数名出现在 frozen 条文里就不许走这条路
    frozen = [str(x) for x in strategy.get("frozen", [])]
    hit = [f for f in frozen if param in f]
    checks.append(("G5 冻结项", not hit,
                   "参数未出现在冻结条文" if not hit else f"命中冻结条文：{hit[0]}"))

    # G6 禁止方向：能机检的硬拦，文本禁令要人显式确认
    fv = spec.get("forbidden_values", [])
    bad = new_value in fv
    if bad:
        checks.append(("G6 禁止方向", False, f"{param}={new_value} 在禁止取值 {fv} 里"))
    elif not ack_forbidden:
        checks.append(("G6 禁止方向", False,
                       "未确认未触犯文本禁令（登记表里那几条）。确认后加 --ack-forbidden"))
    else:
        checks.append(("G6 禁止方向", True, "未取禁用值，人已确认未触犯文本禁令"))

    # G7 改动理由必须是人写的
    ok_why = len((why or "").strip()) >= MIN_TEXT
    ok_wrong = len((wrong_if or "").strip()) >= MIN_TEXT
    checks.append(("G7 改动理由", ok_why and ok_wrong,
                   f"--why {len((why or '').strip())} 字 / --wrong-if "
                   f"{len((wrong_if or '').strip())} 字（各需 ≥{MIN_TEXT}）"))

    # G8 观察期：上一处改动没观察够，不许再动
    if mine:
        until = str(mine[-1].get("observe_until") or "")
        if not until:
            checks.append(("G8 观察期", False, "上次变更没写 observe_until，视为观察未完成"))
        else:
            ok = today() >= until
            checks.append(("G8 观察期", ok,
                           f"上次变更观察至 {until}，今天 {today()}"
                           + ("，已过" if ok else "，未过")))
    else:
        checks.append(("G8 观察期", True, "无待观察变更"))

    return checks


def oos_check(strategy: dict, param: str, new_value, gate_cfg: dict) -> tuple[bool, str]:
    """G9：最近 250 个交易日上，新参数不许劣化。缺依赖就拒绝，不放行。"""
    start = last_n_start(OOS_DAYS)
    if start is None:
        return False, "没有行情库，无法复核"
    base_params = {k: v["value"] for k, v in strategy["params"].items()}
    cand_params = dict(base_params)
    cand_params[param] = new_value
    try:
        b = replay(base_params, start)
        c = replay(cand_params, start)
    except ImportError as e:
        return False, f"缺 pandas/numpy，复核跑不了（{e}）——先 pip install -r requirements.txt"
    except Exception as e:
        return False, f"复核跑挂了（{type(e).__name__}: {e}）"

    edge = c["total"] - b["total"]
    mdd_worse = b["mdd"] - c["mdd"]          # 正数 = 回撤更深
    ok_edge = edge >= gate_cfg["oos_min_edge_pp"]
    ok_mdd = mdd_worse <= gate_cfg["oos_max_mdd_worse_pp"]
    note = (f"近 {OOS_DAYS} 交易日 {start} 起：收益 {b['total']:+.1f}% → {c['total']:+.1f}%"
            f"（{edge:+.1f}pp，需 ≥{gate_cfg['oos_min_edge_pp']:+.1f}）｜"
            f"回撤 {b['mdd']:.1f}% → {c['mdd']:.1f}%"
            f"（恶化 {mdd_worse:+.1f}pp，上限 {gate_cfg['oos_max_mdd_worse_pp']:.1f}）")
    return ok_edge and ok_mdd, note


# ---------------------------------------------------------------- 报告
def fmt_pct(x):
    return f"{x:+.2%}" if isinstance(x, (int, float)) else "—"


def run(detail: bool = False) -> None:
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    log = load_log()
    lines: list[str] = []
    lines.append(f"# 策略体检 {today()}")
    lines.append("")
    lines.append(f"生成时间 {now_str()}　登记表 v{reg['version']}　变更记录 {len(log)} 条")
    lines.append(f"交易日历 {calendar_source()}")
    lines.append("")

    for s in reg["strategies"]:
        lines.append(f"## {s['name']}（`{s['id']}`，{s['status']}）")
        lines.append("")
        extra: dict = {}
        m: dict = {}

        if s.get("book"):
            navs, note = read_nav_book(ROOT / s["book"], s["sheet"])
            lines.append(f"- 数据：{note}")
            if navs:
                series = nav_series(navs)
                rets = period_rets(series)
                m = metrics(series, rets)
                extra["bench_total"] = benchmark_5050(series[0][0], series[-1][0])
                extra["idle_days"] = last_idle_days(ROOT / s["book"], s["sheet"])
                lines.append(f"- 区间：{series[0][0]} → {series[-1][0]}，{m['n_periods']} 个观察期")
                lines.append(f"- 累计 {fmt_pct(m.get('total'))}　最大回撤 {fmt_pct(m.get('mdd'))}　"
                             f"近 20 期均值 {fmt_pct(m.get('mean20'))}　近 20 期胜率 "
                             f"{(m.get('win20') or 0):.0%}　尾部连续 {m.get('tail_streak', 0):+d} 期")
                if extra["bench_total"] is not None:
                    edge = m["total"] - extra["bench_total"]
                    lines.append(f"- 基准（50/50 买入持有）{fmt_pct(extra['bench_total'])}　"
                                 f"超额 {fmt_pct(edge)}")
                if extra["idle_days"]:
                    lines.append(f"- 连续空仓 {extra['idle_days']} 个交易日（按净值表「闸门仓位」列）")
                if detail:
                    lines.append("")
                    lines.append("| 日期 | 净值 | 期收益 |")
                    lines.append("|---|---|---|")
                    rmap = dict(rets)
                    for d, v in series[-20:]:
                        lines.append(f"| {d} | {v:.4f} | {fmt_pct(rmap.get(d))} |")
        else:
            lines.append("- 数据：无台账，用回测曲线当替身（`reflections/` 里最近一次结论仍会附上）")
            cfg = {k: v["value"] for k, v in s.get("params", {}).items()}
            series, idle, note = shadow_series(cfg)
            lines.append(f"- 影子：{note}")
            if series:
                # 诊断只看最近一年：容忍线是按近期风险定的，
                # 拿十年全样本的回撤去比 -15%，每天都会误报。
                win = series[-OOS_DAYS:]
                base = win[0][1]
                win = [(d, v / base) for d, v in win] if base > 0 else win
                rets = period_rets(win)
                m = metrics(win, rets)
                extra["bench_total"] = benchmark_5050(win[0][0], win[-1][0])
                extra["idle_days"] = idle
                lines.append(f"- 区间：{win[0][0]} → {win[-1][0]}，{m['n_periods']} 个观察期"
                             f"（近 {OOS_DAYS} 个交易日）")
                lines.append(f"- 累计 {fmt_pct(m.get('total'))}　最大回撤 {fmt_pct(m.get('mdd'))}　"
                             f"近 20 期均值 {fmt_pct(m.get('mean20'))}　"
                             f"近 20 期胜率 {(m.get('win20') or 0):.0%}　"
                             f"尾部连续 {m.get('tail_streak', 0):+d} 期")
                if not any(r != 0 for _, r in rets[-20:]):
                    lines.append("  - 近 20 期收益全为 0：空仓中，属规则设计，不是数据坏了")
                if extra["bench_total"] is not None:
                    lines.append(f"- 基准（两腿等权平均）{fmt_pct(extra['bench_total'])}　"
                                 f"超额 {fmt_pct(m['total'] - extra['bench_total'])}")
                if idle:
                    lines.append(f"- 连续空仓 {idle} 个交易日（回测口径）")
            lines.append(f"- 最近回测：{latest_backtest_note(s['id'], s.get('name', ''))}")

        hits = diagnose(m, s, extra, reg["gate"]) if m else []
        lines.append("")
        if not hits:
            lines.append("**诊断：无现象命中**，本轮没有改动理由。")
        else:
            lines.append(f"**诊断：命中 {len(hits)} 条**")
            for h in hits:
                book = FIX_BOOK.get(h["key"], {})
                lines.append(f"- `{h['key']}` {book.get('title','')}：{h['evidence']}")
                lines.append(f"  - 候选改法：{book.get('candidates','')}")
                lines.append(f"  - 判据：{book.get('judge','')}")
        lines.append("")
        params = s.get("params", {})
        lines.append("可进化参数：" + "、".join(
            f"`{k}`={v['value']}（网格 {len(v['grid'])} 档）" for k, v in params.items()))
        lines.append("冻结：" + "；".join(s.get("frozen", [])))
        lines.append("")

    lines.append("## 不许再试的方向")
    lines.append("")
    for n in reg.get("negative_list", []):
        lines.append(f"- {n}")
    lines.append("")
    lines.append("## 闸门参数")
    lines.append("")
    for k, v in reg["gate"].items():
        lines.append(f"- {k} = {v}")

    text = "\n".join(lines)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"evolve_{today()}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n→ {out}")


def last_idle_days(book: Path, sheet: str) -> int:
    """净值表最后连续「闸门仓位=0」的行数。找不到该列就返回 0。"""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(book, read_only=True, data_only=True)
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
    except Exception:
        return 0
    if len(rows) < 2:
        return 0
    head = [str(c).strip() if c is not None else "" for c in rows[0]]
    ci = next((i for i, h in enumerate(head) if "闸门仓位" in h), -1)
    if ci < 0:
        return 0
    n = 0
    for r in reversed(rows[1:]):
        if ci < len(r) and r[ci] not in (None, "") and float(r[ci]) == 0:
            n += 1
        else:
            break
    return n


def latest_backtest_note(sid: str, name: str = "") -> str:
    keys = [k for k in (sid, name) if k]
    files = sorted(OUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        if f.name.startswith("evolve_"):     # 跳过本工具自己写的体检报告
            continue
        body = f.read_text(encoding="utf-8", errors="ignore")
        if any(k in body for k in keys):
            return f.name
    return "无记录"


def _coerce(old, raw: str):
    """按现值的类型解析新值。int 参数也接受 "20.0" 这种写法。"""
    try:
        f = float(raw)
    except ValueError:
        raise SystemExit(f"值 {raw!r} 不是合法数字")
    if isinstance(old, bool):
        raise SystemExit("布尔参数不支持命令行改动")
    if isinstance(old, int):
        if abs(f - round(f)) > 1e-9:
            raise SystemExit(f"{raw!r} 与现值类型不符：{old!r} 是整数")
        return int(round(f))
    return f


def apply_change(args) -> None:
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    log = load_log()

    if args.apply.count("=") != 1 or "," in args.apply:
        raise SystemExit("--apply 只接受一处改动，写成 参数=新值，例如 --apply band=0.025")
    param, value_raw = args.apply.split("=", 1)
    param, value_raw = param.strip(), value_raw.strip()

    # 只支持单策略单参数；有多个 live 策略时必须指定
    targets = [s for s in reg["strategies"] if param in s.get("params", {})]
    if len(targets) != 1:
        raise SystemExit(f"「{param}」在 {len(targets)} 个策略里出现，无法确定要改哪一个")
    s = targets[0]
    old = s["params"][param]["value"]
    value = _coerce(old, value_raw)

    checks = gate(s, log, param, value, reg["gate"],
                  why=args.why or "", wrong_if=args.wrong_if or "",
                  ack_forbidden=args.ack_forbidden)
    ok_oos, oos_note = oos_check(s, param, value, reg["gate"])
    checks.append((f"G9 样本外复核", ok_oos, oos_note))

    print("闸门检查：")
    ok_all = True
    for name, ok, note in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}：{note}")
        ok_all = ok_all and ok
    if not ok_all:
        raise SystemExit("闸门未全过，拒绝落地。")

    s["params"][param]["value"] = value
    reg["updated"] = today()
    REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")

    rec = {
        "ts": now_str(), "date": today(), "strategy": s["id"], "action": "apply",
        "param": param, "from": old, "to": value,
        "hypothesis": args.why.strip(),
        "wrong_if": args.wrong_if.strip(),
        "observe_until": plus_trading_days(reg["gate"]["observe_days"]),
        "oos": oos_note,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n已落地：{s['id']}.{param} {old} → {value}，观察至 {rec['observe_until']}")
    _autopublish(f"{s['id']}.{param} {old} → {value}")


def _autopublish(note: str) -> None:
    """一改参数就立刻同步到 GitHub 仓库。发布副本里没有这个工具，静默跳过。

    同步失败不影响参数落地——已经过了九道闸门的改动不该因为网断了就作废，
    但会把失败原因打出来，不让它静默。
    """
    import subprocess

    tool = ROOT / "tools" / "publish_etf.py"
    if not tool.exists() or not (ROOT / "publish" / "ETF").exists():
        return
    try:
        p = subprocess.run(
            [sys.executable, str(tool), "-m", f"策略变更：{note}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if p.returncode == 0:
            lines = [l for l in p.stdout.strip().splitlines() if l.strip()]
            print("已同步到 GitHub 仓库" + (f"（{lines[-1]}）" if lines else ""))
        else:
            print("自动同步失败（参数已落地，仓库未更新）："
                  + (p.stderr or p.stdout).strip()[:300])
    except Exception as e:  # 网络/权限什么都可能，不让它拖垮落地
        print(f"自动同步异常（参数已落地，仓库未更新）：{e}")


def rollback(sid: str) -> None:
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    log = load_log()
    mine = [r for r in log if r.get("strategy") == sid and r.get("action") == "apply"]
    if not mine:
        raise SystemExit(f"{sid} 没有可回滚的变更")
    last = mine[-1]
    s = next(x for x in reg["strategies"] if x["id"] == sid)
    s["params"][last["param"]]["value"] = last["from"]
    reg["updated"] = today()
    REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    rec = {"ts": now_str(), "date": today(), "strategy": sid, "action": "rollback",
           "param": last["param"], "from": last["to"], "to": last["from"],
           "reason": "手动回滚"}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"已回滚 {sid}.{last['param']}：{last['to']} → {last['from']}")
    _autopublish(f"{sid}.{last['param']} 回滚 {last['to']} → {last['from']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true", help="打印最近 20 期明细")
    ap.add_argument("--apply", metavar="PARAM=VALUE", help="过闸后落地一处改动")
    ap.add_argument("--why", help="为什么改（必填，≥12 字，写人话，不许模板）")
    ap.add_argument("--wrong-if", dest="wrong_if",
                    help="什么算改错（必填，≥12 字，事后再看时拿它对照）")
    ap.add_argument("--ack-forbidden", action="store_true",
                    help="显式确认本次未触犯登记表里的文本禁令")
    ap.add_argument("--rollback", metavar="STRATEGY_ID", help="回滚该策略上次变更")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if a.rollback:
        rollback(a.rollback)
        return
    if a.apply:
        apply_change(a)
        return
    run(detail=a.detail)


if __name__ == "__main__":
    main()
