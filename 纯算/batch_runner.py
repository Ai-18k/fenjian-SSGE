#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
智能分拣 · 纯算法批量测试入口（无 Web / 无前端）

================================================================================
运行方式（详细）
================================================================================

【环境要求】
  - Python 3.10+（使用了 `str | list` 等类型注解语法）
  - 无需安装第三方依赖，仅标准库
  - 工作目录：项目根目录 `demo/`（与本文件同级）

【方式一：单次仿真 run】
  在项目根目录执行：

    python batch_runner.py run --seed 42 -n 25000 --specs module-a

  常用参数说明：
    --seed 42              随机种子，决定批次鱼重量序列（可复现）
    -n 25000 / --count     按条数结束（与 --weight 二选一）
    -w 10 / --weight       按总重结束，单位吨（如 10 表示 10 吨）
    --specs module-a       启用规格预设，见下方「规格预设表」
    --specs 15p,20p,25p    或直接逗号分隔规格名
    --move-timeout 30      队首/暂存超时阈值（步或秒，见 --timeout-clock）
    --cap-factor 1         三合一料道扩容系数：容量 = min(装箱尾数) + N
    --timeout-clock intake 超时计时：intake=每入料一步+1；real=墙钟秒
    --speed 50             进料速率（条/秒），模拟 Web 倍速；0=不限速
    -v / --verbose         打印每条入料/封箱日志
    --report               跑完后打印终端汇总报告（单跑默认开启）
    --quiet                仅输出一行摘要
    --artifacts-dir DIR    将 run_report/cartons/remaining 等 CSV 复制到指定目录
    --name my_case         用例名称（用于输出文件前缀）

  示例：
    # 模块 A，25000 条，种子 42
    python batch_runner.py run --seed 42 -n 25000 --specs module-a

    # 模块 C，按 10 吨总重结束，超时 180 步，50 条/秒
    python batch_runner.py run --seed 42 --weight 10 --specs module-c --move-timeout 180 --speed 50

    # 安静模式，只打一行摘要
    python batch_runner.py run --seed 42 -n 1000 --specs default --quiet

【方式二：预设批量对照 preset】
  依次跑多个规格预设，输出对比表格，并写入 data/batch_runs/summary_*.csv/json：

    python batch_runner.py preset module-a module-b module-c -n 25000 --seed 42

  列出所有预设：
    python batch_runner.py preset --list

  额外参数：
    --output-dir DIR       汇总 CSV/JSON 输出目录（默认 data/batch_runs）

【方式三：矩阵用例 matrix】
  从 JSON 文件批量跑多组参数（JSON 需含 cases 数组）：

    python batch_runner.py matrix --file plan/batch_cases.example.json

【方式四：直接跑引擎 Scheduler_Engine.py】
  不经过 batch_runner，直接调用核心引擎：

    python Scheduler_Engine.py --seed 42 -n 25000 --fast
    python Scheduler_Engine.py --seed 42 -n 25000 -v          # 详细日志
    python Scheduler_Engine.py --move-timeout 180 --timeout-clock real

  参数：--seed, -n/--total, -i/--interval, --move-timeout, --timeout-clock,
        --csv, --fast, -v/--verbose, --log-every

【方式五：单独生成批次数据】
    python plan/随机种子生成.py --seed 42 -n 25000 --enabled-specs 15p,20p,25p
    python plan/随机种子生成.py --seed 42 --target-weight-g 10000000   # 按 10 吨

【方式六：算法子模块演示】
    python plan/细分规则.py          # 小/中/大分区划分报告
    python plan/深度搜索.py          # DFS 配盒演示
    python plan/计算需求.py          # 动态需求计算 Demo

【规格预设表 SPEC_PRESETS】
  module-a  → 15p, 20p, 25p, 30p, 35p, 40p   （模块 A，轻规格）
  module-b  → 45p ~ 90p                        （模块 B，中规格）
  module-c  → 100p ~ 150p                      （模块 C，重规格）
  default   → 同 module-a
  all       → 全部 18 个规格

