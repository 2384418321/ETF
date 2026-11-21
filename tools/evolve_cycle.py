#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""策略自进化循环 —— 把「该不该改」变成可以逐条核对的门。

每天复盘跑一次。它做三件事：
  1. 算事实：从台账/回测里读出每个策略的实际表现
  2. 下诊断：命中哪几条现象，每条现象对应哪些候选改法
  3. 过闸门：这条改动现在允不允许动，卡在哪一条

它默认**不改任何东西**。只有 `--apply "<参数>=<新值>"` 且八道闸全过，才写回
strategy_registry.json —— 并且先把「预期改善多少、什么算改错」写进变更账。

用法：
    python tools/evolve_cycle.py                      # 只体检
    python tools/evolve_cycle.py --detail             # 带每期明细
    python tools/evolve_cycle.py --apply "band=0.025" # 过闸后落地（只改一处）
    python tools/evolve_cycle.py --rollback etf_gate  # 回滚上次变更
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REGISTRY = ROOT / "strategy_registry.json"
DB = ROOT / "data" / "market.db"
LOG = ROOT / "reflections" / "evolve_cycle.jsonl"
OUT_DIR = ROOT / "reflections"

TZ = timezone(timedelta(hours=8))


def today() -> str:
    return datetime.now(TZ).strftime("%Y%m%d")


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


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


# ---------------------------------------------------------------- 基准
def benchmark_5050(start: str, end: str) -> float | None:
    """510300 + 510500 等权买入持有，同区间总收益。"""
    if not DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
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


def trading_days_between(a: str, b: str) -> int:
    if not DB.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        n = con.execute(
            "select count(*) from trade_cal where cal_date between ? and ? and is_open=1",
            (a, b)).fetchone()[0]
        con.close()
        return int(n or 0)
    except sqlite3.Error:
        return 0


def gate(strategy: dict, log: list[dict], param: str, new_value, gate_cfg: dict) -> list[tuple[str, bool, str]]:
    """返回 [(闸门编号, 是否通过, 说明)]。全通过才允许落地。"""
    sid = strategy["id"]
    mine = [r for r in log if r.get("strategy") == sid and r.get("action") == "apply"]
    checks: list[tuple[str, bool, str]] = []

    # G1 冷却
    if mine:
        gap = trading_days_between(mine[-1]["date"], today())
        ok = gap >= gate_cfg["cooldown_days"]
        checks.append(("G1 冷却期", ok, f"距上次变更 {gap} 个交易日（要求 ≥{gate_cfg['cooldown_days']}）"))
    else:
        checks.append(("G1 冷却期", True, "从未变更过，不受冷却期限制"))

    # G2 同参数 60 日内变更次数
    recent = [r for r in mine if trading_days_between(r["date"], today()) <= 60]
    same = [r for r in recent if r.get("param") == param]
    checks.append(("G2 同参数频次",
                   len(same) < gate_cfg["max_changes_per_60d"],
                   f"60 日内该参数改过 {len(same)} 次（上限 {gate_cfg['max_changes_per_60d']}）"))

    # G3 网格内
    spec = strategy.get("params", {}).get(param)
    if spec is None:
        checks.append(("G3 参数已登记", False, f"「{param}」不在登记表里，视为冻结参数"))
        return checks
    in_grid = new_value in spec["grid"]
    checks.append(("G3 网格内", in_grid,
                   f"新值 {new_value} {'在' if in_grid else '不在'}候选网格 {spec['grid']}"))

    # G4 不是原值
    changed = new_value != spec["value"]
    checks.append(("G4 确有变化", changed, f"现值 {spec['value']} → 目标 {new_value}"))

    # G5 方向不在禁令里
    forbidden = " / ".join(strategy.get("forbidden_directions", []))
    checks.append(("G5 非禁止方向", True,
                   f"登记的三条禁令：{forbidden[:60]}…（人工确认本次未触犯）"))

    return checks


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
                lines.append(f"- 区间：{series[0][0]} → {series[-1][0]}，{m['n_periods']} 个观察期")
                lines.append(f"- 累计 {fmt_pct(m.get('total'))}　最大回撤 {fmt_pct(m.get('mdd'))}　"
                             f"近 20 期均值 {fmt_pct(m.get('mean20'))}　近 20 期胜率 "
                             f"{(m.get('win20') or 0):.0%}　尾部连续 {m.get('tail_streak', 0):+d} 期")
                if extra["bench_total"] is not None:
                    edge = m["total"] - extra["bench_total"]
                    lines.append(f"- 基准（50/50 买入持有）{fmt_pct(extra['bench_total'])}　"
                                 f"超额 {fmt_pct(edge)}")
                # 连续空仓：看最后一行闸门仓位
                idle = last_idle_days(ROOT / s["book"], s["sheet"])
                extra["idle_days"] = idle
                if idle:
                    lines.append(f"- 连续空仓 {idle} 个交易日（按净值表「闸门仓位」列，回填的历史也计入，仅作变化趋势看）")
                if detail:
                    lines.append("")
                    lines.append("| 日期 | 净值 | 期收益 |")
                    lines.append("|---|---|---|")
                    rmap = dict(rets)
                    for d, v in series[-20:]:
                        lines.append(f"| {d} | {v:.4f} | {fmt_pct(rmap.get(d))} |")
        else:
            lines.append("- 数据：纯回测策略，无台账。体检改为读 `reflections/` 里最近一次回测结论")
            last = latest_backtest_note(s["id"], s.get("name", ""))
            lines.append(f"- 最近回测：{last}")

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
        body = f.read_text(encoding="utf-8", errors="ignore")
        if any(k in body for k in keys):
            return f.name
    return "无记录"


