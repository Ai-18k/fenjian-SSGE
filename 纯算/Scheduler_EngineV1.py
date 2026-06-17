#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project : kaomam_project
@File : Scheduler_Engine.py
@Author : 18k
@Date : 2026/6/1 13:35
@Description: 智能分拣引擎 — 随机种子批次，DFS 自由组合配盒，三合一料道容量，超时回流

主流程：load_or_generate_batch → SchedulerEngine.process_one 循环 → finish_batch
批末产物：run_report / cartons / remaining / timeout_tail CSV（data/ 目录）
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 规格表 SPECS：键=规格名，值含 range(克重区间) 与 counts(合法装箱尾数)
# ---------------------------------------------------------------------------
SPECS: dict[str, dict] = {
    "15p": {"range": (566, 700), "counts": (7, 8)},
    "20p": {"range": (446, 565), "counts": (10, 11)},
    "25p": {"range": (366, 445), "counts": (12, 13, 14)},
    "30p": {"range": (306, 365), "counts": (15, 16)},
    "35p": {"range": (266, 305), "counts": (17, 18, 19)},
    "40p": {"range": (231, 265), "counts": (20, 21)},
    
    "45p": {"range": (211, 230), "counts": (22, 23)},
    "50p": {"range": (183, 210), "counts": (25, 26)},
    "60p": {"range": (153, 182), "counts": (30, 31)},
    "70p": {"range": (133, 152), "counts": (35, 36)},
    "80p": {"range": (116, 132), "counts": (40, 41)},
    "90p": {"range": (106, 115), "counts": (45, 46)},

    "100p": {"range": (96, 105), "counts": (50, 51)},
    "110p": {"range": (87, 95), "counts": (55, 56)},
    "120p": {"range": (80, 86), "counts": (60, 61)},
    "130p": {"range": (74, 79), "counts": (65, 66)},
    "140p": {"range": (69, 73), "counts": (70, 71)},
    "150p": {"range": (65, 68), "counts": (75, 76)},
}

# MODULE_SPECS：三大模块各自包含的规格
MODULE_SPECS: dict[str, tuple[str, ...]] = {
    "A": ("15p", "20p", "25p", "30p", "35p", "40p"),   # 轻规格模块
    "B": ("45p", "50p", "60p", "70p", "80p", "90p"),   # 中规格模块
    "C": ("100p", "110p", "120p", "130p", "140p", "150p"),  # 重规格模块
}

ALL_SPECS: tuple[str, ...] = tuple(SPECS.keys())           # 全部 18 规格
DEFAULT_ENABLED_SPECS: tuple[str, ...] = ("15p", "20p", "25p", "30p", "35p", "40p")  # 默认启用
DEMO_SPECS: tuple[str, ...] = DEFAULT_ENABLED_SPECS          # 演示用规格（同默认）

TARGET_MIN = 4980    # 盒重下限（克）
TARGET_MAX = 5030    # 盒重上限（克）
TARGET_MID = 5005    # 盒重中心值，评分用

BUCKETS = ("small", "medium", "large")                       # 小/中/大分区名
BUCKET_LABEL = {"small": "小", "medium": "中", "large": "大"}  # 分区中文标签

DEFAULT_TOTAL = 25000              # 默认入料条数
DEFAULT_SEED = 42                  # 默认随机种子
DEFAULT_MOVE_TIMEOUT = 30          # 默认队首超时阈值
DEFAULT_CAP_FACTOR = 1             # 三合一扩容：min(counts) + cap_factor（默认 +1）
DEFAULT_STORAGE_CAPACITY = 150     # 暂存箱容量上限（条）
STOP_MODE_COUNT = "count"          # 结束模式：按条数
STOP_MODE_WEIGHT = "weight"        # 结束模式：按总重
DEFAULT_STOP_WEIGHT_TONS = 10.0    # 默认按重结束目标（吨）
DEFAULT_STOP_WEIGHT_G = int(DEFAULT_STOP_WEIGHT_TONS * 1_000_000)  # 同上，单位克
TIMEOUT_CLOCK_INTAKE = "intake"    # 超时计时：每入料一步 +1
TIMEOUT_CLOCK_REAL = "real"        # 超时计时：墙钟秒
DEFAULT_TIMEOUT_CLOCK = TIMEOUT_CLOCK_INTAKE


def batch_total_for_run(
    stop_mode: str,
    stop_count: int,
    stop_weight_g: int,
    enabled_specs: tuple[str, ...] | list[str] | None = None,
) -> int:
    """按结束条件计算需预加载的批次上限（按总重时按启用规格最轻单尾估算条数）。"""
    if stop_mode != STOP_MODE_WEIGHT:
        return max(1, stop_count)
    enabled = normalize_enabled_specs(enabled_specs)
    min_inside = min(SPECS[spec]["range"][0] for spec in enabled)
    # 留 2% 余量应对 ~1% 超规鱼与区间下沿波动
    estimated = math.ceil(stop_weight_g / min_inside * 1.02) + 5000
    return max(DEFAULT_TOTAL, estimated)


def _load_module(name: str, path: Path):
    """作用：动态加载 plan/ 下 Python 脚本（细分规则、种子生成、深度搜索）。
    前端：无直接对应；为 classify_bucket、load_or_generate_batch、BoxPlanner 提供算法支撑。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_root = Path(__file__).resolve().parent  # 项目根目录 demo/
# 动态加载 plan/ 下算法子模块
_bucket_rules = _load_module("bucket_rules", _root / "plan" / "细分规则.py")
_seed_gen = _load_module("fish_seed_gen", _root / "plan" / "随机种子生成.py")
_depth_search = _load_module("depth_search", _root / "plan" / "深度搜索.py")
_demand_calc = _load_module("demand_calc", _root / "plan" / "计算需求.py")
dfs_find_best_from_items = _depth_search.dfs_find_best_from_items  # DFS 配盒入口
BoxDemandCalculator = _demand_calc.BoxDemandCalculator              # 动态需求计算器
_intersect_interval = _demand_calc.intersect_interval             # 区间交集

DFS_MAX_BUFFER = _depth_search.DEFAULT_DFS_MAX_BUFFER               # DFS 窗口上限
DFS_MAX_NODES = _depth_search.DEFAULT_DFS_MAX_NODES                 # DFS 节点上限
DFS_WINDOW_PER_BUCKET = 15                                          # 料道积压时每路取样条数


def dfs_max_buffer_for_spec(spec: str) -> int:
    """DFS 搜索窗口上限：小规格用默认 42；100p+ 需覆盖最大装箱尾数（如 150p→76）。"""
    counts = SPECS[spec]["counts"]
    return max(DFS_MAX_BUFFER, max(counts) + 5)


def dfs_window_per_bucket_for_spec(spec: str) -> int:
    """料道积压时各路取样条数，须能凑够该规格最小装箱尾数。"""
    return max(DFS_WINDOW_PER_BUCKET, math.ceil(dfs_max_buffer_for_spec(spec) / 3))

BUCKET_RANGES = {}  # 各规格的小/中/大克重区间 {spec: BucketRange}
for spec in ALL_SPECS:
    lo, hi = SPECS[spec]["range"]
    counts = SPECS[spec]["counts"]
    primary = counts[len(counts) // 2]
    try:
        BUCKET_RANGES[spec] = _bucket_rules.calc_bucket_split(
            (lo, hi), primary_count=primary
        )
    except ValueError:
        span = hi - lo + 1
        t1 = lo + span // 3 - 1
        t2 = lo + 2 * span // 3 - 1
        BUCKET_RANGES[spec] = _bucket_rules.BucketRange(
            small=(lo, t1),
            medium=(t1 + 1, t2),
            large=(t2 + 1, hi),
        )


def bucket_weight_range(spec: str, bucket: str) -> tuple[int, int]:
    """返回某规格某分区（small/medium/large）的克重区间。"""
    br = BUCKET_RANGES[spec]
    if bucket == "small":
        return br.small
    if bucket == "medium":
        return br.medium
    return br.large


def weight_in_ranges(
    weight: int,
    ranges: list[tuple[int, int]] | list[list[int]],
) -> bool:
    """判断克重是否落在任一闭区间内。"""
    for r in ranges:
        lo, hi = int(r[0]), int(r[1])
        if lo <= weight <= hi:
            return True
    return False


def spec_inventory_weights(lanes: "SortingLanes", spec: str) -> list[int]:
    """某规格料道+暂存箱内全部鱼的克重列表。"""
    weights: list[int] = []
    if spec in lanes.queues:
        for b in BUCKETS:
            weights.extend(f.weight for f in lanes.queues[spec][b])
    weights.extend(f.weight for f in lanes.storage if f.spec == spec)
    return weights


def lane_inventory_weights(lanes: "SortingLanes", spec: str) -> list[int]:
    """某规格三路料道内鱼重量（不含暂存箱）。"""
    weights: list[int] = []
    if spec in lanes.queues:
        for b in BUCKETS:
            weights.extend(f.weight for f in lanes.queues[spec][b])
    return weights


def diagnostic_need_weight_ranges(
    lanes: "SortingLanes",
    spec: str,
) -> list[tuple[int, int]] | None:
    """满容仍无解时，按偏轻/偏重诊断收窄到最缺的小/中/大分区。"""
    q = lanes.queues.get(spec)
    if not q:
        return None
    q_small, q_medium, q_large = q["small"], q["medium"], q["large"]
    total = len(q_small) + len(q_medium) + len(q_large)
    min_cnt = min(SPECS[spec]["counts"])
    if total < min_cnt:
        return None
    p_small = prefix_weights(q_small)
    p_medium = prefix_weights(q_medium)
    p_large = prefix_weights(q_large)
    best: dict | None = None
    for count in SPECS[spec]["counts"]:
        for a in range(min(len(q_small), count) + 1):
            for b in range(min(len(q_medium), count - a) + 1):
                c = count - a - b
                if c > len(q_large):
                    continue
                weight = p_small[a] + p_medium[b] + p_large[c]
                if weight < TARGET_MIN:
                    diff = TARGET_MIN - weight
                elif weight > TARGET_MAX:
                    diff = weight - TARGET_MAX
                else:
                    diff = 0
                score = diff * 10 + abs(weight - TARGET_MID)
                if best is None or score < best["score"]:
                    best = {"diff": diff, "score": score, "weight": weight}
    if not best or best["diff"] == 0:
        return None
    if best["weight"] < TARGET_MIN:
        return [bucket_weight_range(spec, "large")]
    if best["weight"] > TARGET_MAX:
        return [bucket_weight_range(spec, "small")]
    return None


def spec_demand_weight_ranges(
    lanes: "SortingLanes",
    spec: str,
) -> list[tuple[int, int]]:
    """规格广播用的重量区间：三合一库存动态需求（不拆小/中/大）。"""
    lo, hi = SPECS[spec]["range"]
    weights = spec_inventory_weights(lanes, spec)
    if not weights:
        return [(lo, hi)]
    demand = BoxDemandCalculator(spec, weights).calc()
    if demand.next_fish_ranges:
        return list(demand.next_fish_ranges)
    narrowed = diagnostic_need_weight_ranges(lanes, spec)
    if narrowed:
        return narrowed
    return [(lo, hi)]


def lane_demand_weight_ranges(
    lanes: "SortingLanes",
    spec: str,
    bucket: str,
) -> list[tuple[int, int]]:
    """封箱出库用：规格动态需求 ∩ 小/中/大分区区间。"""
    bucket_rng = bucket_weight_range(spec, bucket)
    for rlo, rhi in spec_demand_weight_ranges(lanes, spec):
        hit = _intersect_interval((rlo, rhi), bucket_rng)
        if hit:
            return [hit]
    return [bucket_rng]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Fish:
    """单条鱼的运行时实体。"""

    id: int                      # 鱼 ID（批次序号）
    weight: int                  # 克重
    spec: str | None = None      # 规格名（规格外为 None）
    bucket: str | None = None    # 小/中/大分区
    enter_time: int = 0          # 进入当前队列的 tick
    rounds: int = 1              # 在系统中的轮次（回流 +1）


@dataclass
class FishTrace:
    """单条鱼全生命周期追踪记录。"""

    fish_id: int                              # 鱼 ID
    weight: int                               # 克重
    spec: str | None                          # 规格名
    rounds: int = 1                           # 轮次
    first_in_time: int | None = None          # 首次入系统 tick
    outbound_time: int | None = None          # 出站 tick（封箱或尾料）
    status: str = "pending"                   # 状态（queued/packed/unmatched_* 等）
    reflow_reasons: list[str] = field(default_factory=list)  # 回流原因列表
    bucket: str | None = None                 # 最后所在分区
    lane_wait_s: int | None = None            # 料道/暂存等待秒数（超时时记录）

    @property
    def dwell_time(self) -> int | None:
        """作用：计算鱼在系统中的停留时长（秒）。
        前端：index.html 尾料弹窗 dwell_time 列；GET /api/remaining。"""
        if self.first_in_time is None:
            return None
        end = self.outbound_time if self.outbound_time is not None else self.first_in_time
        return end - self.first_in_time


@dataclass
class BoxPlan:
    """一次成功封箱的方案。"""

    spec: str                                    # 规格名
    count: int                                   # 尾数
    weight: int                                  # 总重（克）
    parts: dict[str, int]                        # 小/中/大配比 {bucket: count}
    fish: list[Fish] = field(default_factory=list) # 入选鱼列表（封箱后填充）
    pick_ids: frozenset[int] | None = None       # DFS 自由组合：按鱼 ID 从料道移除


@dataclass
class Stats:
    """引擎累计统计。"""

    input_count: int = 0              # 入料条数
    input_weight: int = 0             # 入料总重（克）
    packed_fish: int = 0              # 装箱鱼条数
    cartons: int = 0                  # 成盒数
    outside_count: int = 0            # 规格外条数
    reflow_count: int = 0             # 回流次数
    timeout_tail: int = 0             # 料道超时尾料
    overflow_reflow: int = 0          # 超容回流次数
    storage_in: int = 0                 # 暂存箱入箱次数
    storage_to_lane: int = 0          # 暂存箱回料道次数
    storage_packed: int = 0           # 从暂存箱直接成盒次数
    storage_timeout_tail: int = 0       # 暂存箱超时尾料
    storage_full_tail: int = 0          # 暂存箱已满尾料
    storage_batch_tail: int = 0         # 暂存箱批末尾料
    storage_max: int = 0                # 暂存箱历史峰值
    unmatched_count: int = 0            # 未匹配/尾料总数
    tail_count: int = 0                 # 尾料计数


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def expand_spec_list(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """作用：展开规格列表；兼容 query 中 enabled_specs=a,b,c 被解析成单元素的情况。
    前端：fifo_monitor.html「启用规格」勾选；POST /api/start 的 enabled_specs 参数解析。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [raw]
    else:
        items = [str(s) for s in raw]
    out: list[str] = []
    for item in items:
        for part in item.split(","):
            name = part.strip()
            if name:
                out.append(name)
    return out


