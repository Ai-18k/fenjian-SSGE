#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project : kaomam_project
@File : Scheduler_Engine.py
@Author : 18k
@Date : 2026/6/1 13:35
@Description: 智能分拣引擎 — 使用随机种子批次，DFS 自由组合配盒，三合一料道容量，超时回流

前端页面（经 web_server.py 暴露 API）：
  · index.html（/）统计面板：模块库存、成盒/尾料弹窗、趋势图、启停控制
  · fifo_monitor.html（/monitor）FIFO 动画：料道画布、需求广播、装箱工位、运行日志

方法 → 前端模块速查：
  批次/配置  load_or_generate_batch, normalize_enabled_specs, classify_bucket
             → fifo_monitor「启用规格」+ GET /api/batch、/api/config
  入料推进  process_one, record_to_fish, SortingLanes.enqueue
             → 两页「累计来鱼」；fifo_monitor 逐条 spawn 动画
  封箱      BoxPlanner.find_plan, _try_pack_all
             → 两页「完成箱数」；fifo_monitor 装箱工位状态
  回流防堵  _anti_block, _process_reflow_intake, divert_head
             → 两页「回流/尾料」；fifo_monitor 规格外尾料箱
  需求广播  collect_demands, _lane_demand_entry, _best_diagnostic_for_spec
             → fifo_monitor「进料口广播」「需求地址列表」
  状态快照  get_snapshot
             → GET /api/state（两页轮询核心）
  批末导出  finish_batch, save_report, _save_cartons_csv, _save_remaining_csv
             → index「成盒数据」「尾料数据」「下载报告」+ 对应 CSV API
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

# ---------------------------------------------------------------------------
# 规格表
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

MODULE_SPECS: dict[str, tuple[str, ...]] = {
    "A": ("15p", "20p", "25p", "30p", "35p", "40p"),
    "B": ("45p", "50p", "60p", "70p", "80p", "90p"),
    "C": ("100p", "110p", "120p", "130p", "140p", "150p"),
}

ALL_SPECS: tuple[str, ...] = tuple(SPECS.keys())
DEFAULT_ENABLED_SPECS: tuple[str, ...] = ("15p", "20p", "25p", "30p", "35p", "40p")
DEMO_SPECS: tuple[str, ...] = DEFAULT_ENABLED_SPECS

TARGET_MIN = 4980
TARGET_MAX = 5030
TARGET_MID = 5005

BUCKETS = ("small", "medium", "large")
BUCKET_LABEL = {"small": "小", "medium": "中", "large": "大"}

DEFAULT_TOTAL = 25000
DEFAULT_SEED = 42
DEFAULT_MOVE_TIMEOUT = 30
DEFAULT_CAP_FACTOR = 1  # 三合一扩容：min(counts) + cap_factor（默认 +1）
DEFAULT_STORAGE_CAPACITY = 150
STOP_MODE_COUNT = "count"
STOP_MODE_WEIGHT = "weight"
DEFAULT_STOP_WEIGHT_TONS = 10.0
DEFAULT_STOP_WEIGHT_G = int(DEFAULT_STOP_WEIGHT_TONS * 1_000_000)
TIMEOUT_CLOCK_INTAKE = "intake"
TIMEOUT_CLOCK_REAL = "real"
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


FISH_CACHE=[]


