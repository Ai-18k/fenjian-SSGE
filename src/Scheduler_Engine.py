#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project : kaomam_project
@File : Scheduler_Engine.py
@Author : 18k
@Date : 2026/6/1 13:35
@Description: 智能分拣引擎 — 使用随机种子批次，DFS+FIFO 混合配盒，超时回流

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
DEFAULT_CAP_FACTOR = 8
STOP_MODE_COUNT = "count"
STOP_MODE_WEIGHT = "weight"
DEFAULT_STOP_WEIGHT_TONS = 10.0
DEFAULT_STOP_WEIGHT_G = int(DEFAULT_STOP_WEIGHT_TONS * 1_000_000)


def batch_total_for_run(
    stop_mode: str,
    stop_count: int,
    stop_weight_g: int,
) -> int:
    """按结束条件计算需预加载的批次上限（按总重时多备鱼以防批次不足）。"""
    if stop_mode != STOP_MODE_WEIGHT:
        return max(1, stop_count)
    estimated = math.ceil(stop_weight_g / 250) + 5000
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
dfs_find_best_from_items = _depth_search.dfs_find_best_from_items
DFS_MAX_BUFFER = _depth_search.DEFAULT_DFS_MAX_BUFFER
DFS_WINDOW_PER_BUCKET = 15

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


def batch_csv_path(seed: int, enabled_specs: tuple[str, ...]) -> Path:
    """作用：根据种子与启用规格生成批次 CSV 路径。
    前端：GET /api/batch 加载动画鱼序列；index/fifo_monitor 开始模拟前的批次源。"""
    tag = enabled_specs_tag(enabled_specs)
    return _root / "data" / f"fish_seed_{seed}_en_{tag}.csv"


def _batch_valid_for_enabled(
    records: list,
    enabled_specs: tuple[str, ...],
) -> bool:
    """作用：校验缓存批次是否匹配当前启用规格（仅含启用规格鱼 + 真规格外）。
    前端：GET /api/batch 命中缓存前的校验；fifo_monitor 切换启用规格后重载批次。"""
    enabled_set = set(enabled_specs)
    for r in records:
        if r.outside:
            if not _seed_gen.is_true_outside_weight(r.weight):
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