def normalize_enabled_specs(
    enabled_specs: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, ...]:
    """作用：校验并规范化启用规格列表，无效时回退 DEFAULT_ENABLED_SPECS。
    前端：fifo_monitor.html 规格芯片；GET /api/config.default_enabled_specs；POST /api/start。"""
    if not enabled_specs:
        return DEFAULT_ENABLED_SPECS
    valid = tuple(s for s in expand_spec_list(enabled_specs) if s in SPECS)
    return valid or DEFAULT_ENABLED_SPECS


def classify_spec(
    weight: int,
    enabled: set[str] | None = None,
) -> str | None:
    """作用：按克重归入规格（15p~150p）；未启用规格返回 None。
    前端：fifo_monitor.html 入料路由与料道分配；index.html 模块库存按规格统计。"""
    for name, info in SPECS.items():
        lo, hi = info["range"]
        if lo <= weight <= hi:
            if enabled is None or name in enabled:
                return name
            return None
    return None


def enabled_specs_tag(enabled_specs: tuple[str, ...]) -> str:
    """作用：将启用规格列表编码为文件名标签（如 15p-20p-25p）。
    前端：无直接 UI；决定 data/fish_seed_{seed}_en_{tag}.csv 批次文件路径。"""
    return "-".join(enabled_specs)


def batch_csv_path(
    seed: int,
    enabled_specs: tuple[str, ...],
    total: int = DEFAULT_TOTAL,
) -> Path:
    """作用：根据种子、目标条数与启用规格生成批次 CSV 路径。
    前端：GET /api/batch 加载动画鱼序列；index/fifo_monitor 开始模拟前的批次源。"""
    tag = enabled_specs_tag(enabled_specs)
    return _root / "data" / f"fish_seed_{seed}_n{total}_en_{tag}.csv"


def batch_csv_path_for_weight(
    seed: int,
    enabled_specs: tuple[str, ...],
    stop_weight_g: int,
) -> Path:
    """按总重结束条件时的批次 CSV 路径（与按条数缓存分离）。"""
    tag = enabled_specs_tag(enabled_specs)
    return _root / "data" / f"fish_seed_{seed}_wg{stop_weight_g}_en_{tag}.csv"


def _batch_valid_for_enabled(
    records: list,
    enabled_specs: tuple[str, ...],
) -> bool:
    """作用：校验缓存批次是否匹配当前启用规格（规格内 + 相对启用的超规鱼）。
    前端：GET /api/batch 命中缓存前的校验；fifo_monitor 切换启用规格后重载批次。"""
    enabled_set = set(enabled_specs)
    for r in records:
        if r.outside:
            if not _seed_gen.is_outside_weight(r.weight, enabled_specs):
                return False
            continue
        spec = classify_spec(r.weight, enabled_set)
        if not spec:
            return False
        if r.spec and r.spec not in enabled_set:
            return False
    return True


def classify_bucket(spec: str, weight: int) -> str:
    """作用：将鱼按克重归入小/中/大（small/medium/large）料道。
    前端：fifo_monitor.html 三路料道动画与克数标注；GET /api/config.bucket_ranges。"""
    return _bucket_rules.bucket_of(weight, BUCKET_RANGES[spec])


def prefix_weights(fish_list: list[Fish]) -> list[int]:
    """作用：计算料道鱼重量前缀和，用于快速枚举封箱组合重量。
    前端：无直接 UI；支撑 fifo_monitor「需求地址」偏轻/偏重诊断与 collect_demands。"""
    p = [0]
    for f in fish_list:
        p.append(p[-1] + f.weight)
    return p


def spec_min_count(spec: str) -> int:
    """作用：该规格合法装箱尾数的最小值（默认料道容量基准）。"""
    return min(SPECS[spec]["counts"])


def spec_total_capacity(spec: str, cap_factor: int = DEFAULT_CAP_FACTOR) -> int:
    """某规格三合一料道总容量 = min(counts) + cap_factor。"""
    return spec_min_count(spec) + max(1, cap_factor)


def record_to_fish(
    record,
    tick: int,
    enabled: set[str] | None = None,
) -> Fish:
    """
    将批次 CSV 记录 (FishRecord) 转为运行时 Fish 对象。

    参数:
        record: 批次记录，含 id/weight/spec/outside
        tick: 当前仿真 tick，写入 fish.enter_time
        enabled: 启用规格集合；用于重新 classify_spec（批次缓存校验）

    变量:
        spec: 规格名；outside 或不在 enabled 内则为 None
        bucket: 小/中/大分区，由 classify_bucket 按克重划分

    返回:
        Fish 运行时实体
    """
    if record.outside:
        spec = None
    elif enabled is not None:
        spec = classify_spec(record.weight, enabled)
    else:
        spec = record.spec
    bucket = classify_bucket(spec, record.weight) if spec else None
    return Fish(
        id=record.id,
        weight=record.weight,
        spec=spec,
        bucket=bucket,
        enter_time=tick,
        rounds=1,
    )


def _load_batch_csv(csv_path: Path) -> list:
    """作用：从磁盘读取 fish_seed CSV 为 FishRecord 列表。
    前端：GET /api/batch 读缓存批次；fifo_monitor.html loadBatch() 拉取鱼序列。"""
    records = []
    with csv_path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            records.append(
                _seed_gen.FishRecord(
                    id=int(row["id"]),
                    weight=int(row["weight"]),
                    spec=row["spec"] or None,
                    outside=bool(int(row["outside"])),
                )
            )
    return records


def load_or_generate_batch(
    seed: int = DEFAULT_SEED,
    total: int = DEFAULT_TOTAL,
    csv_path: Path | None = None,
    enabled_specs: tuple[str, ...] | list[str] | None = None,
    stop_mode: str = STOP_MODE_COUNT,
    stop_weight_g: int = DEFAULT_STOP_WEIGHT_G,
) -> list:
    """作用：按结束条件生成批次（按条数或按总重；约 1% 相对启用的超规鱼）。
    前端：GET /api/batch；fifo_monitor.html「加载批次」；index.html 开始模拟前预生成 data/fish_seed_*.csv。"""
    enabled = normalize_enabled_specs(enabled_specs)
    if stop_mode == STOP_MODE_WEIGHT:
        stop_weight_g = max(1, int(stop_weight_g))
        csv_path = csv_path or batch_csv_path_for_weight(seed, enabled, stop_weight_g)
        if csv_path.exists():
            cached = _load_batch_csv(csv_path)
            if cached and _batch_valid_for_enabled(cached, enabled):
                cached_weight = sum(r.weight for r in cached)
                if cached_weight >= stop_weight_g:
                    return cached
        max_fish = batch_total_for_run(
            stop_mode, total, stop_weight_g, enabled_specs=enabled
        )
        records, summary = _seed_gen.generate_fish_batch_by_weight(
            target_weight_g=stop_weight_g,
            seed=seed,
            enabled_specs=enabled,
            max_fish=max_fish,
        )
        if summary.total_weight < stop_weight_g:
            raise ValueError(
                f"按总重生成批次未达目标：{summary.total_weight / 1_000_000:.3f}t / "
                f"{stop_weight_g / 1_000_000:.3f}t（{summary.total} 条，上限 {max_fish}）"
                f" · 启用规格 {', '.join(enabled)} 最轻 {min(SPECS[s]['range'][0] for s in enabled)}g/尾"
            )
        _seed_gen.save_csv(records, csv_path)
        return records

    csv_path = csv_path or batch_csv_path(seed, enabled, total)
    if csv_path.exists():
        cached = _load_batch_csv(csv_path)[:total]
        if len(cached) >= total and _batch_valid_for_enabled(cached, enabled):
            return cached[:total]
    records, _ = _seed_gen.generate_fish_batch(
        total=total,
        seed=seed,
        enabled_specs=enabled,
    )
    _seed_gen.save_csv(records, csv_path)
    return records[:total]


# ---------------------------------------------------------------------------
# 尾料 / 成盒明细
# ---------------------------------------------------------------------------
# TAIL_STATUS_LABEL：尾料状态码 → 中文说明
TAIL_STATUS_LABEL = {
    "unmatched_tail": "批末料道未配盒",
    "unmatched_reflow": "回流后未配盒",
    "unmatched_outside": "规格外",
    "unmatched_timeout": "超时尾料",
    "unmatched_storage": "暂存箱批末未配盒",
    "unmatched_storage_timeout": "暂存箱超时尾料",
    "unmatched_storage_full": "暂存箱已满",
    "stored": "暂存箱在库",
}