def _load_module(name: str, path: Path):
    """作用：动态加载 plan/ 下 Python 脚本（细分规则、种子生成、深度搜索）。
    前端：无直接对应；为 classify_bucket、load_or_generate_batch、BoxPlanner 提供算法支撑。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_root = Path(__file__).resolve().parent.parent
_bucket_rules = _load_module("bucket_rules", _root / "plan" / "细分规则.py")
_seed_gen = _load_module("fish_seed_gen", _root / "plan" / "随机种子生成.py")
_depth_search = _load_module("depth_search", _root / "plan" / "深度搜索.py")
_demand_calc = _load_module("demand_calc", _root / "plan" / "计算需求.py")
dfs_find_best_from_items = _depth_search.dfs_find_best_from_items
BoxDemandCalculator = _demand_calc.BoxDemandCalculator
_intersect_interval = _demand_calc.intersect_interval
DFS_MAX_BUFFER = _depth_search.DEFAULT_DFS_MAX_BUFFER
DFS_MAX_NODES = _depth_search.DEFAULT_DFS_MAX_NODES
DFS_WINDOW_PER_BUCKET = 15


def dfs_max_buffer_for_spec(spec: str) -> int:
    """DFS 搜索窗口上限：小规格用默认 42；100p+ 需覆盖最大装箱尾数（如 150p→76）。"""
    counts = SPECS[spec]["counts"]
    return max(DFS_MAX_BUFFER, max(counts) + 5)


def dfs_window_per_bucket_for_spec(spec: str) -> int:
    """料道积压时各路取样条数，须能凑够该规格最小装箱尾数。"""
    return max(DFS_WINDOW_PER_BUCKET, math.ceil(dfs_max_buffer_for_spec(spec) / 3))

BUCKET_RANGES = {}
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
    for r in ranges:
        lo, hi = int(r[0]), int(r[1])
        if lo <= weight <= hi:
            return True
    return False


def spec_inventory_weights(lanes: "SortingLanes", spec: str) -> list[int]:
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
    """作用：单条鱼的运行时实体（重量、规格、小中大分区、轮次）。
    前端：fifo_monitor.html 画布鱼动画；/api/state 不直接暴露单鱼，经 modules/demands 间接体现。"""
    id: int
    weight: int
    spec: str | None = None
    bucket: str | None = None
    enter_time: int = 0
    rounds: int = 1


@dataclass
class FishTrace:
    """作用：单条鱼全生命周期追踪记录（入料、封箱、回流、尾料状态）。
    前端：index.html「尾料数据」弹窗、GET /api/remaining、GET /api/report 导出。"""
    fish_id: int
    weight: int
    spec: str | None
    rounds: int = 1
    first_in_time: int | None = None
    outbound_time: int | None = None
    status: str = "pending"
    reflow_reasons: list[str] = field(default_factory=list)
    bucket: str | None = None
    lane_wait_s: int | None = None

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
    """作用：一次成功封箱的方案（规格、尾数、总重、小中大配比、入选鱼列表）。
    前端：index.html「成盒数据」弹窗与趋势；GET /api/cartons、/api/state.recent_cartons。"""
    spec: str
    count: int
    weight: int
    parts: dict[str, int]
    fish: list[Fish] = field(default_factory=list)
    pick_ids: frozenset[int] | None = None  # DFS 自由组合：按鱼 ID 从料道移除


@dataclass
class Stats:
    """作用：引擎累计统计（入料、成盒、回流、规格外、尾料分项）。
    前端：index.html 顶栏统计卡；fifo_monitor.html「累计来鱼/完成箱数/回流尾料」；GET /api/state。"""
    input_count: int = 0
    input_weight: int = 0
    packed_fish: int = 0
    cartons: int = 0
    outside_count: int = 0
    reflow_count: int = 0
    timeout_tail: int = 0
    overflow_reflow: int = 0
    storage_in: int = 0
    storage_to_lane: int = 0
    storage_packed: int = 0
    storage_timeout_tail: int = 0
    storage_full_tail: int = 0
    storage_batch_tail: int = 0
    storage_max: int = 0
    unmatched_count: int = 0
    tail_count: int = 0


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
    """作用：某规格小/中/大料道三合一合计容量上限。
    默认 = min(counts) + cap_factor（默认扩容 +1）。
    前端：防堵超容判定；index.html 模块库存 total 与 capacity×3 对照。"""
    return spec_min_count(spec) + max(1, cap_factor)


def lane_capacity(spec: str, cap_factor: int = DEFAULT_CAP_FACTOR) -> int:
    """作用：单分区参考容量（三合一合计容量三等分，仅用于 UI 标注）。
    前端：fifo_monitor.html 料道 queue/cap 标注；index.html 模块库存 capacity 字段。"""
    return math.ceil(spec_total_capacity(spec, cap_factor) / 3)


def record_to_fish(
    record,
    tick: int,
    enabled: set[str] | None = None,
) -> Fish:
    """作用：将批次 CSV 记录转为运行时 Fish 对象（含规格与小中大分区）。
    前端：process_one 每入料一条驱动 fifo_monitor 动画 spawn 与 index 入料计数。"""
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


def carton_fish_detail(plan: BoxPlan) -> list[dict]:
    """作用：将封箱方案展开为每条鱼的 id/weight/bucket 明细列表。
    前端：index.html「成盒数据」弹窗鱼明细；GET /api/cartons、/api/state.carton_records.fish。"""
    return [
        {
            "id": f.id,
            "weight": f.weight,
            "bucket": f.bucket or "",
        }
        for f in plan.fish
    ]


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
        """作用：超容鱼进入暂存箱（容量满则失败）。
        前端：fifo_monitor 暂存箱「进」动画与计数。"""
        if fish.spec is None or len(self.storage) >= self.storage_capacity:
            return False
        fish.enter_time = tick
        self.storage.append(fish)
        tracker.mark_stored(fish, tick)
        return True

    def pop_storage_head(self) -> Fish | None:
        if not self.storage:
            return None
        return self.storage.pop(0)

    def remove_from_storage_ids(self, fish_ids: set[int]) -> list[Fish]:
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
        return self.total_in_spec(spec) + len(self.storage_for_spec(spec))

    def _put_in_lane(self, fish: Fish, tick: int) -> str:
        """作用：将鱼放入对应规格+小中大料道队尾。
        前端：fifo_monitor.html 鱼入料道动画；index.html 模块 small/medium/large 计数。"""
        fish.enter_time = tick
        self.queues[fish.spec][fish.bucket].append(fish)
        return fish.bucket

    def enqueue(self, fish: Fish, tick: int, tracker: FishTracker) -> str:
        """作用：入料主入口：规格外进 outside 队列，否则进对应料道。
        前端：fifo_monitor 正常入料/规格外尾料轨；两页 outside_count 统计。"""
        if fish.spec is None or fish.spec not in self.queues:
            fish.enter_time = tick
            self.outside.append(fish)
            tracker.register(fish, tick, status="unmatched_outside")
            return "outside"
        bucket = self._put_in_lane(fish, tick)
        tracker.register(fish, tick, status="queued")
        return bucket

    def try_enqueue_reflow(
        self,
        fish: Fish,
        tick: int,
        tracker: FishTracker,
        cap_factor: int = DEFAULT_CAP_FACTOR,
    ) -> bool:
        """作用：尝试将回流鱼重新放入料道（规格三合一容量未满则成功）。
        前端：GET /api/state.reflow_queue 待入库数；fifo_monitor 回流后再次入道动画。"""
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
        """作用：按封箱方案从暂存箱+料道移除对应鱼（暂存优先）。
        前端：fifo_monitor 装箱工位出料动画；index 成盒数据 fish_ids。"""
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
        """作用：弹出料道队头鱼，优先送入暂存箱；箱满则记尾料。
        返回 (fish, outcome)：stored | storage_full。"""
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
        """作用：弹出料道队头超时鱼，直接记为尾料（不进回流队列）。
        前端：index.html「超时尾料」；GET /api/state.timeout_tail_log。"""
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
        """DFS 窗口搜索；无解则本 tick 不封箱。前端：封箱触发点。"""
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


UI_BUCKET = {"small": "light", "medium": "mid", "large": "heavy"}


def module_of_spec(spec: str) -> str:
    """作用：将规格映射到模块 A/B/C。
    前端：fifo_monitor.html 画布三模块分区与需求地址前缀（A/15p/light）。"""
    for mod, spec_list in MODULE_SPECS.items():
        if spec in spec_list:
            return mod
    return "A"


def spec_address(mod: str, spec: str) -> str:
    """作用：生成规格需求广播地址（如 A/15p），不拆小/中/大。
    前端：fifo_monitor「进料口广播」「需求地址列表」中的 address 字段。"""
    return f"{mod}/{spec}"


def lane_address(mod: str, spec: str, bucket: str) -> str:
    """作用：小/中/大分区地址（封箱/动画内部仍用）。"""
    ui = UI_BUCKET.get(bucket, bucket)
    return f"{mod}/{spec}/{ui}"


class SchedulerEngine:
    """作用：智能分拣仿真主引擎，串联入料→料道→封箱→回流→批末导出。
    前端：web_server SimulationRunner 后台驱动；两页通过 GET /api/state 读取其快照。"""

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
        """作用：初始化引擎（批次、料道、追踪器、统计）；POST /api/start 时创建。
        前端：index/fifo_monitor「开始模拟」；控制种子、条数/总重、超时、启用规格、料道容量倍率。
        exclude_outside_stats：批量测试用，规格外不计入料/结束条件，进度改显超时鱼。"""
        self.seed = seed
        self.interval = interval
        self.specs = specs
        self.move_timeout = move_timeout
        self.cap_factor = cap_factor
        self.verbose = verbose
        self.log_every = log_every
        self.exclude_outside_stats = exclude_outside_stats
        self.stop_mode = stop_mode if stop_mode in (STOP_MODE_COUNT, STOP_MODE_WEIGHT) else STOP_MODE_COUNT
        self.stop_count = max(1, stop_count)
        self.stop_weight_g = max(1, stop_weight_g)
        self.timeout_clock = (
            timeout_clock
            if timeout_clock in (TIMEOUT_CLOCK_INTAKE, TIMEOUT_CLOCK_REAL)
            else DEFAULT_TIMEOUT_CLOCK
        )

        self.batch = batch_records or load_or_generate_batch(seed=seed)
        self.total_fish = len(self.batch)
        if self.stop_mode == STOP_MODE_COUNT:
            self.total_fish = min(self.total_fish, self.stop_count)
            self.batch = self.batch[: self.total_fish]
        self._cursor = 0

        self.lanes = SortingLanes(specs=specs)
        self.planner = BoxPlanner()
        self.tracker = FishTracker()
        self.stats = Stats()
        self.cartons: list[BoxPlan] = []
        self._time_origin = time.monotonic()
        self._paused_at: float | None = None
        self.tick = 0
        self.finished = False
        self.events: list[dict] = []
        self.history: list[dict] = []
        self.timeout_tail_log: list[dict] = []
        self.overflow_reflow_log: list[dict] = []

    def _sync_real_tick(self) -> int:
        """真实系统时间（秒）更新 tick；暂停期间冻结。"""
        now = time.monotonic()
        if self._paused_at is not None:
            now = self._paused_at
        self.tick = int(max(0, now - self._time_origin))
        return self.tick

    def _advance_tick(self, steps: int = 1) -> int:
        """按计时方式推进 tick：进料步进=每处理一步 +1；真实时间=墙钟秒。"""
        if self.timeout_clock == TIMEOUT_CLOCK_REAL:
            return self._sync_real_tick()
        self.tick += max(1, steps)
        return self.tick

    def refresh_tick_for_poll(self) -> int:
        """轮询快照时刷新 tick（仅真实时间模式随墙钟增长）。"""
        if self.timeout_clock == TIMEOUT_CLOCK_REAL:
            return self._sync_real_tick()
        return self.tick

    def pause_clock(self) -> None:
        """作用：暂停仿真时钟（冻结 tick 增长）。
        前端：两页「暂停」按钮 POST /api/pause。"""
        if self._paused_at is None:
            self._paused_at = time.monotonic()

    def resume_clock(self) -> None:
        """作用：恢复仿真时钟（补偿暂停期间时长）。
        前端：两页「继续」按钮 POST /api/resume。"""
        if self._paused_at is not None:
            self._time_origin += time.monotonic() - self._paused_at
            self._paused_at = None

    def _event(self, kind: str, msg: str, **extra) -> None:
        """作用：追加运行时事件（入料/封箱/回流/完成）到环形缓冲。
        前端：GET /api/state.events（最近 40 条）；可供日志面板扩展。"""
        evt = {"tick": self.tick, "kind": kind, "msg": msg, **extra}
        self.events.append(evt)
        if len(self.events) > 300:
            self.events.pop(0)

    def _best_diagnostic_for_spec(self, spec: str) -> dict:
        """作用：诊断某规格封箱状态（可装/不足/偏轻/偏重/接近）。
        前端：fifo_monitor「需求地址」reason 字段（如「偏轻」「可装」）；装箱工位待装箱判断。"""
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

    def _spec_demand_entry(self, mod_key: str, spec: str) -> dict:
        """作用：生成单规格需求广播（三合一，不拆小/中/大）。
        前端：fifo_monitor「需求地址列表」单张卡片；GET /api/state.demands 每规格一条。"""
        address = spec_address(mod_key, spec)
        lo, hi = SPECS[spec]["range"]
        base = {
            "module": mod_key,
            "spec": spec,
            "address": address,
            "lane_id": spec,
            "weight_ranges": [[lo, hi]],
            "target": "lane",
        }
        if spec not in self.specs:
            return {
                **base,
                "priority": 9,
                "count": 0,
                "reason": "未启用",
                "active": False,
            }
        weight_ranges = [
            list(r) for r in spec_demand_weight_ranges(self.lanes, spec)
        ]
        base["weight_ranges"] = weight_ranges
        total = self.lanes.total_in_spec(spec)
        diag = self._best_diagnostic_for_spec(spec)
        short = diag["short"]

        if short == "可装":
            return {
                **base,
                "priority": 4,
                "count": 0,
                "reason": "可装",
                "active": False,
            }
        if short == "不足":
            return {
                **base,
                "priority": 2,
                "count": diag["need_count"] or 1,
                "reason": "不足",
                "active": True,
            }
        if short in ("偏轻", "偏重", "等待"):
            return {
                **base,
                "priority": 3,
                "count": diag.get("need_count") or 1,
                "reason": short,
                "active": True,
            }
        if total > 0:
            return {
                **base,
                "priority": 4,
                "count": 1,
                "reason": short or f"料道{total}条",
                "active": short != "接近",
            }
        return {
            **base,
            "priority": 5,
            "count": 0,
            "reason": "监控",
            "active": False,
        }

    def collect_demands(self) -> list[dict]:
        """作用：汇总全部 18 路规格需求（3 模块 × 18 规格），按优先级排序。
        前端：fifo_monitor「进料口广播」「需求地址列表」「活跃需求」计数；GET /api/state.demands。"""
        items: list[dict] = []
        for mod_key, spec_list in MODULE_SPECS.items():
            for spec in spec_list:
                items.append(self._spec_demand_entry(mod_key, spec))
        items.sort(key=lambda d: (d["priority"], d["address"]))
        return items

    def get_snapshot(
        self,
        *,
        since_carton: int = 0,
        since_timeout_tail: int = 0,
        since_overflow_reflow: int = 0,
    ) -> dict:
        """作用：生成引擎完整状态快照，供前端轮询（支持增量 since_* 游标）。
        前端：GET /api/state 核心数据源；index 统计卡/库存/趋势；fifo_monitor 全页同步。"""
        modules: dict[str, dict] = {}
        for mod, spec_list in MODULE_SPECS.items():
            modules[mod] = {}
            for spec in spec_list:
                enabled = spec in self.specs
                if spec not in self.lanes.queues:
                    modules[mod][spec] = {
                        "small": 0,
                        "medium": 0,
                        "large": 0,
                        "total": 0,
                        "capacity": 0,
                        "total_capacity": 0,
                        "enabled": enabled,
                    }
                    continue
                q = self.lanes.queues[spec]
                per_cap = lane_capacity(spec, self.cap_factor)
                total_cap = spec_total_capacity(spec, self.cap_factor)
                modules[mod][spec] = {
                    "small": len(q["small"]),
                    "medium": len(q["medium"]),
                    "large": len(q["large"]),
                    "total": self.lanes.total_in_spec(spec),
                    "capacity": per_cap,
                    "total_capacity": total_cap,
                    "enabled": enabled,
                }
        rounds_dist: dict[str, int] = {}
        for t in list(self.tracker.traces.values()):
            k = str(t.rounds)
            rounds_dist[k] = rounds_dist.get(k, 0) + 1
        recent_cartons = [
            {
                "spec": c.spec,
                "count": c.count,
                "weight": c.weight,
                "parts": c.parts,
            }
            for c in self.cartons[-8:]
        ]
        since_carton = max(0, min(since_carton, len(self.cartons)))
        since_timeout_tail = max(0, min(since_timeout_tail, len(self.timeout_tail_log)))
        since_overflow_reflow = max(0, min(since_overflow_reflow, len(self.overflow_reflow_log)))
        carton_slice = self.cartons[since_carton:]
        carton_records = [
            {
                "seq": since_carton + idx + 1,
                "spec": c.spec,
                "count": c.count,
                "weight": c.weight,
                "parts": dict(c.parts),
                "fish_ids": [f.id for f in c.fish],
                "fish_weights": [f.weight for f in c.fish],
                "fish": carton_fish_detail(c),
            }
            for idx, c in enumerate(carton_slice)
        ]
        timeout_tail_slice = self.timeout_tail_log[since_timeout_tail:]
        overflow_reflow_slice = self.overflow_reflow_log[since_overflow_reflow:]
        remaining_fish = self.tracker.remaining_records(
            end_tick=self.tick,
            batch_seed=self.seed,
        )
        demands = self.collect_demands()
        active_demands = [d for d in demands if d.get("active")]
        inlet_demand = next(
            (d for d in demands if d.get("active") and d.get("priority", 9) <= 3),
            active_demands[0] if active_demands else None,
        )
        storage_by_spec: dict[str, int] = {}
        for f in list(self.lanes.storage):
            if f.spec:
                storage_by_spec[f.spec] = storage_by_spec.get(f.spec, 0) + 1
        return {
            "tick": self.tick,
            "finished": self.finished,
            "seed": self.seed,
            "move_timeout": self.move_timeout,
            "timeout_clock": self.timeout_clock,
            "cap_factor": self.cap_factor,
            "enabled_specs": list(self.specs),
            "stop_mode": self.stop_mode,
            "stop_target_count": self.stop_count if self.stop_mode == STOP_MODE_COUNT else None,
            "stop_target_weight_g": self.stop_weight_g if self.stop_mode == STOP_MODE_WEIGHT else None,
            "stop_target_weight_tons": (
                round(self.stop_weight_g / 1_000_000, 3)
                if self.stop_mode == STOP_MODE_WEIGHT
                else None
            ),
            "batch_total": self.total_fish,
            "total_fish": (
                self.stop_count if self.stop_mode == STOP_MODE_COUNT else self.total_fish
            ),
            "input_count": self.stats.input_count,
            "input_weight": self.stats.input_weight,
            "input_weight_tons": round(self.stats.input_weight / 1_000_000, 3),
            "cartons": self.stats.cartons,
            "packed_fish": self.stats.packed_fish,
            "outside_count": self.stats.outside_count,
            "reflow_count": self.stats.reflow_count,
            "timeout_tail": self.stats.timeout_tail,
            "overflow_reflow": self.stats.overflow_reflow,
            "unmatched_count": len(self.tracker.unmatched),
            "tail_count": self.stats.tail_count,
            "reflow_queue": len(self.lanes.reflow),
            "outside_queue": len(self.lanes.outside),
            "storage_box": {
                "count": self.lanes.storage_count(),
                "capacity": self.lanes.storage_capacity,
                "max": self.stats.storage_max,
                "by_spec": storage_by_spec,
            },
            "storage_in": self.stats.storage_in,
            "storage_to_lane": self.stats.storage_to_lane,
            "storage_packed": self.stats.storage_packed,
            "storage_timeout_tail": self.stats.storage_timeout_tail,
            "storage_full_tail": self.stats.storage_full_tail,
            "storage_batch_tail": self.stats.storage_batch_tail,
            "storage_max": self.stats.storage_max,
            "modules": modules,
            "recent_cartons": recent_cartons,
            "carton_records": carton_records,
            "carton_total": len(self.cartons),
            "remaining_fish": remaining_fish,
            "remaining_count": len(remaining_fish),
            "events": self.events[-40:],
            "timeout_tail_log": timeout_tail_slice,
            "timeout_tail_total": len(self.timeout_tail_log),
            "overflow_reflow_log": overflow_reflow_slice,
            "overflow_reflow_total": len(self.overflow_reflow_log),
            "snapshot_delta": (
                since_carton > 0 or since_timeout_tail > 0 or since_overflow_reflow > 0
            ),
            "demands": demands,
            "active_demands": active_demands,
            "active_demand_count": len(active_demands),
            "inlet_demand": inlet_demand,
            "history": self.history[-120:],
            "rounds_top": dict(sorted(rounds_dist.items(), key=lambda x: int(x[0]))[:12]),
            "target": {"min": TARGET_MIN, "max": TARGET_MAX},
        }

    def _log(self, msg: str, force: bool = False, kind: str = "info", **extra) -> None:
        """作用：写引擎日志（verbose 时打印终端，非 info 时记入 events）。
        前端：CLI 调试输出；GET /api/state.events 中间接入 fifo_monitor 运行日志可扩展。"""
        if kind != "info" or force:
            self._event(kind, msg, **extra)
        if self.verbose or force:
            print(f"[t={self.tick:05d}s] {msg}")

    def _note_storage_peak(self) -> None:
        """记录暂存箱历史峰值（条数）。"""
        count = self.lanes.storage_count()
        if count > self.stats.storage_max:
            self.stats.storage_max = count

    def _apply_box_plan(self, plan: BoxPlan) -> None:
        """执行一次封箱：从料道/暂存移除鱼并更新统计。"""
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
        self._log(
            f"封箱 #{self.stats.cartons:04d}: {plan.spec.upper()} "
            f"{plan.count}尾 {plan.weight}g ({parts})",
            kind="pack",
            spec=plan.spec,
            weight=plan.weight,
            count=plan.count,
        )

    def _try_pack_spec(self, spec: str) -> int:
        """对单规格循环 DFS 封箱直至无解。"""
        packed = 0
        while True:
            plan = self.planner.find_plan(self.lanes, spec)
            if plan is None:
                break
            self._apply_box_plan(plan)
            packed += 1
        return packed

    def _try_pack_all(self) -> int:
        """作用：遍历启用规格尝试 DFS 封箱，成功则移除料道鱼并累加统计。
        前端：两页「完成箱数」；fifo_monitor 装箱工位；index 成盒数据与趋势图。"""
        packed = 0
        for spec in self.specs:
            packed += self._try_pack_spec(spec)
        return packed

    def _intake_weight_ranges(self, spec: str) -> list[tuple[int, int]]:
        """入料匹配用重量区间：未达最小尾数放宽；满容无解时按偏轻/偏重收窄。"""
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
        """当前料道状态下，该鱼是否为「最需要进道」的重量。"""
        if fish.spec is None:
            return False
        return weight_in_ranges(fish.weight, self._intake_weight_ranges(fish.spec))

    def _push_intake_storage(self, fish: Fish, reason: str) -> bool:
        """入料侧送入暂存箱；箱满则记尾料。"""
        if self.lanes.try_push_storage(fish, self.tick, self.tracker):
            self.stats.storage_in += 1
            self._event(
                "storage_in",
                f"暂存 #{fish.id} {fish.spec.upper()} {fish.weight}g ({reason})",
                fish_id=fish.id,
                spec=fish.spec,
                weight=fish.weight,
                reason=reason,
            )
            if self.verbose:
                self._log(
                    f"暂存入箱: #{fish.id} {fish.spec.upper()}-{BUCKET_LABEL[fish.bucket]} "
                    f"{fish.weight}g ({reason}) "
                    f"{self.lanes.storage_count()}/{self.lanes.storage_capacity}",
                    kind="storage_in",
                    reason=reason,
                    fish_id=fish.id,
                )
            return True
        self.tracker.mark_unmatched(fish, "unmatched_storage_full", tick=self.tick)
        self.stats.storage_full_tail += 1
        self.stats.tail_count += 1
        self._log(
            f"暂存箱满: #{fish.id} {fish.spec.upper()}-{BUCKET_LABEL[fish.bucket]} → 尾料",
            kind="storage_full",
            fish_id=fish.id,
        )
        return False

    def _make_room_for_intake(self, fish: Fish) -> bool:
        """料道已满且 incoming 匹配需求：把最不需要的分区队头换入暂存箱腾位。"""
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
            self.overflow_reflow_log.append(
                {
                    "tick": self.tick,
                    "fish_id": moved.id,
                    "weight": moved.weight,
                    "spec": spec,
                    "bucket": evict_bucket,
                    "lane_len": self.lanes.total_in_spec(spec) + 1,
                    "cap": cap,
                    "rounds": moved.rounds,
                    "destination": "storage",
                }
            )
            self._log(
                f"腾位暂存: #{moved.id} {spec.upper()}-{BUCKET_LABEL[evict_bucket]} "
                f"→ 为 #{fish.id}({fish.weight}g) 让路",
                kind="storage_in",
                reason="swap_for_demand",
                fish_id=moved.id,
            )
            return self.lanes.total_in_spec(spec) < cap
        return False

    def _route_spec_intake(self, fish: Fish) -> str:
        """按容量 + 动态需求决定入料道或暂存箱（满容不硬塞，不匹配则暂存）。"""
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
        """作用：每步将回流队列中的鱼尝试重新放入料道。
        前端：GET /api/state.reflow_queue 待入库数；fifo_monitor 回流鱼二次入道。"""
        remaining: list[Fish] = []
        for fish in self.lanes.reflow:
            if not self.lanes.try_enqueue_reflow(
                fish, self.tick, self.tracker, self.cap_factor
            ):
                remaining.append(fish)
        self.lanes.reflow = remaining

    def _release_storage_by_demands(self) -> int:
        """暂存箱按规格广播需求（重量区间）出库，自动落入对应小/中/大分区。"""
        if not self.lanes.storage:
            return 0
        released = 0
        for demand in self.collect_demands():
            if not demand.get("active") or demand.get("priority", 9) > 3:
                continue
            spec = demand["spec"]
            if spec not in self.specs:
                continue
            count = demand.get("count") or 0
            if count <= 0:
                continue
            ranges = [tuple(r) for r in demand.get("weight_ranges", [])]
            if not ranges:
                ranges = spec_demand_weight_ranges(self.lanes, spec)
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
                self._log(
                    f"暂存出库: {len(moved)}条 → {spec.upper()} "
                    f"区间 {ranges} ({demand.get('reason', '')})",
                    kind="storage_out",
                )
        return released

    def _monitor_storage(self) -> None:
        """作用：暂存箱等待最久者超时 → 尾料（不回料道、不回流）。
        前端：fifo_monitor 暂存箱监控；GET /api/state.storage_timeout_tail。"""
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
        self._log(
            f"暂存超时尾料: #{fish.id} {fish.spec.upper()}-{BUCKET_LABEL[fish.bucket]} "
            f"等待 {dwell}s(阈值{self.move_timeout}s)",
            kind="timeout_tail",
            reason="storage_timeout",
            fish_id=fish.id,
            dwell_s=dwell,
            source="storage",
        )

    def _anti_block(self) -> None:
        """作用：防堵逻辑——队首超时则直接记为尾料（超容改由入料侧路由暂存，不弹队头）。
        前端：两页「回流/尾料」分项；fifo_monitor 暂存箱；GET /api/state 超时日志。"""
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
                        self._log(
                            f"超时尾料: #{fish.id} {spec.upper()}-{BUCKET_LABEL[bucket]} "
                            f"料道等待 {dwell}s(阈值{self.move_timeout}s) → 计入尾料",
                            kind="timeout_tail",
                            reason="timeout",
                            fish_id=fish.id,
                            rounds=fish.rounds,
                            dwell_s=dwell,
                            threshold_s=self.move_timeout,
                            first_in_time=first_in,
                        )
                    return

    def _record_history(self) -> None:
        """作用：按 tick 采样入料/成盒/回流历史点，供趋势图使用。
        前端：index.html 运行趋势折线图；GET /api/state.history。"""
        step = max(1, self.log_every // 5)
        if self.tick % step != 0 and not self.finished:
            return
        self.history.append(
            {
                "tick": self.tick,
                "input": self.stats.input_count,
                "cartons": self.stats.cartons,
                "reflow": self.stats.reflow_count,
                "packed": self.stats.packed_fish,
            }
        )
        if len(self.history) > 500:
            self.history.pop(0)

    def _save_cartons_csv(self, path: Path) -> None:
        """作用：导出成盒明细 CSV（cartons_seed_{seed}.csv）。
        前端：index.html「成盒数据」导出；GET /api/cartons.csv。"""
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
        """作用：判断是否已达入料结束条件（按条数或按累计总重）。"""
        if self.stop_mode == STOP_MODE_WEIGHT:
            return self.stats.input_weight >= self.stop_weight_g
        if self.exclude_outside_stats:
            return self.stats.input_count >= self.stop_count
        return self._cursor >= self.stop_count

    def _timeout_fish_total(self) -> int:
        return self.stats.timeout_tail + self.stats.storage_timeout_tail

    def process_one(self) -> bool:
        """作用：推进仿真一步——入料→回流再入道→封箱→防堵；达到结束条件返回 False。
        前端：web_server 后台线程按 speed 调用；两页 input_count；fifo_monitor 逐条 spawn。"""
        if self._cursor >= self.total_fish or self._intake_complete():
            return False

        self._advance_tick(1)
        record = self.batch[self._cursor]
        self._cursor += 1
        is_outside = record.outside or record.spec is None
        if not (self.exclude_outside_stats and is_outside):
            self.stats.input_count += 1
            self.stats.input_weight += record.weight

        fish = record_to_fish(record, self.tick, enabled=set(self.specs))

        # 回流/暂存出库 → 封箱腾位 → 再按需求决定入料道或暂存箱
        self._process_reflow_intake()
        self._release_storage_by_demands()
        self._try_pack_all()
        self._note_storage_peak()

        if is_outside or fish.spec is None:
            self.stats.outside_count += 1
            self.lanes.enqueue(fish, self.tick, self.tracker)
            self._event(
                "outside",
                f"入料 #{fish.id} {fish.weight}g → 规格外",
                fish_id=fish.id,
                weight=fish.weight,
            )
            if self.verbose:
                self._log(f"入料 #{fish.id} {fish.weight}g → 规格外")
        else:
            dest = self._route_spec_intake(fish)
            self._note_storage_peak()
            if dest == "storage":
                self._event(
                    "storage_in",
                    f"入料 #{fish.id} {fish.spec.upper()} {fish.weight}g → 暂存箱",
                    fish_id=fish.id,
                    spec=fish.spec,
                    weight=fish.weight,
                    bucket=fish.bucket,
                )
            else:
                self._event(
                    "intake",
                    f"入料 #{fish.id} {fish.spec.upper()} {fish.weight}g → {BUCKET_LABEL[dest]}区",
                    fish_id=fish.id,
                    spec=fish.spec,
                    weight=fish.weight,
                    bucket=dest,
                    rounds=fish.rounds,
                )
                if self.verbose:
                    self._log(
                        f"入料 #{fish.id} {fish.spec.upper()} {fish.weight}g "
                        f"→ {BUCKET_LABEL[dest]}区 第{fish.rounds}轮"
                    )

        self._release_storage_by_demands()
        self._try_pack_all()
        self._monitor_storage()
        self._note_storage_peak()
        self._anti_block()
        self._record_history()

        if self.stats.input_count % self.log_every == 0:
            if self.stop_mode == STOP_MODE_WEIGHT:
                progress = (
                    f"累计 {self.stats.input_weight / 1_000_000:.2f}t/"
                    f"{self.stop_weight_g / 1_000_000:.2f}t"
                )
            else:
                progress = f"{self.stats.input_count}/{self.stop_count}"
            if self.exclude_outside_stats:
                tail_note = f"超时 {self._timeout_fish_total()}"
            else:
                tail_note = f"规格外 {self.stats.outside_count}"
            self._log(
                f"进度 {progress} | "
                f"成盒 {self.stats.cartons} | 装箱鱼 {self.stats.packed_fish} | "
                f"回流 {self.stats.reflow_count} | {tail_note}",
                force=True,
            )
        return not self._intake_complete()

    def finish_batch(self) -> None:
        """作用：批末扫尾（继续封箱+回流）→标记尾料→写 CSV 报告→finished=true。
        前端：两页批末状态；index「成盒/尾料/报告」；fifo_monitor 快速跑完与尾料箱最终数。"""
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
        self._record_history()
        data_dir = _root / "data"
        report_path = data_dir / f"run_report_seed_{self.seed}.csv"
        cartons_path = data_dir / f"cartons_seed_{self.seed}.csv"
        remaining_path = data_dir / f"remaining_seed_{self.seed}.csv"
        timeout_tail_path = data_dir / f"timeout_tail_seed_{self.seed}.csv"
        self.tracker.save_report(report_path)
        self._save_cartons_csv(cartons_path)
        self._save_remaining_csv(remaining_path)
        self._save_timeout_tail_csv(timeout_tail_path)
        self._event(
            "done",
            "批处理完成，报告已保存",
            report=str(report_path),
            cartons=str(cartons_path),
            remaining=str(remaining_path),
            timeout_tail=str(timeout_tail_path),
        )

    def print_report(self) -> None:
        """作用：在终端打印批末汇总（入料、成盒、回流、尾料、盒重分布）。
        前端：无 Web UI；CLI 直接运行 python Scheduler_Engine.py 时输出。"""
        rounds_dist: dict[int, int] = {}
        for t in self.tracker.traces.values():
            rounds_dist[t.rounds] = rounds_dist.get(t.rounds, 0) + 1
        report_path = _root / "data" / f"run_report_seed_{self.seed}.csv"

        print("\n" + "=" * 60)
        print("智能分拣汇总")
        print("=" * 60)
        print(f"  批次种子     : {self.seed}")
        if self.exclude_outside_stats:
            print(f"  入料总数     : {self.stats.input_count} (规格内，规格外 {self.stats.outside_count} 条不计)")
        else:
            print(f"  入料总数     : {self.stats.input_count}")
        print(f"  入料总重     : {self.stats.input_weight:,}g ({self.stats.input_weight / 1_000_000:.3f}t)")
        if self.stop_mode == STOP_MODE_WEIGHT:
            print(f"  结束条件     : 总重 ≥ {self.stop_weight_g / 1_000_000:.3f}t")
        else:
            print(f"  结束条件     : 条数 {self.stop_count}")
        print(f"  成功装盒数   : {self.stats.cartons}")
        print(f"  成功装箱鱼   : {self.stats.packed_fish}")
        if not self.exclude_outside_stats:
            print(f"  规格外       : {self.stats.outside_count}")
        print(f"  回流总次数   : {self.stats.reflow_count} (历史回流队列)")
        print(f"    暂存入箱   : {self.stats.storage_in}")
        print(f"    暂存回道   : {self.stats.storage_to_lane}")
        print(f"    暂存成盒   : {self.stats.storage_packed}")
        print(f"    暂存超时   : {self.stats.storage_timeout_tail}")
        print(f"    暂存箱满   : {self.stats.storage_full_tail}")
        print(f"    暂存批末   : {self.stats.storage_batch_tail}")
        print(f"    暂存峰值   : {self.stats.storage_max}/{self.lanes.storage_capacity}")
        print(f"    料道超时   : {self.stats.timeout_tail}")
        print(f"    超时合计   : {self._timeout_fish_total()}")
        print(f"  未匹配/尾料  : {self.stats.unmatched_count}")
        print(f"    批末尾料   : {self.stats.tail_count}")
        print(f"  运行时长     : {self.tick}s (真实经过时间)")
        if self.cartons:
            weights = [c.weight for c in self.cartons]
            print(f"  盒重范围     : {min(weights)}g ~ {max(weights)}g")
            print(f"  盒重均值     : {sum(weights) / len(weights):.0f}g")
        print("  轮数分布     :", dict(sorted(rounds_dist.items())))
        print(f"  明细报告     : {report_path}")
        print("=" * 60)



    def run(self, realtime: bool = True) -> None:
        """作用：循环 process_one 直至批次耗尽，再 finish_batch 并打印报告。
        前端：无 Web 直接调用；CLI 入口与 run_demo 使用；Web 版由 web_server 线程驱动。"""
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
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条入料/封箱")
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