def lane_capacity(spec: str, cap_factor: int = DEFAULT_CAP_FACTOR) -> int:
    """作用：计算单条料道（小/中/大之一）的最大容量。
    前端：fifo_monitor.html 料道 queue/cap 标注；index.html 模块库存 capacity 字段。"""
    return math.ceil(max(SPECS[spec]["counts"]) * cap_factor / 3)


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
) -> list:
    """作用：按启用规格生成或加载种子批次（约 1% 真规格外 <65g 或 >700g）。
    前端：GET /api/batch；fifo_monitor.html「加载批次」；index.html 开始模拟前预生成 data/fish_seed_*.csv。"""
    enabled = normalize_enabled_specs(enabled_specs)
    csv_path = csv_path or batch_csv_path(seed, enabled)
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
    had_timeout = trace.status == "unmatched_timeout" or "timeout" in reasons
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

    def mark_timeout_tail(self, fish: Fish, tick: int, lane_wait_s: int) -> None:
        """作用：超时鱼直接记为尾料（不进回流），记录料道等待时长。
        前端：index.html「尾料数据」「超时尾料」；GET /api/state.timeout_tail_log。"""
        trace = self.traces.get(fish.id)
        if trace is None:
            self.register(fish, tick, status="unmatched_timeout")
            trace = self.traces[fish.id]
        trace.rounds = fish.rounds
        trace.status = "unmatched_timeout"
        trace.outbound_time = tick
        trace.bucket = fish.bucket
        trace.lane_wait_s = lane_wait_s
        if trace not in self.unmatched:
            self.unmatched.append(trace)

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
            for t in sorted(self.unmatched, key=lambda x: x.fish_id)
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

    def try_enqueue_reflow(self, fish: Fish, tick: int, tracker: FishTracker) -> bool:
        """作用：尝试将回流鱼重新放入料道（料道未满则成功）。
        前端：GET /api/state.reflow_queue 待入库数；fifo_monitor 回流后再次入道动画。"""
        if fish.spec is None or fish.spec not in self.queues:
            return False
        lane = self.queues[fish.spec][fish.bucket]
        if len(lane) >= lane_capacity(fish.spec):
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
        """作用：按封箱方案从料道移除对应鱼（DFS 按 ID 或 FIFO 按数量）。
        前端：fifo_monitor 装箱工位出料动画；index 成盒数据 fish_ids。"""
        removed: list[Fish] = []
        if plan.pick_ids:
            targets = set(plan.pick_ids)
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
                n = plan.parts[bucket]
                if n:
                    chunk = self.queues[plan.spec][bucket][:n]
                    del self.queues[plan.spec][bucket][:n]
                    removed.extend(chunk)
                    self._sync_head_enter_time(self.queues[plan.spec][bucket], tick)
        for fish in removed:
            tracker.mark_packed(fish, tick)
        plan.fish = removed
        return removed

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
    """作用：DFS 自由组合 + FIFO 队头回退的混合封箱（plan/深度搜索.py）。
    料道较小时 DFS 全局择优；料道积压时仅搜各路队头窗口，仍无解则 FIFO 队头枚举。
    前端：index.html「完成箱数」；fifo_monitor.html 装箱工位。"""

    @staticmethod
    def _dfs_search_buffer(lanes: SortingLanes, spec: str) -> list[Fish]:
        """取 DFS 搜索窗口：总量小用全量，否则每路只取队头 DFS_WINDOW_PER_BUCKET 条。"""
        q = lanes.queues[spec]
        total = lanes.total_in_spec(spec)
        if total <= DFS_MAX_BUFFER:
            buf: list[Fish] = []
            for bucket in BUCKETS:
                buf.extend(q[bucket])
            return buf
        buf = []
        for bucket in BUCKETS:
            buf.extend(q[bucket][:DFS_WINDOW_PER_BUCKET])
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

    @staticmethod
    def _fifo_head_plan(lanes: SortingLanes, spec: str) -> BoxPlan | None:
        """FIFO 队头前缀枚举（O(尾数³×队深)，保证终止）。"""
        q = lanes.queues[spec]
        q_small, q_medium, q_large = q["small"], q["medium"], q["large"]
        p_small = prefix_weights(q_small)
        p_medium = prefix_weights(q_medium)
        p_large = prefix_weights(q_large)
        best: BoxPlan | None = None
        best_score = float("inf")
        for count in SPECS[spec]["counts"]:
            for a in range(min(len(q_small), count) + 1):
                for b in range(min(len(q_medium), count - a) + 1):
                    c = count - a - b
                    if c > len(q_large):
                        continue
                    weight = p_small[a] + p_medium[b] + p_large[c]
                    if not (TARGET_MIN <= weight <= TARGET_MAX):
                        continue
                    score = abs(weight - TARGET_MID)
                    if a == 0 or b == 0 or c == 0:
                        score += 1.2
                    if score < best_score:
                        best_score = score
                        best = BoxPlan(
                            spec=spec,
                            count=count,
                            weight=weight,
                            parts={"small": a, "medium": b, "large": c},
                            pick_ids=None,
                        )
        return best

    def find_plan(self, lanes: SortingLanes, spec: str) -> BoxPlan | None:
        """DFS 窗口搜索；无解/过大时回退 FIFO 队头。前端：封箱触发点。"""
        if spec not in SPECS:
            return None
        if lanes.total_in_spec(spec) < min(SPECS[spec]["counts"]):
            return None

        buffer = self._dfs_search_buffer(lanes, spec)
        result = dfs_find_best_from_items(buffer, spec, max_buffer=DFS_MAX_BUFFER + 3)
        if result:
            indices, count, weight = result
            return self._plan_from_indices(buffer, spec, indices, count, weight)

        return self._fifo_head_plan(lanes, spec)