【输出文件】
  data/fish_seed_{seed}_*.csv     批次鱼数据（首次运行自动生成）
  data/run_report_seed_{seed}.csv 全批次鱼生命周期追踪
  data/cartons_seed_{seed}.csv    成盒明细
  data/remaining_seed_{seed}.csv  尾料明细
  data/timeout_tail_seed_{seed}.csv 超时尾料明细
  data/batch_runs/summary_*.csv   批量对照汇总

【与 Web 版的区别】
  批量测试默认 exclude_outside_stats=True：规格外鱼不计入入料/结束条件，
  进度与汇总改显超时鱼数量，便于纯算法指标对比。
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# SRC_DIR：本脚本所在目录，即项目根 demo/
SRC_DIR = Path(__file__).resolve().parent
# ROOT：项目根目录（与 SRC_DIR 相同；data/、plan/ 均在此下）
ROOT = SRC_DIR
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from Scheduler_Engine import (  # noqa: E402
    ALL_SPECS,                    # 全部 18 个规格名元组
    DEFAULT_CAP_FACTOR,           # 默认料道扩容系数（+1）
    DEFAULT_ENABLED_SPECS,        # 默认启用规格（module-a）
    DEFAULT_MOVE_TIMEOUT,         # 默认队首超时阈值
    DEFAULT_SEED,                 # 默认随机种子 42
    DEFAULT_STOP_WEIGHT_G,        # 默认按重结束目标（克）
    DEFAULT_STOP_WEIGHT_TONS,     # 默认按重结束目标（吨）10.0
    DEFAULT_TIMEOUT_CLOCK,        # 默认超时计时方式 intake
    DEFAULT_TOTAL,                # 默认入料条数 25000
    MODULE_SPECS,                 # 模块 A/B/C 规格划分
    STOP_MODE_COUNT,              # 结束模式：按条数 "count"
    STOP_MODE_WEIGHT,             # 结束模式：按总重 "weight"
    TIMEOUT_CLOCK_INTAKE,         # 超时计时：进料步进
    TIMEOUT_CLOCK_REAL,           # 超时计时：真实墙钟秒
    SchedulerEngine,              # 分拣仿真主引擎
    _root,                        # 引擎模块根路径（data/ 目录定位）
    batch_total_for_run,          # 按结束条件计算需预加载批次上限
    load_or_generate_batch,       # 加载或生成批次 CSV
    normalize_enabled_specs,      # 校验并规范化启用规格
)

# ---------------------------------------------------------------------------
# 预设规格组（与模块 A/B/C 划分一致）
# ---------------------------------------------------------------------------
# SPEC_PRESETS：CLI/JSON 中 specs 字段的预设名 → 规格元组映射
SPEC_PRESETS: dict[str, tuple[str, ...]] = {
    "module-a": tuple(MODULE_SPECS["A"]),   # 轻规格 15p~40p
    "module-b": tuple(MODULE_SPECS["B"]),   # 中规格 45p~90p
    "module-c": tuple(MODULE_SPECS["C"]),   # 重规格 100p~150p
    "default": DEFAULT_ENABLED_SPECS,       # 默认 = module-a
    "all": ALL_SPECS,                       # 全部 18 规格
}


def resolve_specs(raw: str | list[str] | None) -> tuple[str, ...]:
    """
    解析 CLI/JSON 中的规格配置。

    参数:
        raw: 预设名（如 module-a）、逗号分隔字符串（15p,20p）、或规格列表

    返回:
        规范化后的启用规格元组
    """
    if raw is None:
        return DEFAULT_ENABLED_SPECS
    if isinstance(raw, str):
        key = raw.strip().lower()
        if key in SPEC_PRESETS:
            return SPEC_PRESETS[key]
        # 支持中文逗号
        parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
        return normalize_enabled_specs(parts)
    return normalize_enabled_specs(raw)