# 批末扫尾未配盒（不含超时/规格外/箱满）
_BATCH_TAIL_STATUSES = ("unmatched_tail", "unmatched_storage", "unmatched_reflow")
# 超时尾料
_TIMEOUT_STATUSES = ("unmatched_timeout", "unmatched_storage_timeout")
# 暂存箱爆满尾料
_STORAGE_FULL_STATUSES = ("unmatched_storage_full",)


def sum_unmatched_traces(
    traces: list[FishTrace],
    statuses: tuple[str, ...],
) -> tuple[int, int]:
    """按状态统计尾料条数与总重（克）。"""
    count = 0
    weight = 0
    for t in traces:
        if (t.status or "") in statuses:
            count += 1
            weight += t.weight
    return count, weight


def _fmt_weight_g(weight_g: int) -> str:
    """格式化克重：带千分位，并附吨数。"""
    tons = weight_g / 1_000_000
    return f"{weight_g:,}g ({tons:.3f}t)"


def describe_tail_trace(
    trace: FishTrace,
    end_tick: int | None = None,
    batch_seed: int | None = None,
) -> dict:
    """作用：解析尾料未匹配原因（批末/回流/规格外/超时）及超容回流摘要。
    前端：index.html「尾料数据」弹窗各列；GET /api/remaining、/api/state.remaining_fish。"""
    reasons = list(trace.reflow_reasons)
    had_timeout = trace.status in (
        "unmatched_timeout",
        "unmatched_storage_timeout",
    ) or "timeout" in reasons
    had_overflow = "overflow" in reasons
    status = trace.status or ""
    tail_cause = TAIL_STATUS_LABEL.get(status, status or "未知")

    reflow_parts: list[str] = []
    if had_overflow:
        reflow_parts.append("超容回流")
    reflow_summary = "、".join(reflow_parts) if reflow_parts else "无"

    dwell_time: int | None = None
    if trace.first_in_time is not None:
        if trace.outbound_time is not None:
            dwell_time = trace.outbound_time - trace.first_in_time
        elif end_tick is not None:
            dwell_time = end_tick - trace.first_in_time

    return {
        "tail_cause": tail_cause,
        "reflow_summary": reflow_summary,
        "had_timeout": had_timeout,
        "had_overflow": had_overflow,
        "dwell_time": dwell_time,
        "first_in_time": trace.first_in_time,
        "outbound_time": trace.outbound_time,
        "bucket": trace.bucket or "",
        "lane_wait_s": trace.lane_wait_s,
        "batch_seed": batch_seed,
    }


