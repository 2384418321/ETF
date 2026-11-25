# 换台电脑接手：从零到出信号

不需要任何人交接。照着做，十分钟内能跑出今天该不该持仓。

## 0. 你需要什么

- Python 3.9+
- 能访问外网（拉行情用公开接口，**不需要任何 token**）
- 没了

## 1. 拉代码、装依赖

```bash
git clone https://github.com/2384418321/ETF.git
cd ETF
python -m pip install -r requirements.txt
```

## 2. 建行情库（只有这一步需要联网）

```bash
python data/fetch_etf_daily.py
```

它会拉 510300.SH 与 510500.SH 的全历史日线，写进 `data/market.db`（表 `etf_daily`）。
数据源是东方财富公开 K 线接口，一次返回全部历史，没有 token、没有配额。

输出应该长这样：

```
510300.SH  20150105 → 2026xxxxxxxx  2887 行  收盘 4.018
510500.SH  20150105 → 2026xxxxxxxx  2885 行  收盘 6.053
合计 5772 行 → .../data/market.db
```

`data/market.db` 已在 `.gitignore` 里，不会进仓库——所以换机器必须重拉一次。

## 3. 出信号

```bash
python strategy/etf_gate.py --capital 100000
```

看三列就够：`信号`（多头/空仓）、`目标股数`、`合计目标仓位`。
**今天的信号是给下一个交易日开盘用的**，不是今天收盘就动手。

## 4. 验证回测能复现

```bash
python backtest/gate_backtest.py --start 20220101
```

应该打出跟 `reflections/BACKTESTS.md` 里一模一样的数字。对不上就先别信信号——查数据。

## 5. 每日体检（可选，但建议）

```bash
python tools/evolve_cycle.py
```

它按 `strategy_registry.json` 算事实、下诊断、过九道闸门，默认什么都不改。
要改参数只能走 `--apply`，九道闸门全过才落地；`--why` 与 `--wrong-if` 是必填，
代码不接受模板，也不接受「先改了再说」。

---

## 文件都是干什么的

| 路径 | 作用 |
|---|---|
| `strategy/etf_gate.py` | 出今日信号与目标股数。**每天只需要跑它** |
| `strategy/config.json` | 参数（均线长度、缓冲、单只仓位），改这里或走 `--apply` |
| `backtest/gate_backtest.py` | 账户级回测。`--grid` 扫参数面，`--scale` 降杠杆 |
| `data/fetch_etf_daily.py` | 建行情库。换机器第一步 |
| `tools/evolve_cycle.py` | 体检 + 九道闸门 + 落地 + 回滚 |
| `strategy_registry.json` | 登记表：哪些参数能改、能改到哪几档、哪些冻死、哪些方向禁止 |
| `skills/strategy-self-evolution/` | 自进化协议的完整纪律，改参数前必读 |
| `reflections/BACKTESTS.md` | 回测结论，体检时被引用 |
| `reflections/evolve_cycle.jsonl` | 改动账：每次改了什么、预期什么、什么算改错 |
| `RULES.md` | 交易规则条文 |

## 台账（你自己的账户记录）

这个仓库**刻意不带台账**。台账是你自己的钱怎么走，属于私人记录，不该进公开仓库。

想让它也进体检，就在仓库里放一个 xlsx，然后把 `strategy_registry.json` 里
`etf_gate` 的 `book` 从 `null` 改成文件名、`sheet` 填净值表的名字。
体检会按表头名去找「日期」「总资产」两列，不依赖列位置。

没填 `book` 时，体检拿回测曲线当替身（只看最近 250 个交易日），闸门照样能跑，
只是少了一层实盘表现。

## 改参数的正确姿势

```bash
python tools/evolve_cycle.py --apply "band=0.025" \n    --why "震荡期贴着均线反复穿越，2% 缓冲不够" \n    --wrong-if "20 个交易日后跑输基线 3 个百分点" \n    --ack-forbidden                                # 九闸全过才落地
python tools/evolve_cycle.py --rollback etf_gate    # 改错了回滚
```

不许手改 `strategy_registry.json` 绕过闸门。闸门拦的正是这个：拿当期盈亏去拧参数。
