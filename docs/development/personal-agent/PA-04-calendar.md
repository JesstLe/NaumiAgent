# PA-04 v1 日历协议

日期：2026-11-05。

本模块只实现周期委托的日历解释与查询。旧 `scheduler/cron.py`、既有 reminder 和
PA-01 的单次 Item 协议保持不变。周期定义、发生账本、累计预算、通知、界面和真实长期运行
分别由 PA-04 对应模块验收；本模块通过不代表整个 Personal Agent 产品完成。

## 1. 实际实现

- `personal/routine_schedule.py`：输入解析、严格持久化协议、IANA/DST、五段 Cron、有限查询。
- `personal/trading_calendar.py`：真实交易所日历适配器、完整会话快照、摘要和覆盖校验。
- `tests/unit/test_personal_routine_schedule.py`：直接使用真实 IANA 与已安装的交易日历组件。

### 1.1 稳定的时间身份

`normalize_schedule(input, now)` 只在新请求第一次受理时调用；返回与输入分离、可 JSON
持久化的规范对象。重放必须读回原对象，用 `validate_schedule(resolved)` 复核，不能再次采样
`now` 或重建新日历。共有字段：

| 字段 | 合同 |
|---|---|
| `version` | 固定整数 `1` |
| `kind` | `once / cron / trading_day` |
| `timezone` | 明确 IANA 时区，默认 `Asia/Shanghai` |
| `timezone_sha256` | 实际读取的 TZif 字节摘要 |
| `starts_at / ends_at` | 规范 UTC、微秒精度；起止均包含，结束可为空 |
| `dst_gap` | 固定 `skip` |
| `dst_fold` | `first / second / both`，默认 `first` |

时区解析从实际 TZif 字节构造 `ZoneInfo`，并保留同份字节摘要。日后 tzdb 改变时返回
`calendar_timezone_changed`，不静默改变未来 nominal UTC。每次发生返回
`due_at / local_time / fold / calendar_evidence`，计划的 nominal due 不会被执行时刻 `now` 替换。

### 1.2 once

输入 `local_time` 是无偏移的完整本地日期时间。不存在的本地时刻明确拒绝；重叠时刻必须
提供 `fold=0/1`。规范对象额外保存唯一的 `due_at`。它必须位于包含边界的计划起止之内。

### 1.3 Cron

输入 `expression` 是严格五段数字表达式，支持列表、范围、正步长和 `0/7` 周日别名。
`N/step` 从 N 一直扩展到该字段最大值。DOM 与 DOW 均非字面 `*` 时按 OR；其中一个
为字面 `*` 时只约束另一个。`*/2` 是受限集合，和指定 DOW 仍按 OR。

此语义与旧 reminder 的 AND 解释独立，不迁移、不重释旧表达式。算法按本地日期和选定
时分组合搜索，不逐分钟扫描多年；有效闰日可跨过 2100 非闰年到 2104。Spring gap 跳过，
fall overlap 按选定策略产生 UTC 排序的一个或两个发生。

## 2. 真实交易日历

依赖 `exchange-calendars>=4.11,<5`。调研来源为上游 PyPI 元数据、官方 README、
`get_calendar()`、`ExchangeCalendar` 和 `PrecomputedExchangeCalendar` 源码。
本次读取的版本为 **4.13.2**。组件说明其日历由社区维护；规则投影不是实时临时停市确认。

输入字段为 `calendar_id / coverage_start / coverage_end / at / local_time / offset_minutes`。

- `at=open/close` 使用真实会话开闭市，不按平日固定时刻猜测，半日市会提前触发。
- `at=time` 使用真实 session label 筛选日期，并在计划 IANA 时区的 `HH:MM` 触发。
- `offset_minutes` 是在选定真实 UTC 锚点上的绝对分钟偏移，范围为正负 10080。
- 日期标签与实际开闭市日期分别保留，适配跨午夜会话。
- 快照保留 `date/open/close/break_start/break_end/early_close/late_open`。
- 凭据保留 provider、provider version、canonical calendar id、exchange timezone、请求覆盖、
  `source=versioned_rules_not_realtime` 与全快照 SHA256。
- 重开快照只校验原摘要，不导入新 provider 重算原有日期。
- 覆盖最多 **1831 个日历日、5000 条会话**；超出时明确报错，不截断。
- 预计算日历同时服从实际已发布假日表年份和库的 bounds。4.13.2 的 XSHG 已知假日表覆盖
  **1991-2026**（605 条，官方 docstring 的 1999 起点已过期）；2027 明确返回
  `calendar_coverage_unknown`，不以周一至周五补出交易日。