# ---------------------------------------------------------------------------
# 追踪器
# ---------------------------------------------------------------------------
class FishTracker:
    """作用：管理全批次每条鱼的追踪状态（入队、封箱、回流、尾料）。
    前端：GET /api/report 全量追踪；index.html「尾料数据」；fifo_monitor 回流/尾料统计分项。"""

    def __init__(self) -> None:
        """作用：初始化空追踪表。
        前端：引擎创建时调用，无直接 UI。"""
        self.traces: dict[int, FishTrace] = {}
        self.unmatched: list[FishTrace] = []

    def register(self, fish: Fish, tick: int, status: str = "queued") -> None:
        """作用：登记鱼首次入系统或更新轮次/状态（入队、规格外等）。
        前端：入料时隐式更新；GET /api/report 的 status 字段来源。"""
        if fish.id not in self.traces:
            self.traces[fish.id] = FishTrace(
                fish_id=fish.id,
                weight=fish.weight,
                spec=fish.spec,
                rounds=fish.rounds,
                first_in_time=tick,
                status=status,
            )
        else:
            trace = self.traces[fish.id]
            trace.rounds = fish.rounds
            trace.status = status

    def mark_packed(self, fish: Fish, tick: int) -> None:
        """作用：标记鱼已成功装入成盒并记录出站时间。
        前端：index.html「完成箱数/装箱鱼」；GET /api/cartons 的 fish_ids。"""
        trace = self.traces[fish.id]
        trace.rounds = fish.rounds
        trace.outbound_time = tick
        trace.status = "packed"

    def mark_reflow(self, fish: Fish, tick: int, reason: str) -> None:
        """作用：标记鱼因超容回流，记录原因（overflow）。
        前端：两页「回流/尾料」分项；GET /api/state.overflow_reflow。"""
        trace = self.traces.get(fish.id)
        if trace is None:
            self.register(fish, tick, status="reflow")
            trace = self.traces[fish.id]
        trace.rounds = fish.rounds
        trace.status = "reflow"
        trace.reflow_reasons.append(reason)

    def mark_timeout_tail(self, fish: Fish, tick: int, lane_wait_s: int, status: str = "unmatched_timeout") -> None:
        """作用：超时鱼直接记为尾料（不进回流），记录料道/暂存等待时长。
        前端：index.html「尾料数据」「超时尾料」；GET /api/state.timeout_tail_log。"""
        trace = self.traces.get(fish.id)
        if trace is None:
            self.register(fish, tick, status=status)
            trace = self.traces[fish.id]
        trace.rounds = fish.rounds
        trace.status = status
        trace.outbound_time = tick
        trace.bucket = fish.bucket
        trace.lane_wait_s = lane_wait_s
        if trace not in self.unmatched:
            self.unmatched.append(trace)

    def mark_stored(self, fish: Fish, tick: int) -> None:
        """作用：超容鱼进入暂存箱，记录入箱时刻。
        前端：fifo_monitor 暂存箱可视化；GET /api/state.storage_box。"""
        trace = self.traces.get(fish.id)
        if trace is None:
            self.register(fish, tick, status="stored")
            trace = self.traces[fish.id]
        trace.rounds = fish.rounds
        trace.status = "stored"
        trace.bucket = fish.bucket

    def mark_unmatched(self, fish: Fish, status: str, tick: int | None = None) -> None:
        """作用：批末将未配盒鱼标为尾料（unmatched_tail/reflow/outside）。
        前端：index.html「尾料数据」；fifo_monitor「规格外尾料箱」；GET /api/remaining。"""
        trace = self.traces.get(fish.id)
        if trace is None:
            self.register(fish, tick or 0, status=status)
            trace = self.traces[fish.id]
        trace.rounds = fish.rounds
        trace.status = status
        if tick is not None:
            trace.outbound_time = tick
        if fish.bucket and not trace.bucket:
            trace.bucket = fish.bucket
        if trace not in self.unmatched:
            self.unmatched.append(trace)

    def save_report(self, path: Path) -> None:
        """作用：导出全批次鱼生命周期 CSV（run_report_seed_{seed}.csv）。
        前端：index.html「下载报告」按钮；GET /api/report。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "fish_id",
                    "weight",
                    "spec",
                    "rounds",
                    "first_in_time",
                    "outbound_time",
                    "dwell_time",
                    "status",
                    "reflow_reasons",
                ],
            )
            writer.writeheader()
            for t in sorted(self.traces.values(), key=lambda x: x.fish_id):
                writer.writerow(
                    {
                        "fish_id": t.fish_id,
                        "weight": t.weight,
                        "spec": t.spec or "",
                        "rounds": t.rounds,
                        "first_in_time": t.first_in_time if t.first_in_time is not None else "",
                        "outbound_time": t.outbound_time if t.outbound_time is not None else "",
                        "dwell_time": t.dwell_time if t.dwell_time is not None else "",
                        "status": t.status,
                        "reflow_reasons": "|".join(t.reflow_reasons),
                    }
                )

    def remaining_records(
        self,
        end_tick: int | None = None,
        batch_seed: int | None = None,
    ) -> list[dict]:
        """作用：汇总所有未成盒尾料的 JSON 记录（含原因与停留时长）。
        前端：index.html「尾料数据」弹窗；GET /api/remaining、/api/state.remaining_fish。"""
        return [
            {
                "fish_id": t.fish_id,
                "weight": t.weight,
                "spec": t.spec or "",
                "rounds": t.rounds,
                "status": t.status,
                "reflow_reasons": list(t.reflow_reasons),
                **describe_tail_trace(t, end_tick, batch_seed),
            }
            for t in sorted(list(self.unmatched), key=lambda x: x.fish_id)
        ]


# ---------------------------------------------------------------------------
# 料道 & 装箱
# ---------------------------------------------------------------------------
class SortingLanes:
    """作用：管理全部规格的小/中/大料道队列、回流队列与规格外队列。
    前端：fifo_monitor.html 料道画布与库存可视化；index.html A/B/C 模块库存表。"""

    def __init__(self, specs: tuple[str, ...] = DEMO_SPECS):
        """作用：按启用规格初始化空料道结构。
        前端：POST /api/start 创建引擎时；GET /api/state.modules 库存数据源。"""
        self.specs = specs
        self.queues: dict[str, dict[str, list[Fish]]] = {
            spec: {b: [] for b in BUCKETS} for spec in specs
        }
        self.outside: list[Fish] = []
        self.reflow: list[Fish] = []
        self.storage: list[Fish] = []
        self.storage_capacity = DEFAULT_STORAGE_CAPACITY

    def storage_for_spec(self, spec: str) -> list[Fish]:
        """作用：取暂存箱内某规格的鱼（入箱仍 FIFO append；出库按需求区间匹配）。
        前端：GET /api/state.storage_box.by_spec。"""
        return [f for f in self.storage if f.spec == spec]

    def storage_count(self) -> int:
        """暂存箱当前鱼条数。"""
        return len(self.storage)

    def bucket_fish(self, spec: str, bucket: str) -> list[Fish]:
        """作用：料道 FIFO 视图（暂存鱼经需求匹配出库后进入料道，不在此合并）。"""
        return self.queues[spec][bucket]

    def pick_storage_matching(
        self,
        spec: str,
        bucket: str,
        weight_ranges: list[tuple[int, int]],
        limit: int,
    ) -> list[Fish]:
        """按广播需求重量区间从暂存箱取鱼（非队头顺序，优先等待最久者）。"""
        if limit <= 0:
            return []
        candidates = [
            f
            for f in self.storage
            if f.spec == spec
            and f.bucket == bucket
            and weight_in_ranges(f.weight, weight_ranges)
        ]
        candidates.sort(key=lambda f: f.enter_time)
        picked_ids = {f.id for f in candidates[:limit]}
        return self.remove_from_storage_ids(picked_ids)

    def pick_storage_matching_spec(
        self,
        spec: str,
        weight_ranges: list[tuple[int, int]],
        limit: int,
    ) -> list[Fish]:
        """按规格广播重量区间从暂存箱取鱼（不限小/中/大，优先等待最久）。"""
        if limit <= 0:
            return []
        candidates = [
            f
            for f in self.storage
            if f.spec == spec and weight_in_ranges(f.weight, weight_ranges)
        ]
        candidates.sort(key=lambda f: f.enter_time)
        picked_ids = {f.id for f in candidates[:limit]}
        return self.remove_from_storage_ids(picked_ids)

    def transfer_storage_to_lane(
        self,
        spec: str,
        bucket: str,
        weight_ranges: list[tuple[int, int]],
        limit: int,
        tick: int,
        tracker: FishTracker,
        cap_factor: int = DEFAULT_CAP_FACTOR,
    ) -> list[Fish]:
        """暂存箱 → 料道：满足需求区间的鱼直接出库入道（不要求 FIFO 队头）。"""
        lane = self.queues[spec][bucket]
        room = max(0, spec_total_capacity(spec, cap_factor) - self.total_in_spec(spec))
        take = min(limit, room)
        if take <= 0:
            return []
        picked = self.pick_storage_matching(spec, bucket, weight_ranges, take)
        for fish in picked:
            fish.enter_time = tick
            lane.append(fish)
            tracker.register(fish, tick, status="queued")
        return picked

    def transfer_storage_for_spec(
        self,
        spec: str,
        weight_ranges: list[tuple[int, int]],
        limit: int,
        tick: int,
        tracker: FishTracker,
        cap_factor: int = DEFAULT_CAP_FACTOR,
    ) -> list[Fish]:
        """暂存箱 → 规格料道：按广播区间出库，自动落入对应小/中/大分区。"""
        room = max(0, spec_total_capacity(spec, cap_factor) - self.total_in_spec(spec))
        take = min(limit, room)
        if take <= 0:
            return []
        picked = self.pick_storage_matching_spec(spec, weight_ranges, take)
        for fish in picked:
            fish.enter_time = tick
            bucket = fish.bucket or classify_bucket(spec, fish.weight)
            lane = self.queues[spec][bucket]
            lane.append(fish)
            tracker.register(fish, tick, status="queued")
        return picked

    def try_push_storage(self, fish: Fish, tick: int, tracker: FishTracker) -> bool:
        """
        将鱼 append 到暂存箱尾部（process_one → _push_intake_storage 调用）。

        满容（len(storage) >= storage_capacity）或 spec 为空时返回 False。
        成功时更新 fish.enter_time 并 tracker.mark_stored。
        """
        if fish.spec is None or len(self.storage) >= self.storage_capacity:
            return False
        fish.enter_time = tick
        self.storage.append(fish)
        tracker.mark_stored(fish, tick)
        return True

    def pop_storage_head(self) -> Fish | None:
        """弹出暂存箱队头鱼（FIFO）。"""
        if not self.storage:
            return None
        return self.storage.pop(0)

    def remove_from_storage_ids(self, fish_ids: set[int]) -> list[Fish]:
        """按 ID 集合从暂存箱移除鱼，返回被移除列表。"""
        removed: list[Fish] = []
        kept: list[Fish] = []
        for fish in self.storage:
            if fish.id in fish_ids:
                removed.append(fish)
            else:
                kept.append(fish)
        self.storage = kept
        return removed

    def total_available_for_spec(self, spec: str) -> int:
        """某规格料道+暂存箱可用鱼总数。"""
        return self.total_in_spec(spec) + len(self.storage_for_spec(spec))

    def _put_in_lane(self, fish: Fish, tick: int) -> str:
        """作用：将鱼放入对应规格+小中大料道队尾。
        前端：fifo_monitor.html 鱼入料道动画；index.html 模块 small/medium/large 计数。"""
        fish.enter_time = tick
        self.queues[fish.spec][fish.bucket].append(fish)
        return fish.bucket

    def enqueue(self, fish: Fish, tick: int, tracker: FishTracker) -> str:
        """
        入料主入口（process_one 规格外路由 & _route_spec_intake 入道）。

        规格外 → outside 队列，status=unmatched_outside，返回 "outside"；
        规格内 → _put_in_lane 放入对应小/中/大料道队尾，返回 bucket 名。
        """
        if fish.spec is None or fish.spec not in self.queues:
            fish.enter_time = tick
            self.outside.append(fish)
            tracker.register(fish, tick, status="unmatched_outside")
            return "outside"
        bucket = self._put_in_lane(fish, tick)
        tracker.register(fish, tick, status="queued")
        return bucket

    def try_enqueue_reflow(self, fish: Fish, tick: int, tracker: FishTracker, cap_factor: int = DEFAULT_CAP_FACTOR) -> bool:
        """
        回流鱼二次入道（process_one → _process_reflow_intake 调用）。

        料道三合一未满则 _put_in_lane 成功返回 True；满则 False 留待下步。
        """
        if fish.spec is None or fish.spec not in self.queues:
            return False
        if self.total_in_spec(fish.spec) >= spec_total_capacity(fish.spec, cap_factor):
            return False
        self._put_in_lane(fish, tick)
        tracker.register(fish, tick, status="queued")
        return True

    def total_in_spec(self, spec: str) -> int:
        """作用：统计某规格三路料道鱼总数。
        前端：index.html 模块库存 total 列；封箱前置条件判断。"""
        return sum(len(self.queues[spec][b]) for b in BUCKETS)

    @staticmethod
    def _sync_head_enter_time(lane: list[Fish], tick: int) -> None:
        """作用：队头变更后重置队头 enter_time，超时只统计担任队头的等待时间。
        前端：fifo_monitor「队首超时」控制项；GET /api/state 超时回流日志。"""
        if lane:
            lane[0].enter_time = tick

    def remove_plan(self, plan: BoxPlan, tick: int, tracker: FishTracker) -> list[Fish]:
        """
        按封箱方案从暂存箱+料道移除鱼（process_one → _apply_box_plan 调用）。

        plan.pick_ids 非空时按 ID 精确移除（DFS 自由组合）；
        否则按 plan.parts 小/中/大数量从队头前缀取（兼容旧逻辑）。
        移除后 tracker.mark_packed，plan.fish 填入实际移除列表。
        """
        removed: list[Fish] = []
        if plan.pick_ids:
            targets = set(plan.pick_ids)
            removed.extend(self.remove_from_storage_ids(targets))
            for bucket in BUCKETS:
                lane = self.queues[plan.spec][bucket]
                picked = [f for f in lane if f.id in targets]
                if not picked:
                    continue
                self.queues[plan.spec][bucket] = [f for f in lane if f.id not in targets]
                removed.extend(picked)
                self._sync_head_enter_time(self.queues[plan.spec][bucket], tick)
        else:
            for bucket in BUCKETS:
                need = plan.parts[bucket]
                if not need:
                    continue
                ranges = lane_demand_weight_ranges(self, plan.spec, bucket)
                from_storage = self.pick_storage_matching(
                    plan.spec, bucket, ranges, need
                )
                removed.extend(from_storage)
                need -= len(from_storage)
                if need:
                    chunk = self.queues[plan.spec][bucket][:need]
                    del self.queues[plan.spec][bucket][:need]
                    removed.extend(chunk)
                    self._sync_head_enter_time(self.queues[plan.spec][bucket], tick)
        for fish in removed:
            tracker.mark_packed(fish, tick)
        plan.fish = removed
        return removed

    def divert_head_to_storage(
        self,
        spec: str,
        bucket: str,
        tick: int,
        tracker: FishTracker,
    ) -> tuple[Fish | None, str]:
        """
        弹出料道队头鱼尝试送入暂存箱（_make_room_for_intake 腾位调用）。

        返回 (fish, outcome):
            stored       成功入暂存箱
            storage_full 暂存箱满，鱼记 unmatched_storage_full
            ""           料道为空
        """
        lane = self.queues[spec][bucket]
        if not lane:
            return None, ""
        fish = lane.pop(0)
        self._sync_head_enter_time(lane, tick)
        if self.try_push_storage(fish, tick, tracker):
            return fish, "stored"
        tracker.mark_unmatched(fish, "unmatched_storage_full", tick=tick)
        return fish, "storage_full"

    def divert_head(self, spec: str, bucket: str, tick: int, reason: str, tracker: FishTracker) -> Fish | None:
        """作用：弹出料道队头鱼送入回流队列（防堵：超容）。
        前端：两页回流统计；fifo_monitor 运行日志「回流」；GET /api/state 回流日志。"""
        lane = self.queues[spec][bucket]
        if not lane:
            return None
        fish = lane.pop(0)
        self._sync_head_enter_time(lane, tick)
        fish.rounds += 1
        fish.enter_time = tick
        self.reflow.append(fish)
        tracker.mark_reflow(fish, tick, reason)
        return fish

    def discard_head_timeout(
        self,
        spec: str,
        bucket: str,
        tick: int,
        lane_wait_s: int,
        tracker: FishTracker,
    ) -> Fish | None:
        """
        弹出料道队头超时鱼并记尾料（process_one → _anti_block 调用）。

        不进回流队列；tracker.mark_timeout_tail 写入 unmatched_timeout。
        队头变更后 _sync_head_enter_time 重置新队首 enter_time。
        """
        lane = self.queues[spec][bucket]
        if not lane:
            return None
        fish = lane.pop(0)
        self._sync_head_enter_time(lane, tick)
        tracker.mark_timeout_tail(fish, tick, lane_wait_s)
        return fish

    def iter_lanes(self):
        """作用：迭代所有启用规格的 (spec, bucket, lane) 三元组。
        前端：无直接 UI；批末扫尾 mark_unmatched 时遍历料道。"""
        for spec in self.specs:
            for bucket in BUCKETS:
                yield spec, bucket, self.queues[spec][bucket]


class BoxPlanner:
    """作用：DFS 自由组合封箱（plan/深度搜索.py）；三合一料道容量下不按队头顺序取鱼。
    料道较小时 DFS 全局择优；积压时搜各路队头窗口，无解则等待下一 tick。
    前端：index.html「完成箱数」；fifo_monitor.html 装箱工位。"""

    @staticmethod
    def _dfs_search_buffer(lanes: SortingLanes, spec: str) -> list[Fish]:
        """取 DFS 搜索窗口：暂存箱鱼优先，再取料道（小/中/大）。"""
        buf = lanes.storage_for_spec(spec)
        q = lanes.queues[spec]
        lane_total = lanes.total_in_spec(spec)
        max_buf = dfs_max_buffer_for_spec(spec)
        if lane_total + len(buf) <= max_buf:
            for bucket in BUCKETS:
                buf.extend(q[bucket])
            return buf
        window = dfs_window_per_bucket_for_spec(spec)
        for bucket in BUCKETS:
            buf.extend(q[bucket][:window])
        return buf

    @staticmethod
    def _plan_from_indices(buffer: list[Fish], spec: str, indices: list[int], count: int, weight: int) -> BoxPlan:
        picked = [buffer[i] for i in indices]
        parts = {b: 0 for b in BUCKETS}
        for fish in picked:
            parts[fish.bucket] += 1
        return BoxPlan(
            spec=spec,
            count=count,
            weight=weight,
            parts=parts,
            pick_ids=frozenset(f.id for f in picked),
        )

    def find_plan(self, lanes: SortingLanes, spec: str) -> BoxPlan | None:
        """
        DFS 搜索可封箱方案（process_one → _try_pack_spec 循环调用）。

        步骤:
            1. _dfs_search_buffer 取暂存+料道搜索窗口
            2. dfs_find_best_from_items 在窗口内自由组合
            3. 总重 4980~5030g 且尾数合法则 _plan_from_indices 生成 BoxPlan

        无解返回 None，本 tick 该规格不封箱。
        """
        if spec not in SPECS:
            return None
        if lanes.total_available_for_spec(spec) < min(SPECS[spec]["counts"]):
            return None

        buffer = self._dfs_search_buffer(lanes, spec)
        max_buf = dfs_max_buffer_for_spec(spec)
        result = dfs_find_best_from_items(
            buffer,
            spec,
            max_buffer=max_buf,
            max_nodes=max(DFS_MAX_NODES, max_buf * 8000),
        )
        if result:
            indices, count, weight = result
            return self._plan_from_indices(buffer, spec, indices, count, weight)

        return None


class SchedulerEngine:
    """智能分拣仿真主引擎：入料 → 料道/暂存 → 封箱 → 防堵 → 批末导出。"""

    def __init__(
        self,
        batch_records: list | None = None,
        seed: int = DEFAULT_SEED,
        interval: float = 1.0,
        specs: tuple[str, ...] = DEMO_SPECS,
        move_timeout: int = DEFAULT_MOVE_TIMEOUT,
        cap_factor: int = DEFAULT_CAP_FACTOR,
        verbose: bool = False,
        log_every: int = 500,
        stop_mode: str = STOP_MODE_COUNT,
        stop_count: int = DEFAULT_TOTAL,
        stop_weight_g: int = DEFAULT_STOP_WEIGHT_G,
        timeout_clock: str = DEFAULT_TIMEOUT_CLOCK,
        exclude_outside_stats: bool = False,
    ):
        """初始化引擎：批次、料道、追踪器、统计。
        exclude_outside_stats：批量测试用，规格外不计入料/结束条件。"""
        # --- 运行配置 ---
        self.seed = seed                              # 随机种子，决定批次鱼序列
        self.interval = interval                      # 入料间隔（秒），CLI/后台循环 sleep 用
        self.specs = specs                            # 启用的规格列表（如 15p/20p/…）
        self.move_timeout = move_timeout              # 队首鱼在料道/暂存等待超时阈值
        self.cap_factor = cap_factor                  # 三合一料道容量倍率：min(counts) + cap_factor
        self.verbose = verbose                        # 是否打印详细调试日志
        self.log_every = log_every                    # 每入料 N 条打印一次进度
        self.exclude_outside_stats = exclude_outside_stats  # 批量测试：规格外不计入料/结束条件
        self.stop_mode = stop_mode if stop_mode in (STOP_MODE_COUNT, STOP_MODE_WEIGHT) else STOP_MODE_COUNT  # 结束模式：count 按条数 / weight 按总重
        self.stop_count = max(1, stop_count)          # 按条数结束时的目标入料条数
        self.stop_weight_g = max(1, stop_weight_g)    # 按总重结束时的目标克重
        self.timeout_clock = (                        # 超时计时方式：intake 每步+1 / real 墙钟秒
            timeout_clock
            if timeout_clock in (TIMEOUT_CLOCK_INTAKE, TIMEOUT_CLOCK_REAL)
            else DEFAULT_TIMEOUT_CLOCK
        )

        # --- 批次与入料游标 ---
        self.batch = batch_records or load_or_generate_batch(seed=seed)  # 待入料的原始批次记录
        self.total_fish = len(self.batch)             # 本批实际处理条数（按条数模式会截断）
        if self.stop_mode == STOP_MODE_COUNT:
            self.total_fish = min(self.total_fish, self.stop_count)
            self.batch = self.batch[: self.total_fish]
        self._cursor = 0                              # 批次读取游标，process_one 逐条推进

        # --- 核心子模块 ---
        self.lanes = SortingLanes(specs=specs)        # 料道/回流/规格外/暂存箱状态
        self.planner = BoxPlanner()                   # DFS 配盒规划器
        self.tracker = FishTracker()                  # 单鱼生命周期追踪（入队/装箱/回流/尾料）
        self.stats = Stats()                          # 累计统计（入料、成盒、回流、暂存等）
        self.cartons: list[BoxPlan] = []               # 已成盒记录列表

        # --- 仿真时钟 ---
        self._time_origin = time.monotonic()          # 墙钟模式起始时刻
        self.tick = 0                                 # 当前仿真 tick（步数或秒）
        self.finished = False                         # 批次是否已结束

        # --- 日志缓冲 ---
        self.timeout_tail_log: list[dict] = []         # 超时尾料明细（导出 CSV）
        self._timeout_warn_ratio = 0.8                # 等待达阈值 80% 时打预警

    def _tick_unit(self) -> str:
        return "s" if self.timeout_clock == TIMEOUT_CLOCK_REAL else "步"

    def _tick_prefix(self) -> str:
        return f"t={self.tick:05d}{self._tick_unit()}"

    def _log(self, tag: str, msg: str, *, force: bool = False) -> None:
        """verbose 或 force 时打印带分类标签的日志。"""
        if self.verbose or force:
            print(f"[{self._tick_prefix()}][{tag}] {msg}")

    def _log_flow(self, msg: str) -> None:
        """verbose 时打印单条鱼/物料流向。"""
        if self.verbose:
            self._log("流向", msg)

    def _storage_status(self) -> str:
        cur = self.lanes.storage_count()
        cap = self.lanes.storage_capacity
        peak = self.stats.storage_max
        cur_pct = round(cur / cap * 100, 1) if cap else 0.0
        peak_pct = round(peak / cap * 100, 1) if cap else 0.0
        return f"暂存 {cur}/{cap}({cur_pct}%) 峰值 {peak}/{cap}({peak_pct}%)"

    def _timeout_status(self) -> str:
        lane_to = self.stats.timeout_tail
        stor_to = self.stats.storage_timeout_tail
        return f"超时 料道{lane_to}+暂存{stor_to}={lane_to + stor_to}"

    def _lane_status(self, spec: str) -> str:
        total = self.lanes.total_in_spec(spec)
        cap = spec_total_capacity(spec, self.cap_factor)
        q = self.lanes.queues[spec]
        parts = "/".join(f"{BUCKET_LABEL[b]}{len(q[b])}" for b in BUCKETS)
        return f"料道 {spec.upper()} {total}/{cap} ({parts})"

    def _note_storage_peak(self) -> None:
        """更新暂存箱历史峰值，创新高时强制打印。"""
        count = self.lanes.storage_count()
        prev_peak = self.stats.storage_max
        if count > self.stats.storage_max:
            self.stats.storage_max = count
            cap = self.lanes.storage_capacity
            pct = round(count / cap * 100, 1) if cap else 0.0
            delta = count - prev_peak
            self._log(
                "暂存峰值",
                f"↑ 新峰值 {count}/{cap} ({pct}%)，+{delta} | {self._timeout_status()}",
                force=True,
            )

    def _warn_timeout_pressure(self) -> None:
        """verbose 时扫描料道队首与暂存最久鱼，接近阈值则预警。"""
        if self.move_timeout <= 0 or not self.verbose:
            return
        warn_at = max(1, int(self.move_timeout * self._timeout_warn_ratio))
        unit = self._tick_unit()
        if self.lanes.storage:
            fish = min(self.lanes.storage, key=lambda f: f.enter_time)
            dwell = self.tick - fish.enter_time
            if warn_at <= dwell < self.move_timeout:
                self._log(
                    "超时预警",
                    f"暂存 #{fish.id} {fish.spec.upper()}-{BUCKET_LABEL[fish.bucket]} "
                    f"{fish.weight}g 已等 {dwell}{unit}/{self.move_timeout}{unit} "
                    f"({dwell * 100 // self.move_timeout}%) | {self._storage_status()}",
                )
        for spec in self.specs:
            for bucket in BUCKETS:
                lane = self.lanes.queues[spec][bucket]
                if not lane:
                    continue
                head = lane[0]
                dwell = self.tick - head.enter_time
                if warn_at <= dwell < self.move_timeout:
                    self._log(
                        "超时预警",
                        f"料道 #{head.id} {spec.upper()}-{BUCKET_LABEL[bucket]} "
                        f"{head.weight}g 队首已等 {dwell}{unit}/{self.move_timeout}{unit} "
                        f"({dwell * 100 // self.move_timeout}%)",
                    )

    def _sync_real_tick(self) -> int:
        """真实系统时间（秒）更新 tick。"""
        self.tick = int(max(0, time.monotonic() - self._time_origin))
        return self.tick

    def _advance_tick(self, steps: int = 1) -> int:
        """
        推进仿真时钟 tick（用于超时判定与事件时间戳）。

        intake 模式: 每 process_one 一步 tick += 1；
        real 模式: tick = 墙钟秒（_sync_real_tick）。
        """
        if self.timeout_clock == TIMEOUT_CLOCK_REAL:
            return self._sync_real_tick()
        self.tick += max(1, steps)
        return self.tick

    def _best_diagnostic_for_spec(self, spec: str) -> dict:
        """诊断某规格封箱状态（可装/不足/偏轻/偏重/接近）。"""
        if spec not in self.specs:
            return {"kind": "off", "short": "未启用", "need_bucket": None, "need_count": 0}
        q = self.lanes.queues[spec]
        q_small, q_medium, q_large = q["small"], q["medium"], q["large"]
        total = len(q_small) + len(q_medium) + len(q_large)
        min_cnt = min(SPECS[spec]["counts"])
        if total < min_cnt:
            return {
                "kind": "bad",
                "short": "不足",
                "need_bucket": None,
                "need_count": min_cnt - total,
            }
        if self.planner.find_plan(self.lanes, spec):
            return {"kind": "good", "short": "可装", "need_bucket": None, "need_count": 0}
        p_small = prefix_weights(q_small)
        p_medium = prefix_weights(q_medium)
        p_large = prefix_weights(q_large)
        best: dict | None = None
        for count in SPECS[spec]["counts"]:
            for a in range(min(len(q_small), count) + 1):
                for b in range(min(len(q_medium), count - a) + 1):
                    c = count - a - b
                    if c > len(q_large):
                        continue
                    weight = p_small[a] + p_medium[b] + p_large[c]
                    if weight < TARGET_MIN:
                        diff = TARGET_MIN - weight
                    elif weight > TARGET_MAX:
                        diff = weight - TARGET_MAX
                    else:
                        diff = 0
                    score = diff * 10 + abs(weight - TARGET_MID)
                    if best is None or score < best["score"]:
                        best = {"count": count, "weight": weight, "diff": diff, "score": score}
        if not best:
            return {"kind": "bad", "short": "等待", "need_bucket": "medium", "need_count": 1}
        if best["weight"] < TARGET_MIN:
            return {"kind": "warn", "short": "偏轻", "need_bucket": "large", "need_count": 1}
        if best["weight"] > TARGET_MAX:
            return {"kind": "warn", "short": "偏重", "need_bucket": "small", "need_count": 1}
        return {"kind": "good", "short": "接近", "need_bucket": None, "need_count": 0}

    def _demand_for_spec(self, spec: str) -> dict:
        """计算单规格的暂存出库需求（active/priority/count/weight_ranges）。"""
        lo, hi = SPECS[spec]["range"]
        if spec not in self.specs:
            return {
                "spec": spec,
                "active": False,
                "priority": 9,
                "count": 0,
                "weight_ranges": [(lo, hi)],
                "reason": "未启用",
            }
        weight_ranges = spec_demand_weight_ranges(self.lanes, spec)
        diag = self._best_diagnostic_for_spec(spec)
        short = diag["short"]

        if short == "可装":
            return {
                "spec": spec,
                "active": False,
                "priority": 4,
                "count": 0,
                "weight_ranges": weight_ranges,
                "reason": "可装",
            }
        if short == "不足":
            return {
                "spec": spec,
                "active": True,
                "priority": 2,
                "count": diag["need_count"] or 1,
                "weight_ranges": weight_ranges,
                "reason": "不足",
            }
        if short in ("偏轻", "偏重", "等待"):
            return {
                "spec": spec,
                "active": True,
                "priority": 3,
                "count": diag.get("need_count") or 1,
                "weight_ranges": weight_ranges,
                "reason": short,
            }
        total = self.lanes.total_in_spec(spec)
        if total > 0:
            return {
                "spec": spec,
                "active": short != "接近",
                "priority": 4,
                "count": 1,
                "weight_ranges": weight_ranges,
                "reason": short or f"料道{total}条",
            }
        return {
            "spec": spec,
            "active": False,
            "priority": 5,
            "count": 0,
            "weight_ranges": weight_ranges,
            "reason": "监控",
        }

    def _active_storage_demands(self) -> list[dict]:
        """返回 active 且 priority<=3 的需求，供暂存箱出库使用。"""
        items = [
            self._demand_for_spec(spec)
            for spec_list in MODULE_SPECS.values()
            for spec in spec_list
        ]
        items.sort(key=lambda d: (d["priority"], d["spec"]))
        return [d for d in items if d.get("active") and d.get("priority", 9) <= 3]

    def _apply_box_plan(self, plan: BoxPlan) -> None:
        """
        执行一次封箱方案。

        变量:
            plan: DFS 搜索得到的 BoxPlan（含规格、尾数、总重、pick_ids）
            storage_before: 封箱前暂存箱内鱼 ID 集合，用于统计 storage_packed
        """
        storage_before = {f.id for f in self.lanes.storage}
        self.lanes.remove_plan(plan, self.tick, self.tracker)
        self.stats.storage_packed += sum(
            1 for f in plan.fish if f.id in storage_before
        )
        self.stats.cartons += 1
        self.stats.packed_fish += plan.count
        self.cartons.append(plan)
        parts = " + ".join(
            f"{BUCKET_LABEL[b]}{plan.parts[b]}" for b in BUCKETS if plan.parts[b]
        )
        fish_ids = ",".join(f"#{f.id}" for f in plan.fish)
        from_storage = sum(1 for f in plan.fish if f.id in storage_before)
        src = f"含暂存{from_storage}条" if from_storage else "料道直取"
        self._log(
            "封箱",
            f"盒#{self.stats.cartons:04d} {plan.spec.upper()} {plan.count}尾 "
            f"{plan.weight}g ({parts}) | 鱼[{fish_ids}] {src}",
        )

    def _try_pack_spec(self, spec: str) -> int:
        """
        对单个规格循环尝试 DFS 封箱，直至 planner.find_plan 返回 None。

        返回:
            packed: 本步该规格成功封箱次数
        """
        packed = 0
        while True:
            plan = self.planner.find_plan(self.lanes, spec)
            if plan is None:
                break
            self._apply_box_plan(plan)
            packed += 1
        return packed

    def _try_pack_all(self) -> int:
        """
        遍历所有启用规格，逐个调用 _try_pack_spec 尝试封箱。

        在 process_one 中于入料前/入料后各调用一次，尽量在每条新鱼进道前后腾出空位。

        返回:
            packed: 本步全部规格合计封箱次数
        """
        packed = 0
        for spec in self.specs:
            packed += self._try_pack_spec(spec)
        return packed

    def _intake_weight_ranges(self, spec: str) -> list[tuple[int, int]]:
        """
        计算入料路由时「最需要进道」的克重区间。

        逻辑:
            料道鱼数 < min(counts) → 放宽为整规格区间；
            否则用 BoxDemandCalculator 动态需求；
            仍无解时用 diagnostic_need_weight_ranges 偏轻/偏重诊断收窄。
        """
        lo, hi = SPECS[spec]["range"]
        lane_weights = lane_inventory_weights(self.lanes, spec)
        min_cnt = min(SPECS[spec]["counts"])
        if len(lane_weights) < min_cnt:
            return [(lo, hi)]
        demand = BoxDemandCalculator(spec, lane_weights).calc()
        if demand.next_fish_ranges:
            return list(demand.next_fish_ranges)
        narrowed = diagnostic_need_weight_ranges(self.lanes, spec)
        if narrowed:
            return narrowed
        return [(lo, hi)]

    def _intake_matches_demand(self, fish: Fish) -> bool:
        """
        判断 incoming 鱼的克重是否落在当前料道动态需求区间内。

        用于 _route_spec_intake 决定直接入道还是进暂存箱。
        """
        if fish.spec is None:
            return False
        return weight_in_ranges(fish.weight, self._intake_weight_ranges(fish.spec))

    def _push_intake_storage(self, fish: Fish, reason: str) -> bool:
        """
        将鱼送入暂存箱；箱满则直接记为尾料（unmatched_storage_full）。

        参数:
            fish: 待入箱的鱼
            reason: 入箱原因（"需求不匹配" / "料道已满"），记入事件日志

        返回:
            True 入箱成功；False 箱满记尾料
        """
        if self.lanes.try_push_storage(fish, self.tick, self.tracker):
            self.stats.storage_in += 1
            self._log_flow(
                f"#{fish.id} {fish.weight}g {fish.spec.upper()}-{BUCKET_LABEL[fish.bucket]} "
                f"→ 暂存箱 ({reason}) | {self._storage_status()}"
            )
            return True
        self.tracker.mark_unmatched(fish, "unmatched_storage_full", tick=self.tick)
        self.stats.storage_full_tail += 1
        self.stats.tail_count += 1
        self._log(
            "暂存满",
            f"#{fish.id} {fish.spec.upper()}-{BUCKET_LABEL[fish.bucket]} "
            f"{fish.weight}g → 尾料(箱满) | {self._storage_status()}",
            force=True,
        )
        return False

    def _make_room_for_intake(self, fish: Fish) -> bool:
        """
        料道三合一已满但 incoming 鱼匹配需求时，腾出一个料道位。

        策略:
            1. 先尝试 _try_pack_spec 封箱减压；
            2. 仍满则按诊断结果选 evict_bucket（偏重弹大分区队头，偏轻弹小分区）；
            3. 将队头鱼 divert_head_to_storage 换入暂存箱。

        变量:
            cap: 该规格三合一料道总容量
            diag/short: 封箱诊断（偏重/偏轻/可装等）
            evict_bucket: 被弹出队头的分区
            moved/outcome: 弹出结果（stored=入暂存，storage_full=箱满尾料）

        返回:
            True 腾位后料道有空位；False 无法腾位
        """
        spec = fish.spec
        if spec is None:
            return False
        cap = spec_total_capacity(spec, self.cap_factor)
        self._try_pack_spec(spec)
        if self.lanes.total_in_spec(spec) < cap:
            return True

        diag = self._best_diagnostic_for_spec(spec)
        short = diag.get("short", "")
        if short == "偏重" or fish.bucket == "small":
            evict_bucket = "large"
        elif short == "偏轻" or fish.bucket == "large":
            evict_bucket = "small"
        else:
            evict_bucket = max(
                BUCKETS,
                key=lambda b: len(self.lanes.queues[spec][b]),
            )
        lane = self.lanes.queues[spec][evict_bucket]
        if not lane:
            for bucket in BUCKETS:
                if self.lanes.queues[spec][bucket]:
                    evict_bucket = bucket
                    lane = self.lanes.queues[spec][bucket]
                    break
        if not lane:
            return False

        moved, outcome = self.lanes.divert_head_to_storage(
            spec, evict_bucket, self.tick, self.tracker
        )
        if moved and outcome == "stored":
            self.stats.storage_in += 1
            self._log_flow(
                f"#{moved.id} {moved.weight}g {spec.upper()}-{BUCKET_LABEL[evict_bucket]} "
                f"料道队首 → 暂存箱(为#{fish.id}让路) | {self._storage_status()}"
            )
            return self.lanes.total_in_spec(spec) < cap
        return False

    def _route_spec_intake(self, fish: Fish) -> str:
        """
        规格内鱼的入料路由：料道 or 暂存箱。

        变量:
            cap: 三合一料道总容量 = min(counts) + cap_factor
            min_cnt: 该规格最小合法装箱尾数
            total: 当前料道已有鱼数
            matches: incoming 是否匹配动态需求区间

        决策树:
            total < min_cnt 或 (未满且 matches)     → enqueue 入料道，返回 bucket 名
            未满但不 matches                        → 暂存箱（需求不匹配）
            已满且 matches 且 _make_room 成功       → enqueue 入料道
            其余                                    → 暂存箱（料道已满）

        返回:
            bucket 名（small/medium/large）或 "storage"
        """
        spec = fish.spec
        if spec is None:
            return "outside"
        cap = spec_total_capacity(spec, self.cap_factor)
        min_cnt = min(SPECS[spec]["counts"])
        total = self.lanes.total_in_spec(spec)
        matches = self._intake_matches_demand(fish)

        if total < min_cnt or (total < cap and matches):
            bucket = self.lanes.enqueue(fish, self.tick, self.tracker)
            return bucket

        if total < cap and not matches:
            self._push_intake_storage(fish, "需求不匹配")
            return "storage"

        if matches and self._make_room_for_intake(fish):
            bucket = self.lanes.enqueue(fish, self.tick, self.tracker)
            return bucket

        self._push_intake_storage(fish, "料道已满")
        return "storage"

    def _process_reflow_intake(self) -> None:
        """
        处理回流队列：尝试将 reflow 中的鱼重新放入料道。

        变量:
            remaining: 本步仍无法入道的回流鱼，写回 lanes.reflow

        注：当前防堵逻辑已改为暂存箱路由，回流队列通常为空；
            try_enqueue_reflow 在料道未满时调用 enqueue 重新入道。
        """
        remaining: list[Fish] = []
        for fish in self.lanes.reflow:
            if not self.lanes.try_enqueue_reflow(
                fish, self.tick, self.tracker, self.cap_factor
            ):
                remaining.append(fish)
        self.lanes.reflow = remaining

    def _release_storage_by_demands(self) -> int:
        """
        按规格广播需求从暂存箱出库到料道。

        遍历 _active_storage_demands()，按 weight_ranges 匹配暂存鱼出库入道。

        变量:
            released: 本步从暂存箱释放到料道的鱼条数

        返回:
            released 条数
        """
        if not self.lanes.storage:
            return 0
        released = 0
        for demand in self._active_storage_demands():
            spec = demand["spec"]
            if spec not in self.specs:
                continue
            count = demand.get("count") or 0
            if count <= 0:
                continue
            ranges = demand["weight_ranges"]
            moved = self.lanes.transfer_storage_for_spec(
                spec,
                ranges,
                count,
                self.tick,
                self.tracker,
                self.cap_factor,
            )
            if moved:
                released += len(moved)
                self.stats.storage_to_lane += len(moved)
                ids = ",".join(f"#{f.id}" for f in moved)
                self._log_flow(
                    f"[{ids}] 暂存箱 → {spec.upper()}料道 "
                    f"区间{ranges} ({demand.get('reason', '')}) | "
                    f"{self._lane_status(spec)} | {self._storage_status()}"
                )
        return released

    def _monitor_storage(self) -> None:
        """
        暂存箱超时监控：等待最久的鱼超过 move_timeout 则记为尾料。

        变量:
            fish: enter_time 最早（等待最久）的暂存鱼
            dwell: 在暂存箱内的等待 tick 数
            trace/first_in: 鱼的全局追踪记录，用于 system_dwell_s

        每步最多处理 1 条，避免单 tick 批量淘汰。
        """
        if self.move_timeout <= 0 or not self.lanes.storage:
            return
        fish = min(self.lanes.storage, key=lambda f: f.enter_time)
        if self.tick - fish.enter_time < self.move_timeout:
            return
        self.lanes.remove_from_storage_ids({fish.id})
        dwell = self.tick - fish.enter_time
        trace = self.tracker.traces.get(fish.id)
        first_in = trace.first_in_time if trace else None
        self.tracker.mark_timeout_tail(
            fish, self.tick, dwell, status="unmatched_storage_timeout"
        )
        self.stats.storage_timeout_tail += 1
        self.stats.tail_count += 1
        self.timeout_tail_log.append(
            {
                "tick": self.tick,
                "fish_id": fish.id,
                "weight": fish.weight,
                "spec": fish.spec,
                "bucket": fish.bucket,
                "first_in_time": first_in,
                "lane_wait_s": dwell,
                "system_dwell_s": (
                    self.tick - first_in if first_in is not None else None
                ),
                "threshold_s": self.move_timeout,
                "rounds": fish.rounds,
                "batch_seed": self.seed,
                "source": "storage",
            }
        )
        unit = self._tick_unit()
        system_dwell = self.tick - first_in if first_in is not None else None
        self._log(
            "超时",
            f"暂存 #{fish.id} {fish.spec.upper()}-{BUCKET_LABEL[fish.bucket]} "
            f"{fish.weight}g 等待 {dwell}{unit} ≥ 阈值{self.move_timeout}{unit} → 尾料"
            + (
                f" | 系统停留 {system_dwell}{unit}"
                if system_dwell is not None
                else ""
            )
            + f" | {self._storage_status()}",
            force=True,
        )

    def _anti_block(self) -> None:
        """
        料道防堵：队首鱼等待超过 move_timeout 则弹出并记为超时尾料。

        变量:
            lane: 当前检查的 (spec, bucket) 料道队列
            dwell: 队首鱼担任队首的等待 tick 数（enter_time 在队首变更时重置）
            fish: 被弹出的超时鱼

        每步只处理第一条超时队首（return 退出），避免同 tick 大量淘汰。
        注：超容不再弹队头到回流队列，改由入料侧 _route_spec_intake 路由暂存箱。
        """
        for spec in self.specs:
            for bucket in BUCKETS:
                lane = self.lanes.queues[spec][bucket]
                if not lane:
                    continue
                if (
                    self.move_timeout > 0
                    and self.tick - lane[0].enter_time >= self.move_timeout
                ):
                    dwell = self.tick - lane[0].enter_time
                    trace = self.tracker.traces.get(lane[0].id)
                    first_in = trace.first_in_time if trace else None
                    fish = self.lanes.discard_head_timeout(
                        spec, bucket, self.tick, dwell, self.tracker
                    )
                    if fish:
                        self.stats.timeout_tail += 1
                        self.stats.tail_count += 1
                        self.timeout_tail_log.append(
                            {
                                "tick": self.tick,
                                "fish_id": fish.id,
                                "weight": fish.weight,
                                "spec": spec,
                                "bucket": bucket,
                                "first_in_time": first_in,
                                "lane_wait_s": dwell,
                                "system_dwell_s": (
                                    self.tick - first_in if first_in is not None else None
                                ),
                                "threshold_s": self.move_timeout,
                                "rounds": fish.rounds,
                                "batch_seed": self.seed,
                            }
                        )
                        unit = self._tick_unit()
                        system_dwell = (
                            self.tick - first_in if first_in is not None else None
                        )
                        self._log(
                            "超时",
                            f"料道 #{fish.id} {spec.upper()}-{BUCKET_LABEL[bucket]} "
                            f"{fish.weight}g 队首等待 {dwell}{unit} ≥ "
                            f"阈值{self.move_timeout}{unit} → 尾料 R{fish.rounds}"
                            + (
                                f" | 系统停留 {system_dwell}{unit}"
                                if system_dwell is not None
                                else ""
                            )
                            + f" | {self._timeout_status()}",
                            force=True,
                        )
                    return

    def _save_cartons_csv(self, path: Path) -> None:
        """导出成盒明细 CSV（cartons_seed_{seed}.csv）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "carton_seq",
                    "spec",
                    "count",
                    "weight",
                    "small",
                    "medium",
                    "large",
                    "fish_ids",
                    "fish_weights",
                    "fish_buckets",
                ],
            )
            writer.writeheader()
            for idx, plan in enumerate(self.cartons, start=1):
                writer.writerow(
                    {
                        "carton_seq": idx,
                        "spec": plan.spec,
                        "count": plan.count,
                        "weight": plan.weight,
                        "small": plan.parts.get("small", 0),
                        "medium": plan.parts.get("medium", 0),
                        "large": plan.parts.get("large", 0),
                        "fish_ids": "|".join(str(f.id) for f in plan.fish),
                        "fish_weights": "|".join(str(f.weight) for f in plan.fish),
                        "fish_buckets": "|".join(f.bucket or "" for f in plan.fish),
                    }
                )

    def _save_remaining_csv(self, path: Path) -> None:
        """作用：导出未成盒尾料 CSV（remaining_seed_{seed}.csv）。
        前端：index.html「尾料数据」导出；GET /api/remaining.csv。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "fish_id",
                    "weight",
                    "spec",
                    "bucket",
                    "rounds",
                    "status",
                    "tail_cause",
                    "reflow_summary",
                    "had_timeout",
                    "had_overflow",
                    "first_in_time",
                    "outbound_time",
                    "lane_wait_s",
                    "dwell_time",
                    "batch_seed",
                    "reflow_reasons",
                ],
            )
            writer.writeheader()
            for row in self.tracker.remaining_records(
                end_tick=self.tick,
                batch_seed=self.seed,
            ):
                writer.writerow(
                    {
                        **row,
                        "had_timeout": int(row["had_timeout"]),
                        "had_overflow": int(row["had_overflow"]),
                        "first_in_time": row["first_in_time"] if row["first_in_time"] is not None else "",
                        "outbound_time": row["outbound_time"] if row["outbound_time"] is not None else "",
                        "lane_wait_s": row["lane_wait_s"] if row["lane_wait_s"] is not None else "",
                        "dwell_time": row["dwell_time"] if row["dwell_time"] is not None else "",
                        "batch_seed": row["batch_seed"] if row["batch_seed"] is not None else "",
                        "reflow_reasons": "|".join(row["reflow_reasons"]),
                    }
                )

    def _save_timeout_tail_csv(self, path: Path) -> None:
        """作用：导出超时尾料明细 CSV（timeout_tail_seed_{seed}.csv），便于分析优化。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "fish_id",
                    "weight",
                    "spec",
                    "bucket",
                    "batch_seed",
                    "first_in_time",
                    "outbound_tick",
                    "lane_wait_s",
                    "system_dwell_s",
                    "threshold_s",
                    "rounds",
                ],
            )
            writer.writeheader()
            for row in self.timeout_tail_log:
                writer.writerow(
                    {
                        "fish_id": row["fish_id"],
                        "weight": row["weight"],
                        "spec": row["spec"],
                        "bucket": row["bucket"],
                        "batch_seed": row["batch_seed"],
                        "first_in_time": row["first_in_time"] if row["first_in_time"] is not None else "",
                        "outbound_tick": row["tick"],
                        "lane_wait_s": row["lane_wait_s"],
                        "system_dwell_s": row["system_dwell_s"] if row["system_dwell_s"] is not None else "",
                        "threshold_s": row["threshold_s"],
                        "rounds": row["rounds"],
                    }
                )

    def _intake_complete(self) -> bool:
        """
        判断是否已达入料结束条件。

        按总重模式：累计入料克重 >= stop_weight_g；
        按条数模式（exclude_outside_stats）：规格内入料条数 >= stop_count；
        按条数模式（默认）：批次游标 >= stop_count。
        """
        if self.stop_mode == STOP_MODE_WEIGHT:
            return self.stats.input_weight >= self.stop_weight_g
        if self.exclude_outside_stats:
            return self.stats.input_count >= self.stop_count
        return self._cursor >= self.stop_count

    def _timeout_fish_total(self) -> int:
        """超时尾料合计（料道 timeout_tail + 暂存箱 storage_timeout_tail）。"""
        return self.stats.timeout_tail + self.stats.storage_timeout_tail

    def build_final_metrics(self) -> dict[str, Any]:
        """
        批末核心指标（供 print_report / 外部汇总使用）。

        包含：配箱、暂存箱峰值、超时、回流/箱满、批末未配盒。
        """
        unmatched = list(self.tracker.unmatched)
        cap = self.lanes.storage_capacity
        peak = self.stats.storage_max

        lane_to_n, lane_to_w = sum_unmatched_traces(unmatched, ("unmatched_timeout",))
        stor_to_n, stor_to_w = sum_unmatched_traces(
            unmatched, ("unmatched_storage_timeout",)
        )
        timeout_n = lane_to_n + stor_to_n
        timeout_w = lane_to_w + stor_to_w

        tail_lane_n, tail_lane_w = sum_unmatched_traces(unmatched, ("unmatched_tail",))
        tail_stor_n, tail_stor_w = sum_unmatched_traces(
            unmatched, ("unmatched_storage",)
        )
        tail_refl_n, tail_refl_w = sum_unmatched_traces(unmatched, ("unmatched_reflow",))
        tail_batch_n = tail_lane_n + tail_stor_n + tail_refl_n
        tail_batch_w = tail_lane_w + tail_stor_w + tail_refl_w

        full_n, full_w = sum_unmatched_traces(unmatched, _STORAGE_FULL_STATUSES)

        return {
            "cartons": self.stats.cartons,
            "packed_fish": self.stats.packed_fish,
            "storage_peak": peak,
            "storage_capacity": cap,
            "storage_peak_pct": round(peak / cap * 100, 1) if cap else 0.0,
            "timeout_lane_count": lane_to_n,
            "timeout_lane_weight_g": lane_to_w,
            "timeout_storage_count": stor_to_n,
            "timeout_storage_weight_g": stor_to_w,
            "timeout_total_count": timeout_n,
            "timeout_total_weight_g": timeout_w,
            "reflow_batch_count": tail_refl_n,
            "reflow_batch_weight_g": tail_refl_w,
            "storage_full_count": full_n,
            "storage_full_weight_g": full_w,
            "tail_lane_count": tail_lane_n,
            "tail_lane_weight_g": tail_lane_w,
            "tail_storage_count": tail_stor_n,
            "tail_storage_weight_g": tail_stor_w,
            "tail_reflow_count": tail_refl_n,
            "tail_reflow_weight_g": tail_refl_w,
            "tail_batch_count": tail_batch_n,
            "tail_batch_weight_g": tail_batch_w,
        }

    def process_one(self) -> bool:
        """
        推进仿真一步（处理批次中下一条鱼）。

        单步流水线（入料前 → 入料 → 入料后）::

            [前置] 回流再入道 → 暂存按需求出库 → 封箱 → 记暂存峰值
            [入料] 读批次记录 → 统计入料 → 转 Fish → 规格外入 outside / 规格内 _route_spec_intake
            [后置] 暂存出库 → 封箱 → 暂存超时 → 记峰值 → 料道超时防堵 → 采样历史

        实例变量（本方法读写）:
            _cursor      批次读取游标，指向下一条待处理 FishRecord 下标
            total_fish   本批预加载鱼总数（按条数模式可能被截断）
            tick         仿真时钟，超时判定与 enter_time 基准
            batch        预加载的 FishRecord 列表
            stats        累计统计（入料/成盒/超时/暂存等）
            lanes        料道 + 暂存箱 + 回流队列
            tracker      单鱼生命周期追踪

        局部变量:
            record       当前步从 batch[_cursor] 取出的原始记录
            is_outside   是否超规鱼（record.outside 或 spec 为空）
            fish         record_to_fish 转换后的运行时鱼对象
            dest         _route_spec_intake 返回值：small/medium/large 或 storage

        返回:
            True  本步已处理且未达结束条件，可继续下一步
            False 批次读完或已达 stop 条件，调用方应 stop 循环
        """
        # ── 1. 结束判定：批次游标越界或入料目标已达成 ──
        if self._cursor >= self.total_fish or self._intake_complete():
            return False

        # ── 2. 推进时钟，取下一条批次记录 ──
        self._advance_tick(1)
        record = self.batch[self._cursor]   # 当前原始批次记录
        self._cursor += 1                   # 游标前移，下次处理下一条
        is_outside = record.outside or record.spec is None  # 是否规格外/超规

        # ── 3. 累计入料统计（批量模式 exclude_outside_stats 时超规不计入）──
        if not (self.exclude_outside_stats and is_outside):
            self.stats.input_count += 1
            self.stats.input_weight += record.weight

        # ── 4. 转为运行时 Fish（含规格、小中大分区、enter_time）──
        fish = record_to_fish(record, self.tick, enabled=set(self.specs))

        # ── 5. 入料前处理：腾出空位、尝试封箱 ──
        self._process_reflow_intake()       # 回流队列 → 料道（通常为空）
        self._release_storage_by_demands()  # 暂存箱按需求出库 → 料道
        self._try_pack_all()                # 各规格 DFS 封箱
        self._note_storage_peak()           # 更新暂存箱历史峰值

        # ── 6. 入料路由 ──
        if is_outside or fish.spec is None:
            self.stats.outside_count += 1
            self.lanes.enqueue(fish, self.tick, self.tracker)
            self._log_flow(f"#{fish.id} {fish.weight}g → 规格外箱")
        else:
            dest = self._route_spec_intake(fish)
            self._note_storage_peak()
            if dest != "storage":
                self._log_flow(
                    f"#{fish.id} {fish.weight}g {fish.spec.upper()}-"
                    f"{BUCKET_LABEL[dest]} → 料道{BUCKET_LABEL[dest]}区 R{fish.rounds} | "
                    f"{self._lane_status(fish.spec)}"
                )

        # ── 7. 入料后处理：再封箱、暂存超时、料道防堵 ──
        self._release_storage_by_demands()
        self._try_pack_all()
        self._monitor_storage()
        self._note_storage_peak()
        self._anti_block()
        self._warn_timeout_pressure()

        # ── 8. 周期性进度日志 ──
        if self.stats.input_count % self.log_every == 0:
            if self.stop_mode == STOP_MODE_WEIGHT:
                progress = (
                    f"累计 {self.stats.input_weight / 1_000_000:.2f}t/"
                    f"{self.stop_weight_g / 1_000_000:.2f}t"
                )
            else:
                progress = f"入料 {self.stats.input_count}/{self.stop_count}"
            if self.exclude_outside_stats:
                tail_note = f"规格外(不计) {self.stats.outside_count}"
            else:
                tail_note = f"规格外 {self.stats.outside_count}"
            self._log(
                "进度",
                f"{progress} | 成盒 {self.stats.cartons} | 装箱 {self.stats.packed_fish} | "
                f"{self._storage_status()} | {self._timeout_status()} | {tail_note}",
                force=True,
            )
        # 若本步处理后已达结束条件，返回 False 通知调用方停止
        return not self._intake_complete()

    def finish_batch(self) -> None:
        """批末扫尾 → 标记尾料 → 写 CSV 报告。"""
        for _ in range(5000):
            self._advance_tick(1)
            before = self.stats.cartons
            self._process_reflow_intake()
            self._release_storage_by_demands()
            self._try_pack_all()
            self._monitor_storage()
            self._note_storage_peak()
            if self.stats.cartons == before and not self.lanes.reflow and not self.lanes.storage:
                break
            self._anti_block()

        for spec in self.specs:
            for bucket in BUCKETS:
                for fish in self.lanes.queues[spec][bucket]:
                    self.tracker.mark_unmatched(fish, "unmatched_tail", tick=self.tick)
                    self.stats.tail_count += 1
        if self.lanes.storage:
            self.stats.storage_batch_tail += len(self.lanes.storage)
        for fish in list(self.lanes.storage):
            self.tracker.mark_unmatched(fish, "unmatched_storage", tick=self.tick)
            self.stats.tail_count += 1
        self.lanes.storage.clear()
        for fish in self.lanes.reflow:
            self.tracker.mark_unmatched(fish, "unmatched_reflow", tick=self.tick)
            self.stats.tail_count += 1
        for fish in self.lanes.outside:
            if self.tracker.traces[fish.id].status == "unmatched_outside":
                self.tracker.unmatched.append(self.tracker.traces[fish.id])

        self.stats.unmatched_count = len(self.tracker.unmatched)
        self.finished = True
        data_dir = _root / "data"
        report_path = data_dir / f"run_report_seed_{self.seed}.csv"
        cartons_path = data_dir / f"cartons_seed_{self.seed}.csv"
        remaining_path = data_dir / f"remaining_seed_{self.seed}.csv"
        timeout_tail_path = data_dir / f"timeout_tail_seed_{self.seed}.csv"
        self.tracker.save_report(report_path)
        self._save_cartons_csv(cartons_path)
        self._save_remaining_csv(remaining_path)
        self._save_timeout_tail_csv(timeout_tail_path)
        cap = self.lanes.storage_capacity
        peak = self.stats.storage_max
        peak_pct = round(peak / cap * 100, 1) if cap else 0.0
        self._log(
            "批末",
            f"完成 | 成盒 {self.stats.cartons} | {self._timeout_status()} | "
            f"暂存峰值 {peak}/{cap}({peak_pct}%)",
            force=True,
        )

    def print_report(self) -> None:
        """在终端打印批末汇总，突出配箱、暂存峰值、超时、回流、批末尾料五项指标。"""
        m = self.build_final_metrics()
        rounds_dist: dict[int, int] = {}
        for t in self.tracker.traces.values():
            rounds_dist[t.rounds] = rounds_dist.get(t.rounds, 0) + 1
        report_path = _root / "data" / f"run_report_seed_{self.seed}.csv"

        print("\n" + "=" * 60)
        print("智能分拣汇总")
        print("=" * 60)
        print(f"  批次种子     : {self.seed}")
        if self.exclude_outside_stats:
            print(
                f"  入料总数     : {self.stats.input_count} "
                f"(规格内，规格外 {self.stats.outside_count} 条不计)"
            )
        else:
            print(f"  入料总数     : {self.stats.input_count}")
        print(
            f"  入料总重     : "
            f"{_fmt_weight_g(self.stats.input_weight)}"
        )
        if self.stop_mode == STOP_MODE_WEIGHT:
            print(f"  结束条件     : 总重 ≥ {self.stop_weight_g / 1_000_000:.3f}t")
        else:
            print(f"  结束条件     : 条数 {self.stop_count}")

        print("-" * 60)
        print("【配箱】")
        print(f"  箱数         : {m['cartons']}")
        print(f"  鱼数         : {m['packed_fish']}")

        print("【暂存箱峰值】")
        print(
            f"  峰值/上限    : {m['storage_peak']} / {m['storage_capacity']} 条"
        )
        print(f"  峰值使用率   : {m['storage_peak_pct']}%")

        print("【超时尾料】")
        print(
            f"  料道         : {m['timeout_lane_count']} 条, "
            f"{_fmt_weight_g(m['timeout_lane_weight_g'])}"
        )
        print(
            f"  暂存箱       : {m['timeout_storage_count']} 条, "
            f"{_fmt_weight_g(m['timeout_storage_weight_g'])}"
        )
        print(
            f"  合计         : {m['timeout_total_count']} 条, "
            f"{_fmt_weight_g(m['timeout_total_weight_g'])}"
        )

        print("【回流 / 暂存箱满】")
        print(
            f"  批末回流未配盒: {m['reflow_batch_count']} 条, "
            f"{_fmt_weight_g(m['reflow_batch_weight_g'])}"
        )
        print(
            f"  暂存箱爆满尾料: {m['storage_full_count']} 条, "
            f"{_fmt_weight_g(m['storage_full_weight_g'])}"
        )

        print("【批末未配盒】（扫尾时料道/暂存/回流队列剩余）")
        print(
            f"  料道         : {m['tail_lane_count']} 条, "
            f"{_fmt_weight_g(m['tail_lane_weight_g'])}"
        )
        print(
            f"  暂存箱       : {m['tail_storage_count']} 条, "
            f"{_fmt_weight_g(m['tail_storage_weight_g'])}"
        )
        print(
            f"  回流队列     : {m['tail_reflow_count']} 条, "
            f"{_fmt_weight_g(m['tail_reflow_weight_g'])}"
        )
        print(
            f"  合计         : {m['tail_batch_count']} 条, "
            f"{_fmt_weight_g(m['tail_batch_weight_g'])}"
        )

        print("-" * 60)
        if not self.exclude_outside_stats:
            print(f"  规格外       : {self.stats.outside_count}")
        print(f"  未匹配/尾料  : {self.stats.unmatched_count} (含超时/规格外/箱满等全部尾料)")
        print(f"  运行时长     : {self.tick}s")
        if self.cartons:
            weights = [c.weight for c in self.cartons]
            print(f"  盒重范围     : {min(weights)}g ~ {max(weights)}g")
            print(f"  盒重均值     : {sum(weights) / len(weights):.0f}g")
        print("  轮数分布     :", dict(sorted(rounds_dist.items())))
        print(f"  明细报告     : {report_path}")
        print("=" * 60)



    def run(self, realtime: bool = True) -> None:
        """循环 process_one 直至批次耗尽，再 finish_batch 并打印报告。"""
        print("智能分拣引擎启动")
        print(f"  批次       : fish_seed_{self.seed}.csv ({self.total_fish} 条)")
        print(f"  分拣规格   : {len(self.specs)} 个 ({', '.join(s.upper() for s in self.specs[:3])} …)")
        print(f"  模块批次   : A/B/C 共 {len(MODULE_SPECS)} 组")
        print(f"  盒重目标   : {TARGET_MIN}-{TARGET_MAX}g")
        clock_label = (
            "进料步进"
            if self.timeout_clock == TIMEOUT_CLOCK_INTAKE
            else "真实时间"
        )
        unit = "步" if self.timeout_clock == TIMEOUT_CLOCK_INTAKE else "s"
        print(
            f"  移动超时   : {self.move_timeout}{unit} "
            f"({clock_label}计时，超出则直接记为尾料，不进回流)"
        )
        print(f"  入料间隔   : {self.interval}s")
        print(f"  暂存容量   : {self.lanes.storage_capacity} 条")
        if self.verbose:
            print("  详细日志   : 开启 [流向] 每条入料/封箱/暂存出入")
            print("               [超时预警] 等待≥80%阈值 | [超时]/[暂存峰值] 始终输出")
        else:
            print("  详细日志   : 关闭（加 -v 查看每条流向；进度/超时/峰值仍输出）")
        print("-" * 60)

        while self.process_one():
            if realtime:
                time.sleep(self.interval)

        self.finish_batch()
        self.print_report()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def run_demo(
    seed: int = DEFAULT_SEED,
    total: int = DEFAULT_TOTAL,
    interval: float = 1.0,
    move_timeout: int = DEFAULT_MOVE_TIMEOUT,
    fast: bool = False,
    verbose: bool = False,
    csv_path: Path | None = None,
) -> SchedulerEngine:
    """作用：快捷封装：加载批次→创建引擎→跑完全程→返回引擎实例。
    前端：无 Web UI；供脚本/测试一次性跑完，等价于 index「快速跑完」后端逻辑。"""
    records = load_or_generate_batch(seed=seed, total=total, csv_path=csv_path)
    engine = SchedulerEngine(
        batch_records=records,
        seed=seed,
        interval=interval,
        move_timeout=move_timeout,
        verbose=verbose,
    )
    engine.run(realtime=not fast)
    return engine