# DEFAULT_BATCH_SPEED：批量测试默认进料速率（条/秒）；0 表示不限速
DEFAULT_BATCH_SPEED = 20.0


@dataclass
class RunConfig:
    """单次仿真运行的配置参数。"""

    name: str = "run"                                    # 用例名称（输出标识）
    seed: int = DEFAULT_SEED                             # 随机种子
    stop_mode: str = STOP_MODE_COUNT                     # 结束模式：count / weight
    stop_count: int = DEFAULT_TOTAL                      # 按条数结束时的目标条数
    stop_weight_g: int = DEFAULT_STOP_WEIGHT_G           # 按总重结束时的目标克重
    enabled_specs: tuple[str, ...] = DEFAULT_ENABLED_SPECS  # 启用的规格列表
    move_timeout: int = DEFAULT_MOVE_TIMEOUT             # 队首/暂存超时阈值
    cap_factor: int = DEFAULT_CAP_FACTOR                 # 料道扩容系数
    timeout_clock: str = DEFAULT_TIMEOUT_CLOCK           # 超时计时方式
    speed: float = DEFAULT_BATCH_SPEED                   # 进料速率（条/秒）
    verbose: bool = False                                # 是否打印详细日志


@dataclass
class RunSummary:
    """单次仿真结束后的汇总指标。"""

    name: str                          # 用例名称
    seed: int                          # 随机种子
    stop_mode: str                     # 结束模式
    stop_target: str                   # 结束目标（条数字符串或 "10.0t"）
    enabled_specs: str                 # 启用规格（逗号拼接）
    input_count: int                   # 入料条数（规格内）
    input_weight_tons: float           # 入料总重（吨）
    cartons: int                       # 成盒数
    packed_fish: int                   # 装箱鱼条数
    pack_rate_pct: float               # 装箱率（%）
    outside_count: int                 # 规格外条数
    timeout_total: int                 # 超时尾料合计（料道+暂存）
    timeout_lane: int                  # 料道超时尾料
    reflow_count: int                  # 回流次数
    timeout_tail: int                  # 料道超时（同 timeout_lane）
    overflow_reflow: int               # 超容回流次数
    storage_in: int                    # 暂存箱入箱次数
    storage_to_lane: int               # 暂存箱回料道次数
    storage_packed: int                # 从暂存箱直接成盒次数
    storage_max: int                   # 暂存箱峰值条数
    storage_capacity: int              # 暂存箱容量上限
    storage_timeout_tail: int          # 暂存箱超时尾料
    storage_batch_tail: int            # 暂存箱批末尾料
    tail_batch_total: int              # 批末未配盒尾料合计
    tail_lane_batch: int               # 批末料道未配盒
    tail_storage_batch: int            # 批末暂存未配盒
    tail_reflow_batch: int             # 批末回流未配盒
    storage_peak_pct: float            # 暂存箱峰值占用率（%）
    unmatched_count: int               # 未匹配/尾料总数
    tail_count: int                    # 尾料计数
    sim_tick: int                      # 仿真 tick（步数或秒）
    wall_seconds: float                # 墙钟耗时（秒）
    carton_weight_min: int | None = None   # 盒重最小值（克）
    carton_weight_max: int | None = None   # 盒重最大值（克）
    carton_weight_avg: float | None = None # 盒重均值（克）

    def as_row(self) -> dict[str, Any]:
        """将汇总转为字典行，供 CSV/JSON 导出。"""
        return asdict(self)