`available_calendars(now=None, include_all=False)` 返回真实组件元数据，默认提供上海、纽约、
香港、伦敦、东京；`include_all=True` 返回所有 canonical ID，底层支持组件全部有效 ID。
规则型日历的 metadata 范围是组件默认投影范围，不表示交易所已逐日确认未来临时安排。

## 3. 有界查询与恢复

所有接口先严格复核原规范对象。`after` 严格排除；`until` 与计划起止均包含。

| 接口 | 返回 |
|---|---|
| `next_occurrences(resolved, after, limit, until=None)` | 最多 limit 条、按 nominal UTC 排序 |
| `count_occurrences(resolved, after, until, max_count=100000)` | 精确整数；超限明确失败 |
| `coalesce_occurrences(resolved, after, until, max_count=100000)` | 最新原发生与精确合并计数 |
| `catch_up_occurrences(resolved, after, until, limit)` | 按原顺序的发生页、has_more、原 resume_after |

单页最多 1000 条，一次查询跨度最多 2936 天；调用者按原游标分段。精确计数超过容量返回
`calendar_query_limit`，不以截断计数冒充总数。交易日查询触及未知覆盖返回
`calendar_coverage_unknown`，不当作自然结束。已知节假日空区间可以明确返回零发生。

## 4. 原始反例与验证记录

事务路径：`.naumi/transactions/pa04-recurring-20261105/calendar/`。
`BASELINE.txt` 保存新增模块缺失、旧 Cron DOM/DOW AND、N/step 与 366 天闰日反例及
原 `scheduler/cron.py` SHA256；原模块不是本次修改对象。官方元数据与源码读取产物同目录保存。

集中验证命令与字面结果：

```text
.venv/Scripts/python.exe -m pytest tests/unit/test_personal_routine_schedule.py -q
93 passed in 17.83s
EXIT_STATUS: 0

.venv/Scripts/python.exe -m ruff check src/naumi_agent/personal/routine_schedule.py src/naumi_agent/personal/trading_calendar.py tests/unit/test_personal_routine_schedule.py
All checks passed!
EXIT_STATUS: 0
```

首次集中验证为 `4 failed, 89 passed, 193 warnings in 6.65s`，exit 1，原日志保留为
`MODIFIED-tests-first.txt`，未改写为成功。其间暴露两项本模块真实问题：多套预计算表不能直接
用 `min()` 比较、XSHG docstring 起点过期；均已按实际表与子类交集修正。另一项是真实依赖
兼容性：`pandas 3.1.1` 的 DateOffset 不支持包内 OrthodoxEaster，读取所有日历时实际报错。
Root 负责增加 `pandas>=2.2,<3` 并更新 lock，当前安装 `pandas 2.3.3`；上述最终 93 项包含
所有 canonical 日历 metadata 实际读取，不通过跳过失效日历消除错误。

独立样例命令：

```text
.venv/Scripts/python.exe .naumi/transactions/pa04-recurring-20261105/calendar/calendar_probe.py --output .naumi/transactions/pa04-recurring-20261105/calendar/calendar-samples.json
DOM_DOW_OR 2026-12-01T09:00:00.000000+00:00
NUMBER_STEP 05,20,35,50
LEAP_DAY 2104-02-29T09:00:00.000000+00:00
DST_BOTH 2030-11-03T05:30:00.000000+00:00 2030-11-03T06:30:00.000000+00:00
DST_GAP 2030-03-11T06:30:00.000000+00:00
XNYS_HALF_DAY 2026-11-27T18:00:00.000000+00:00
XSHG_COVERAGE 1991-01-01 2026-12-31
EXACT_COALESCED_COUNT 5
SNAPSHOT_SHA256 70bc0a9144915654dd1a5bc96d5cce4f7c05062cd432cf90fbd7bdd143b053cd
SAMPLES_SHA256 f98facb60ef45b9cc7b5b8e378841e46487186518ce1cdc48d8eaf5baaa1a71d
CALENDAR_PROBE_OK
EXIT_STATUS: 0
```

样例 JSON 是实际规范对象、真实 session 快照和发生结果，不是示意数据。原日历组件仍是
版本化本地规则证据，不从该层推导实时账户、模型执行、送达或长期运行验收。

本模块四个新增文件的基线均为 absent；独立副本回滚删除这些新增文件，原旧 Scheduler
哈希和行为保持不变。完整四文件清单、源码 SHA、差异、可执行 guarded rollback、逐字
baseline/modified/rollback 命令和结果见同目录 `VERIFICATION.txt`。回滚只覆盖这四个文件，
不降级 Root 的项目依赖、数据库或其他 PA-04 模块。