def main() -> None:
    """作用：CLI 入口，解析命令行参数并启动引擎独立运行（不经过 web_server）。
    前端：无；命令行 python src/Scheduler_Engine.py --seed 42 --fast。"""
    parser = argparse.ArgumentParser(description="智能分拣引擎（25000 条种子批次）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子")
    parser.add_argument("-n", "--total", type=int, default=DEFAULT_TOTAL, help="入料总条数")
    parser.add_argument("-i", "--interval", type=float, default=1.0, help="每条间隔秒数")
    parser.add_argument(
        "--move-timeout",
        type=int,
        default=DEFAULT_MOVE_TIMEOUT,
        help="料道移动超时阈值（步或秒，见 --timeout-clock）",
    )
    parser.add_argument(
        "--timeout-clock",
        choices=[TIMEOUT_CLOCK_INTAKE, TIMEOUT_CLOCK_REAL],
        default=DEFAULT_TIMEOUT_CLOCK,
        help="超时计时：intake=每入料一步+1；real=真实系统秒",
    )
    parser.add_argument("--csv", type=Path, default=None, help="指定种子 CSV 路径")
    parser.add_argument("--fast", action="store_true", help="快速跑完（不等待）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条鱼流向、封箱、暂存出入及超时预警")
    parser.add_argument("--log-every", type=int, default=500, help="进度日志间隔")
    args = parser.parse_args()

    records = load_or_generate_batch(seed=args.seed, total=args.total, csv_path=args.csv)
    engine = SchedulerEngine(
        batch_records=records,
        seed=args.seed,
        interval=args.interval,
        move_timeout=args.move_timeout,
        verbose=args.verbose,
        log_every=args.log_every,
        timeout_clock=args.timeout_clock,
    )
    engine.run(realtime=not args.fast)


if __name__ == "__main__":
    main()