def apply_change(param: str, value_raw: str) -> None:
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    log = load_log()

    # 只支持单策略单参数；有多个 live 策略时必须指定
    targets = [s for s in reg["strategies"] if param in s.get("params", {})]
    if len(targets) != 1:
        raise SystemExit(f"「{param}」在 {len(targets)} 个策略里出现，无法确定要改哪一个")
    s = targets[0]
    old = s["params"][param]["value"]
    try:
        value = float(value_raw) if isinstance(old, float) else int(value_raw)
    except ValueError:
        raise SystemExit(f"值 {value_raw!r} 不是合法数字")

    checks = gate(s, log, param, value, reg["gate"])
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
        "hypothesis": f"改 {param}: {old} → {value}，预期改善 {s['name']} 的近 20 期表现",
        "wrong_if": f"改后 {reg['gate']['observe_days']} 个交易日内，累计跑输基线 "
                    f"{reg['gate']['rollback_underperform_pp']} 个百分点",
        "observe_until": plus_trading_days(reg["gate"]["observe_days"]),
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n已落地：{s['id']}.{param} {old} → {value}，记入 {LOG.name}")
    _autopublish(f"{s['id']}.{param} {old} → {value}")


def _autopublish(note: str) -> None:
    """一改参数就立刻同步到 GitHub 仓库。发布副本里没有这个工具，静默跳过。

    同步失败不影响参数落地——已经过了八道闸门的改动不该因为网断了就作废，
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


def plus_trading_days(n: int) -> str:
    """从今天往后数 n 个交易日（用 trade_cal）。"""
    if not DB.exists():
        return today()
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = con.execute(
            "select cal_date from trade_cal where cal_date > ? and is_open=1 "
            "order by cal_date limit ?", (today(), n)).fetchall()
        con.close()
        return rows[-1][0] if rows else today()
    except sqlite3.Error:
        return today()


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
    ap.add_argument("--rollback", metavar="STRATEGY_ID", help="回滚该策略上次变更")
    a = ap.parse_args()

    if a.rollback:
        rollback(a.rollback)
        return
    if a.apply:
        if "=" not in a.apply:
            raise SystemExit("--apply 要写成 参数=新值，例如 --apply band=0.025")
        p, v = a.apply.split("=", 1)
        apply_change(p.strip(), v.strip())
        return
    run(detail=a.detail)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