def config_from_dict(data: dict[str, Any], default_name: str = "case") -> RunConfig:
    """
    从 JSON 用例 dict 构建 RunConfig。

    参数:
        data: JSON 中的单个用例对象
        default_name: 未指定 name 时的默认名

    支持字段: name, seed, stop_mode, stop_count/total, stop_weight_g/stop_weight_tons,
              specs/enabled_specs, move_timeout, cap_factor, timeout_clock, speed, verbose
    """
    stop_mode = str(data.get("stop_mode", STOP_MODE_COUNT))
    stop_weight_tons = float(
        data.get("stop_weight_tons", DEFAULT_STOP_WEIGHT_TONS)
    )
    specs_raw = data.get("specs") or data.get("enabled_specs")
    return RunConfig(
        name=str(data.get("name", default_name)),
        seed=int(data.get("seed", DEFAULT_SEED)),
        stop_mode=stop_mode,
        stop_count=int(data.get("stop_count", data.get("total", DEFAULT_TOTAL))),
        stop_weight_g=int(
            data.get("stop_weight_g", stop_weight_tons * 1_000_000)
        ),
        enabled_specs=resolve_specs(specs_raw),
        move_timeout=int(data.get("move_timeout", DEFAULT_MOVE_TIMEOUT)),
        cap_factor=int(data.get("cap_factor", DEFAULT_CAP_FACTOR)),
        timeout_clock=str(data.get("timeout_clock", DEFAULT_TIMEOUT_CLOCK)),
        speed=float(data.get("speed", DEFAULT_BATCH_SPEED)),
        verbose=bool(data.get("verbose", False)),
    )