UI_BUCKET = {"small": "light", "medium": "mid", "large": "heavy"}


def module_of_spec(spec: str) -> str:
    """作用：将规格映射到模块 A/B/C。
    前端：fifo_monitor.html 画布三模块分区与需求地址前缀（A/15p/light）。"""
    for mod, spec_list in MODULE_SPECS.items():
        if spec in spec_list:
            return mod
    return "A"


def lane_address(mod: str, spec: str, bucket: str) -> str:
    """作用：生成料道需求地址字符串（如 B/50p/mid）。
    前端：fifo_monitor「进料口广播」「需求地址列表」中的 address 字段。"""
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
    ):
        """作用：初始化引擎（批次、料道、追踪器、统计）；POST /api/start 时创建。
        前端：index/fifo_monitor「开始模拟」；控制种子、条数/总重、超时、启用规格、料道容量倍率。"""
        self.seed = seed
        self.interval = interval
        self.specs = specs
        self.move_timeout = move_timeout
        self.cap_factor = cap_factor
        self.verbose = verbose
        self.log_every = log_every
        self.stop_mode = stop_mode if stop_mode in (STOP_MODE_COUNT, STOP_MODE_WEIGHT) else STOP_MODE_COUNT
        self.stop_count = max(1, stop_count)
        self.stop_weight_g = max(1, stop_weight_g)

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

    def _sync_tick(self) -> int:
        """作用：用真实经过时间（秒）更新 tick，与入料条数解耦；暂停期间冻结。
        前端：GET /api/state.tick；fifo_monitor 需求广播「后端 t=」；超时回流阈值计时。"""
        now = time.monotonic()
        if self._paused_at is not None:
            now = self._paused_at
        self.tick = int(max(0, now - self._time_origin))
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

    def _lane_demand_entry(self, mod_key: str, spec: str, bucket: str) -> dict:
        """作用：生成单路料道（模块/规格/小中大）的需求条目（优先级、缺鱼数、原因）。
        前端：fifo_monitor「需求地址列表」单张卡片；GET /api/state.demands 每路一条。"""
        ui_bucket = UI_BUCKET[bucket]
        address = lane_address(mod_key, spec, bucket)
        base = {
            "module": mod_key,
            "spec": spec,
            "bucket": ui_bucket,
            "address": address,
            "lane_id": f"{spec}_{ui_bucket}",
            "lane_bucket": bucket,
        }
        if spec not in self.specs:
            return {
                **base,
                "priority": 9,
                "count": 0,
                "target": "lane",
                "reason": "未启用",
                "active": False,
            }
        lane = self.lanes.queues[spec][bucket]
        diag = self._best_diagnostic_for_spec(spec)
        queued = len(lane)
        if diag["need_bucket"] == bucket:
            return {
                **base,
                "priority": 2 if diag["kind"] == "bad" else 3,
                "count": diag["need_count"] or 1,
                "target": "lane",
                "reason": diag["short"],
                "active": True,
            }
        if diag["kind"] == "bad" and diag["need_bucket"] is None and bucket == "medium":
            return {
                **base,
                "priority": 2,
                "count": diag["need_count"] or 1,
                "target": "lane",
                "reason": "不足",
                "active": True,
            }
        if diag["kind"] == "good":
            return {
                **base,
                "priority": 4 if queued else 5,
                "count": queued,
                "target": "lane",
                "reason": f"料道{queued}条" if queued else "可装",
                "active": queued > 0,
            }
        return {
            **base,
            "priority": 4 if queued else 5,
            "count": queued,
            "target": "lane",
            "reason": f"料道{queued}条" if queued else "监控",
            "active": False,
        }

    def collect_demands(self) -> list[dict]:
        """作用：汇总全部 54 路料道需求（3 模块 × 18 规格 × 小中大），按优先级排序。
        前端：fifo_monitor「进料口广播」「需求地址列表」「活跃需求」计数；GET /api/state.demands。"""
        items: list[dict] = []
        for mod_key, spec_list in MODULE_SPECS.items():
            for spec in spec_list:
                for bucket in BUCKETS:
                    items.append(self._lane_demand_entry(mod_key, spec, bucket))
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
                        "enabled": enabled,
                    }
                    continue
                q = self.lanes.queues[spec]
                cap = lane_capacity(spec, self.cap_factor)
                modules[mod][spec] = {
                    "small": len(q["small"]),
                    "medium": len(q["medium"]),
                    "large": len(q["large"]),
                    "total": self.lanes.total_in_spec(spec),
                    "capacity": cap,
                    "enabled": enabled,
                }
        rounds_dist: dict[str, int] = {}
        for t in self.tracker.traces.values():
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
        return {
            "tick": self.tick,
            "finished": self.finished,
            "seed": self.seed,
            "move_timeout": self.move_timeout,
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

    def _try_pack_all(self) -> int:
        """作用：遍历启用规格尝试 DFS 封箱，成功则移除料道鱼并累加统计。
        前端：两页「完成箱数」；fifo_monitor 装箱工位；index 成盒数据与趋势图。"""
        packed = 0
        for spec in self.specs:
            while True:
                plan = self.planner.find_plan(self.lanes, spec)
                if plan is None:
                    break
                self.lanes.remove_plan(plan, self.tick, self.tracker)
                self.stats.cartons += 1
                self.stats.packed_fish += plan.count
                self.cartons.append(plan)
                packed += 1
                parts = " + ".join(
                    f"{BUCKET_LABEL[b]}{plan.parts[b]}" for b in BUCKETS if plan.parts[b]
                )
                self._log(
                    f"封箱 #{self.stats.cartons:04d}: {spec.upper()} "
                    f"{plan.count}尾 {plan.weight}g ({parts})",
                    kind="pack",
                    spec=spec,
                    weight=plan.weight,
                    count=plan.count,
                )
        return packed

    def _process_reflow_intake(self) -> None:
        """作用：每步将回流队列中的鱼尝试重新放入料道。
        前端：GET /api/state.reflow_queue 待入库数；fifo_monitor 回流鱼二次入道。"""
        remaining: list[Fish] = []
        for fish in self.lanes.reflow:
            if not self.lanes.try_enqueue_reflow(fish, self.tick, self.tracker):
                remaining.append(fish)
        self.lanes.reflow = remaining

    def _anti_block(self) -> None:
        """作用：防堵逻辑——料道超容则弹出队头鱼回流；队首超时则直接记为尾料。
        前端：两页「回流/尾料」分项；fifo_monitor 日志；GET /api/state 超时/超容日志。"""
        for spec in self.specs:
            if self.planner.find_plan(self.lanes, spec):
                continue
            cap = lane_capacity(spec, self.cap_factor)
            for bucket in BUCKETS:
                lane = self.lanes.queues[spec][bucket]
                if not lane:
                    continue
                if len(lane) > cap:
                    lane_len = len(lane)
                    fish = self.lanes.divert_head(spec, bucket, self.tick, "overflow", self.tracker)
                    if fish:
                        self.stats.reflow_count += 1
                        self.stats.overflow_reflow += 1
                        self.overflow_reflow_log.append(
                            {
                                "tick": self.tick,
                                "fish_id": fish.id,
                                "weight": fish.weight,
                                "spec": spec,
                                "bucket": bucket,
                                "lane_len": lane_len,
                                "cap": cap,
                                "rounds": fish.rounds,
                            }
                        )
                        self._log(
                            f"回流: #{fish.id} {spec.upper()}-{BUCKET_LABEL[bucket]} "
                            f"超容 → 第{fish.rounds}轮",
                            kind="reflow",
                            reason="overflow",
                            fish_id=fish.id,
                            rounds=fish.rounds,
                        )
                    return
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
        return self._cursor >= self.stop_count

    def process_one(self) -> bool:
        """作用：推进仿真一步——入料→回流再入道→封箱→防堵；达到结束条件返回 False。
        前端：web_server 后台线程按 speed 调用；两页 input_count；fifo_monitor 逐条 spawn。"""
        if self._cursor >= self.total_fish or self._intake_complete():
            return False

        self._sync_tick()
        record = self.batch[self._cursor]
        self._cursor += 1
        self.stats.input_count += 1
        self.stats.input_weight += record.weight

        # 按重量映射规格/分区；规格外或禁用规格 → outside 队列，否则入对应料道
        fish = record_to_fish(record, self.tick, enabled=set(self.specs))
        if record.outside or fish.spec is None:
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
            bucket = self.lanes.enqueue(fish, self.tick, self.tracker)
            self._event(
                "intake",
                f"入料 #{fish.id} {fish.spec.upper()} {fish.weight}g → {BUCKET_LABEL[bucket]}区",
                fish_id=fish.id,
                spec=fish.spec,
                weight=fish.weight,
                bucket=bucket,
                rounds=fish.rounds,
            )
            if self.verbose:
                self._log(
                    f"入料 #{fish.id} {fish.spec.upper()} {fish.weight}g "
                    f"→ {BUCKET_LABEL[bucket]}区 第{fish.rounds}轮"
                )

        # 回流待入库 → 料道；满足 5kg 方案则封箱；仍堵则超容或超时弹出队头
        self._process_reflow_intake()
        self._try_pack_all()
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
            self._log(
                f"进度 {progress} | "
                f"成盒 {self.stats.cartons} | 装箱鱼 {self.stats.packed_fish} | "
                f"回流 {self.stats.reflow_count} | 规格外 {self.stats.outside_count}",
                force=True,
            )
        return not self._intake_complete()

    def finish_batch(self) -> None:
        """作用：批末扫尾（继续封箱+回流）→标记尾料→写 CSV 报告→finished=true。
        前端：两页批末状态；index「成盒/尾料/报告」；fifo_monitor 快速跑完与尾料箱最终数。"""
        for _ in range(5000):
            self._sync_tick()
            before = self.stats.cartons
            self._process_reflow_intake()
            self._try_pack_all()
            if self.stats.cartons == before and not self.lanes.reflow:
                break
            self._anti_block()

        for spec in self.specs:
            for bucket in BUCKETS:
                for fish in self.lanes.queues[spec][bucket]:
                    self.tracker.mark_unmatched(fish, "unmatched_tail", tick=self.tick)
                    self.stats.tail_count += 1
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
        print(f"  入料总数     : {self.stats.input_count}")
        print(f"  入料总重     : {self.stats.input_weight:,}g ({self.stats.input_weight / 1_000_000:.3f}t)")
        if self.stop_mode == STOP_MODE_WEIGHT:
            print(f"  结束条件     : 总重 ≥ {self.stop_weight_g / 1_000_000:.3f}t")
        else:
            print(f"  结束条件     : 条数 {self.stop_count}")
        print(f"  成功装盒数   : {self.stats.cartons}")
        print(f"  成功装箱鱼   : {self.stats.packed_fish}")
        print(f"  规格外       : {self.stats.outside_count}")
        print(f"  回流总次数   : {self.stats.reflow_count} (仅超容)")
        print(f"    超时尾料   : {self.stats.timeout_tail}")
        print(f"    超容回流   : {self.stats.overflow_reflow}")
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
        print(f"  移动超时   : {self.move_timeout}s (超出则直接记为尾料，不进回流)")
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
        help="料道移动超时秒数，超出回流",
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
    )
    engine.run(realtime=not args.fast)


if __name__ == "__main__":
    main()