def build_engine(cfg: RunConfig, *, quiet: bool = False) -> SchedulerEngine:
    """
    根据 RunConfig 构造 SchedulerEngine 实例。

    流程: 计算批次上限 → 加载/生成批次 CSV → 创建引擎（exclude_outside_stats=True）

    参数:
        cfg: 运行配置
        quiet: True 时抑制进度日志（log_every 设为极大值）
    """
    enabled = cfg.enabled_specs
    stop_count = max(1, cfg.stop_count)
    # batch_total：按结束条件需预加载的批次鱼条数上限
    batch_total = batch_total_for_run(
        cfg.stop_mode, stop_count, cfg.stop_weight_g, enabled_specs=enabled
    )
    records = load_or_generate_batch(
        seed=cfg.seed,
        total=batch_total,
        enabled_specs=enabled,
        stop_mode=cfg.stop_mode,
        stop_weight_g=cfg.stop_weight_g,
    )
    # log_every：每隔多少条入料打印一次进度
    log_every = 999_999_999 if quiet else max(50, batch_total // 50)
    return SchedulerEngine(
        batch_records=records,
        seed=cfg.seed,
        move_timeout=cfg.move_timeout,
        cap_factor=max(1, cfg.cap_factor),
        specs=enabled,
        log_every=log_every,
        stop_mode=cfg.stop_mode,
        stop_count=stop_count,
        stop_weight_g=cfg.stop_weight_g,
        timeout_clock=cfg.timeout_clock,
        verbose=cfg.verbose,
        exclude_outside_stats=True,
    )


def count_batch_tail_breakdown(engine: SchedulerEngine) -> dict[str, int]:
    """
    统计批末扫尾未配盒尾料（不含运行中超时/箱满/规格外）。

    返回:
        tail_lane_batch: 料道批末尾料数
        tail_storage_batch: 暂存箱批末尾料数
        tail_reflow_batch: 回流批末尾料数
        tail_batch_total: 三者合计
    """
    lane = storage = reflow = 0
    for trace in engine.tracker.unmatched:
        status = trace.status or ""
        if status == "unmatched_tail":
            lane += 1
        elif status == "unmatched_storage":
            storage += 1
        elif status == "unmatched_reflow":
            reflow += 1
    total = lane + storage + reflow
    return {
        "tail_lane_batch": lane,
        "tail_storage_batch": storage,
        "tail_reflow_batch": reflow,
        "tail_batch_total": total,
    }


def summarize(engine: SchedulerEngine, cfg: RunConfig, wall_seconds: float) -> RunSummary:
    """
    从引擎实例提取 RunSummary 汇总对象。

    参数:
        engine: 跑完（或跑至结束）的引擎
        cfg: 原始运行配置
        wall_seconds: 墙钟耗时
    """
    s = engine.stats  # Stats 累计统计对象
    if cfg.stop_mode == STOP_MODE_WEIGHT:
        target = f"{cfg.stop_weight_g / 1_000_000:.1f}t"
    else:
        target = str(cfg.stop_count)
    weights = [c.weight for c in engine.cartons]  # 所有成盒重量列表
    timeout_lane = s.timeout_tail
    timeout_total = timeout_lane + s.storage_timeout_tail
    cap = engine.lanes.storage_capacity
    peak = s.storage_max
    tails = count_batch_tail_breakdown(engine)
    return RunSummary(
        name=cfg.name,
        seed=cfg.seed,
        stop_mode=cfg.stop_mode,
        stop_target=target,
        enabled_specs=",".join(cfg.enabled_specs),
        input_count=s.input_count,
        input_weight_tons=round(s.input_weight / 1_000_000, 3),
        cartons=s.cartons,
        packed_fish=s.packed_fish,
        pack_rate_pct=round(s.packed_fish / s.input_count * 100, 2)
        if s.input_count
        else 0.0,
        outside_count=s.outside_count,
        timeout_total=timeout_total,
        timeout_lane=timeout_lane,
        reflow_count=s.reflow_count,
        timeout_tail=timeout_lane,
        overflow_reflow=s.overflow_reflow,
        storage_in=s.storage_in,
        storage_to_lane=s.storage_to_lane,
        storage_packed=s.storage_packed,
        storage_max=s.storage_max,
        storage_capacity=engine.lanes.storage_capacity,
        storage_timeout_tail=s.storage_timeout_tail,
        storage_batch_tail=s.storage_batch_tail,
        tail_batch_total=tails["tail_batch_total"],
        tail_lane_batch=tails["tail_lane_batch"],
        tail_storage_batch=tails["tail_storage_batch"],
        tail_reflow_batch=tails["tail_reflow_batch"],
        storage_peak_pct=round(peak / cap * 100, 1) if cap else 0.0,
        unmatched_count=s.unmatched_count,
        tail_count=s.tail_count,
        sim_tick=engine.tick,
        wall_seconds=round(wall_seconds, 2),
        carton_weight_min=min(weights) if weights else None,
        carton_weight_max=max(weights) if weights else None,
        carton_weight_avg=round(sum(weights) / len(weights), 1) if weights else None,
    )


def run_once(
    cfg: RunConfig,
    *,
    print_report: bool = False,
    archive: Path | None = None,
    quiet: bool = False,
) -> RunSummary:
    """
    执行一次完整仿真：process_one 循环 → finish_batch → 汇总。

    参数:
        cfg: 运行配置
        print_report: 是否在终端打印引擎报告
        archive: 若指定，将 CSV 产物复制到此目录
        quiet: 是否安静模式（传给 build_engine）

    返回:
        RunSummary 汇总对象
    """
    engine = build_engine(cfg, quiet=quiet)
    t0 = time.perf_counter()
    # throttle：每条鱼之间的 sleep 间隔（秒）；speed=0 时不限速
    throttle = 1.0 / cfg.speed if cfg.speed > 0 else 0.0
    while engine.process_one():
        if throttle:
            time.sleep(throttle)
    engine.finish_batch()
    elapsed = time.perf_counter() - t0
    summary = summarize(engine, cfg, elapsed)
    if print_report:
        engine.print_report()
        print_tail_storage_summary(summary)
    if archive:
        _archive_run_artifacts(engine.seed, cfg.name, archive)
    return summary


def _archive_run_artifacts(seed: int, case_name: str, out_dir: Path) -> None:
    """
    将 data/*_seed_{seed}.csv 复制到批量输出目录，避免多用例互相覆盖。

    参数:
        seed: 批次种子
        case_name: 用例名（用作文件名前缀）
        out_dir: 目标目录
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir = _root / "data"
    safe = case_name.replace("/", "-").replace("\\", "-")
    for suffix in ("run_report", "cartons", "remaining", "timeout_tail"):
        src = data_dir / f"{suffix}_seed_{seed}.csv"
        if src.is_file():
            shutil.copy2(src, out_dir / f"{safe}_{suffix}.csv")


def print_tail_storage_summary(s: RunSummary) -> None:
    """打印批末尾料与暂存箱峰值摘要。"""
    print(
        f"  批末未配盒 : {s.tail_batch_total} "
        f"(料道{s.tail_lane_batch} · 暂存{s.tail_storage_batch} · 回流{s.tail_reflow_batch})"
    )
    print(
        f"  暂存箱峰值 : {s.storage_max} / {s.storage_capacity} "
        f"({s.storage_peak_pct}%) · 入{s.storage_in} 回道{s.storage_to_lane} 成盒{s.storage_packed}"
    )


def print_summary_line(s: RunSummary) -> None:
    """打印单行摘要（quiet 模式用）。"""
    print(
        f"[{s.name}] seed={s.seed} {s.stop_mode}={s.stop_target} "
        f"specs={s.enabled_specs} | "
        f"入{s.input_count}({s.input_weight_tons}t) 盒{s.cartons} "
        f"装{s.packed_fish}({s.pack_rate_pct}%) | "
        f"批末尾{s.tail_batch_total}(料{s.tail_lane_batch}+箱{s.tail_storage_batch}+回{s.tail_reflow_batch}) "
        f"超时{s.timeout_total} | "
        f"暂存峰{s.storage_max}/{s.storage_capacity}({s.storage_peak_pct}%) | "
        f"{s.wall_seconds}s"
    )


def print_summary_table(rows: list[RunSummary]) -> None:
    """以对齐表格打印多组 RunSummary 对比。"""
    if not rows:
        return
    headers = [
        "name",
        "seed",
        "stop",
        "input",
        "tons",
        "cartons",
        "packed%",
        "批末尾",
        "料道尾",
        "暂存尾",
        "回流尾",
        "暂存峰",
        "峰值%",
        "timeout",
        "wall_s",
    ]
    table_rows: list[list[str]] = []
    for s in rows:
        table_rows.append(
            [
                s.name,
                str(s.seed),
                f"{s.stop_mode}={s.stop_target}",
                str(s.input_count),
                f"{s.input_weight_tons:.3f}",
                str(s.cartons),
                f"{s.pack_rate_pct:.1f}",
                str(s.tail_batch_total),
                str(s.tail_lane_batch),
                str(s.tail_storage_batch),
                str(s.tail_reflow_batch),
                f"{s.storage_max}/{s.storage_capacity}",
                f"{s.storage_peak_pct:.0f}",
                str(s.timeout_total),
                f"{s.wall_seconds:.1f}",
            ]
        )
    # widths：每列最大宽度，用于格式化对齐
    widths = [
        max(len(h), *(len(r[i]) for r in table_rows)) for i, h in enumerate(headers)
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in table_rows:
        print(fmt.format(*row))


def save_summary_csv(rows: list[RunSummary], path: Path) -> None:
    """将多组 RunSummary 写入 CSV 文件（UTF-8 BOM）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    fieldnames = list(rows[0].as_row().keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in rows:
            writer.writerow(s.as_row())


def save_summary_json(rows: list[RunSummary], path: Path) -> None:
    """将多组 RunSummary 写入 JSON 文件（含 generated_at 时间戳）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases": [s.as_row() for s in rows],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_matrix_file(path: Path) -> list[RunConfig]:
    """
    从 JSON 矩阵文件加载多组 RunConfig。

    JSON 格式: {"cases": [{...}, {...}]} 或直接为数组
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases", data)
    if not isinstance(cases, list):
        raise ValueError("矩阵文件需包含 cases 数组")
    return [config_from_dict(c, default_name=f"case_{i}") for i, c in enumerate(cases)]


# ---------------------------------------------------------------------------
# CLI 子命令
# ---------------------------------------------------------------------------
def _add_common_run_args(p: argparse.ArgumentParser) -> None:
    """为 run/preset/matrix 子命令添加通用仿真参数。"""
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子")
    p.add_argument(
        "--specs",
        default="default",
        help="启用规格：预设 module-a|module-b|module-c|default|all 或 15p,20p,...",
    )
    p.add_argument(
        "--move-timeout",
        type=int,
        default=DEFAULT_MOVE_TIMEOUT,
        help="队首超时阈值（步或秒，见 --timeout-clock）",
    )
    p.add_argument(
        "--cap-factor",
        type=int,
        default=DEFAULT_CAP_FACTOR,
        help="三合一扩容 N：容量 = min(装箱尾数)+N",
    )
    p.add_argument(
        "--timeout-clock",
        choices=[TIMEOUT_CLOCK_INTAKE, TIMEOUT_CLOCK_REAL],
        default=DEFAULT_TIMEOUT_CLOCK,
        help="超时计时方式",
    )
    p.add_argument(
        "--speed",
        type=float,
        default=DEFAULT_BATCH_SPEED,
        help="进料速率（条/秒），与 Web 模拟倍速一致；0=不限速（默认）",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="打印每条入料/封箱")
    p.add_argument(
        "--report",
        action="store_true",
        help="跑完后打印终端汇总（单跑默认开启）",
    )
    p.add_argument(
        "--artifacts-dir",
        type=Path,
        default=None,
        help="将 run_report/cartons/remaining 等 CSV 复制到此目录",
    )


def _add_stop_args(p: argparse.ArgumentParser) -> None:
    """添加互斥的结束条件参数：-n 按条数 / -w 按总重。"""
    g = p.add_mutually_exclusive_group()
    g.add_argument("-n", "--count", type=int, default=None, help="按条数结束")
    g.add_argument(
        "-w",
        "--weight",
        type=float,
        default=None,
        help="按总重结束（吨）",
    )


def _stop_from_args(args: argparse.Namespace) -> tuple[str, int, int]:
    """
    从 argparse 命名空间解析结束条件。

    返回:
        (stop_mode, stop_count, stop_weight_g) 三元组
    """
    if args.weight is not None:
        return STOP_MODE_WEIGHT, DEFAULT_TOTAL, int(args.weight * 1_000_000)
    count = args.count if args.count is not None else DEFAULT_TOTAL
    return STOP_MODE_COUNT, count, DEFAULT_STOP_WEIGHT_G


def cmd_run(args: argparse.Namespace) -> int:
    """子命令 run：执行单次仿真测试。"""
    stop_mode, stop_count, stop_weight_g = _stop_from_args(args)
    cfg = RunConfig(
        name=args.name or "run",
        seed=args.seed,
        stop_mode=stop_mode,
        stop_count=stop_count,
        stop_weight_g=stop_weight_g,
        enabled_specs=resolve_specs(args.specs),
        move_timeout=args.move_timeout,
        cap_factor=args.cap_factor,
        timeout_clock=args.timeout_clock,
        speed=max(0.0, float(args.speed)),
        verbose=args.verbose,
    )
    speed_hint = f" speed={cfg.speed}" if cfg.speed > 0 else ""
    print(
        f"开始 [{cfg.name}] seed={cfg.seed} {cfg.stop_mode} "
        f"specs={','.join(cfg.enabled_specs)}{speed_hint} ..."
    )
    summary = run_once(
        cfg,
        print_report=args.report or not args.quiet,
        archive=args.artifacts_dir,
        quiet=args.quiet,
    )
    if args.quiet and not args.report:
        print_summary_line(summary)
    return 0


def cmd_preset(args: argparse.Namespace) -> int:
    """子命令 preset：按模块预设批量对照运行。"""
    if args.list:
        print("可用规格预设：")
        for key, specs in SPEC_PRESETS.items():
            print(f"  {key:10s}  {', '.join(specs)}")
        return 0
    if not args.presets:
        print("请指定预设名，或使用 preset --list", file=sys.stderr)
        return 2
    stop_mode, stop_count, stop_weight_g = _stop_from_args(args)
    rows: list[RunSummary] = []
    for preset in args.presets:
        cfg = RunConfig(
            name=preset,
            seed=args.seed,
            stop_mode=stop_mode,
            stop_count=stop_count,
            stop_weight_g=stop_weight_g,
            enabled_specs=resolve_specs(preset),
            move_timeout=args.move_timeout,
            cap_factor=args.cap_factor,
            timeout_clock=args.timeout_clock,
            speed=max(0.0, float(args.speed)),
            verbose=args.verbose,
        )
        print(f"--- 预设 {preset} ({','.join(cfg.enabled_specs)}) ---")
        rows.append(
            run_once(
                cfg,
                print_report=False,
                archive=args.artifacts_dir,
                quiet=True,
            )
        )
    print("\n批量汇总：")
    print_summary_table(rows)
    out_dir = args.output_dir or (ROOT / "data" / "batch_runs")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_summary_csv(rows, out_dir / f"summary_{ts}.csv")
    save_summary_json(rows, out_dir / f"summary_{ts}.json")
    print(f"\n已写入 {out_dir / f'summary_{ts}.csv'}")
    return 0


def cmd_matrix(args: argparse.Namespace) -> int:
    """子命令 matrix：从 JSON 文件批量跑矩阵用例。"""
    path = Path(args.file)
    if not path.is_file():
        print(f"找不到矩阵文件: {path}", file=sys.stderr)
        return 2
    configs = load_matrix_file(path)
    rows: list[RunSummary] = []
    for cfg in configs:
        print(f"--- {cfg.name} ---")
        rows.append(
            run_once(
                cfg,
                print_report=args.report,
                archive=args.artifacts_dir,
                quiet=not args.report,
            )
        )
    print("\n矩阵汇总：")
    print_summary_table(rows)
    out_dir = args.output_dir or (ROOT / "data" / "batch_runs")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = path.stem
    save_summary_csv(rows, out_dir / f"summary_{tag}_{ts}.csv")
    save_summary_json(rows, out_dir / f"summary_{tag}_{ts}.json")
    print(f"\n已写入 {out_dir / f'summary_{tag}_{ts}.csv'}")
    return 0


def main() -> int:
    """CLI 主入口：解析子命令并分发执行。"""
    parser = argparse.ArgumentParser(
        description="智能分拣纯算法批量测试（无 Web 前端）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python batch_runner.py run -n 25000 --specs module-a --seed 42
  python batch_runner.py run -w 10 --specs module-c --move-timeout 180 --speed 50
  python batch_runner.py preset module-a module-b module-c -n 25000
  python batch_runner.py matrix --file plan/batch_cases.example.json
        """.strip(),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="单次测试")
    p_run.add_argument("--name", default="run", help="用例名称")
    p_run.add_argument("--quiet", action="store_true", help="仅一行摘要")
    _add_stop_args(p_run)
    _add_common_run_args(p_run)
    p_run.set_defaults(func=cmd_run)

    p_preset = sub.add_parser("preset", help="按模块预设批量对照")
    p_preset.add_argument(
        "presets",
        nargs="*",
        help="module-a / module-b / module-c / default / all",
    )
    p_preset.add_argument("--list", action="store_true", help="列出预设")
    p_preset.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="汇总 CSV/JSON 输出目录（默认 data/batch_runs）",
    )
    _add_stop_args(p_preset)
    _add_common_run_args(p_preset)
    p_preset.set_defaults(func=cmd_preset)

    p_matrix = sub.add_parser("matrix", help="从 JSON 文件跑矩阵用例")
    p_matrix.add_argument(
        "--file",
        type=Path,
        required=True,
        help="用例 JSON（见 plan/batch_cases.example.json）",
    )
    p_matrix.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="汇总 CSV/JSON 输出目录",
    )
    _add_common_run_args(p_matrix)
    p_matrix.set_defaults(func=cmd_matrix)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
