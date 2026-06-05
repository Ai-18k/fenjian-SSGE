#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project : kaomam_project
@File : Scheduler_Engine.py
@Author : 18k
@Date : 2026/6/1 13:35
@Description: 智能分拣引擎 — 使用随机种子批次，FIFO 分区段装盒，超时回流
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


FISH_CACHE=[]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_root = Path(__file__).resolve().parent.parent
_bucket_rules = _load_module("bucket_rules", _root / "plan" / "细分规则.py")
_seed_gen = _load_module("fish_seed_gen", _root / "plan" / "随机种子生成.py")

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
    id: int
    weight: int
    spec: str | None = None
    bucket: str | None = None
    enter_time: int = 0
    rounds: int = 1


@dataclass
class FishTrace:
    fish_id: int
    weight: int
    spec: str | None
    rounds: int = 1
    first_in_time: int | None = None
    outbound_time: int | None = None
    status: str = "pending"
    reflow_reasons: list[str] = field(default_factory=list)

    @property
    def dwell_time(self) -> int | None:
        if self.first_in_time is None:
            return None
        end = self.outbound_time if self.outbound_time is not None else self.first_in_time
        return end - self.first_in_time


@dataclass
class BoxPlan:
    spec: str
    count: int
    weight: int
    parts: dict[str, int]
    fish: list[Fish] = field(default_factory=list)


@dataclass
class Stats:
    input_count: int = 0
    packed_fish: int = 0
    cartons: int = 0
    outside_count: int = 0
    reflow_count: int = 0
    timeout_reflow: int = 0
    overflow_reflow: int = 0
    unmatched_count: int = 0
    tail_count: int = 0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def expand_spec_list(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """展开规格列表；兼容 query 中 enabled_specs=a,b,c 被解析成单元素的情况。"""
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
    if not enabled_specs:
        return DEFAULT_ENABLED_SPECS
    valid = tuple(s for s in expand_spec_list(enabled_specs) if s in SPECS)
    return valid or DEFAULT_ENABLED_SPECS


def classify_spec(
    weight: int,
    enabled: set[str] | None = None,
) -> str | None:
    for name, info in SPECS.items():
        lo, hi = info["range"]
        if lo <= weight <= hi:
            if enabled is None or name in enabled:
                return name
            return None
    return None


def enabled_specs_tag(enabled_specs: tuple[str, ...]) -> str:
    return "-".join(enabled_specs)


def batch_csv_path(seed: int, enabled_specs: tuple[str, ...]) -> Path:
    tag = enabled_specs_tag(enabled_specs)
    return _root / "data" / f"fish_seed_{seed}_en_{tag}.csv"


def _batch_valid_for_enabled(
    records: list,
    enabled_specs: tuple[str, ...],
) -> bool:
    """批次须仅含：启用规格内的鱼 + 真实规格外（<65 或 >700g）。"""
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
    return _bucket_rules.bucket_of(weight, BUCKET_RANGES[spec])


def prefix_weights(fish_list: list[Fish]) -> list[int]:
    p = [0]
    for f in fish_list:
        p.append(p[-1] + f.weight)
    return p


def lane_capacity(spec: str, cap_factor: int = DEFAULT_CAP_FACTOR) -> int:
    return math.ceil(max(SPECS[spec]["counts"]) * cap_factor / 3)


def record_to_fish(
    record,
    tick: int,
    enabled: set[str] | None = None,
) -> Fish:
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
    """按启用规格生成/加载批次：只在启用重量段内随机，约 1% 为 <65 或 >700g 真规格外。"""
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
}


def carton_fish_detail(plan: BoxPlan) -> list[dict]:
    return [
        {
            "id": f.id,
            "weight": f.weight,
            "bucket": f.bucket or "",
        }
        for f in plan.fish
    ]


def describe_tail_trace(trace: FishTrace, end_tick: int | None = None) -> dict:
    """尾料未匹配原因：批末未配盒 / 回流未再入盒 / 规格外，及是否曾超时、超容回流。"""
    reasons = list(trace.reflow_reasons)
    had_timeout = "timeout" in reasons
    had_overflow = "overflow" in reasons
    status = trace.status or ""
    tail_cause = TAIL_STATUS_LABEL.get(status, status or "未知")

    reflow_parts: list[str] = []
    if had_timeout:
        reflow_parts.append("超时回流")
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
    }


# ---------------------------------------------------------------------------
# 追踪器
# ---------------------------------------------------------------------------
class FishTracker:
    def __init__(self) -> None:
        self.traces: dict[int, FishTrace] = {}
        self.unmatched: list[FishTrace] = []

    def register(self, fish: Fish, tick: int, status: str = "queued") -> None:
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
        trace = self.traces[fish.id]
        trace.rounds = fish.rounds
        trace.outbound_time = tick
        trace.status = "packed"

    def mark_reflow(self, fish: Fish, tick: int, reason: str) -> None:
        trace = self.traces.get(fish.id)
        if trace is None:
            self.register(fish, tick, status="reflow")
            trace = self.traces[fish.id]
        trace.rounds = fish.rounds
        trace.status = "reflow"
        trace.reflow_reasons.append(reason)

    def mark_unmatched(self, fish: Fish, status: str, tick: int | None = None) -> None:
        trace = self.traces.get(fish.id)
        if trace is None:
            self.register(fish, tick or 0, status=status)
            trace = self.traces[fish.id]
        trace.rounds = fish.rounds
        trace.status = status
        if tick is not None:
            trace.outbound_time = tick
        self.unmatched.append(trace)

    def save_report(self, path: Path) -> None:
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

    def remaining_records(self, end_tick: int | None = None) -> list[dict]:
        return [
            {
                "fish_id": t.fish_id,
                "weight": t.weight,
                "spec": t.spec or "",
                "rounds": t.rounds,
                "status": t.status,
                "reflow_reasons": list(t.reflow_reasons),
                **describe_tail_trace(t, end_tick),
            }
            for t in sorted(self.unmatched, key=lambda x: x.fish_id)
        ]


# ---------------------------------------------------------------------------
# 料道 & 装箱
# ---------------------------------------------------------------------------
class SortingLanes:
    def __init__(self, specs: tuple[str, ...] = DEMO_SPECS):
        self.specs = specs
        self.queues: dict[str, dict[str, list[Fish]]] = {
            spec: {b: [] for b in BUCKETS} for spec in specs
        }
        self.outside: list[Fish] = []
        self.reflow: list[Fish] = []

    def _put_in_lane(self, fish: Fish, tick: int) -> str:
        fish.enter_time = tick
        self.queues[fish.spec][fish.bucket].append(fish)
        return fish.bucket

    def enqueue(self, fish: Fish, tick: int, tracker: FishTracker) -> str:
        if fish.spec is None or fish.spec not in self.queues:
            fish.enter_time = tick
            self.outside.append(fish)
            tracker.register(fish, tick, status="unmatched_outside")
            return "outside"
        bucket = self._put_in_lane(fish, tick)
        tracker.register(fish, tick, status="queued")
        return bucket

    def try_enqueue_reflow(self, fish: Fish, tick: int, tracker: FishTracker) -> bool:
        if fish.spec is None or fish.spec not in self.queues:
            return False
        lane = self.queues[fish.spec][fish.bucket]
        if len(lane) >= lane_capacity(fish.spec):
            return False
        self._put_in_lane(fish, tick)
        tracker.register(fish, tick, status="queued")
        return True

    def total_in_spec(self, spec: str) -> int:
        return sum(len(self.queues[spec][b]) for b in BUCKETS)

    def remove_plan(self, plan: BoxPlan, tick: int, tracker: FishTracker) -> list[Fish]:
        removed: list[Fish] = []
        for bucket in BUCKETS:
            n = plan.parts[bucket]
            if n:
                chunk = self.queues[plan.spec][bucket][:n]
                del self.queues[plan.spec][bucket][:n]
                removed.extend(chunk)
        for fish in removed:
            tracker.mark_packed(fish, tick)
        plan.fish = removed
        return removed

    def divert_head(self, spec: str, bucket: str, tick: int, reason: str, tracker: FishTracker) -> Fish | None:
        lane = self.queues[spec][bucket]
        if not lane:
            return None
        fish = lane.pop(0)
        fish.rounds += 1
        fish.enter_time = tick
        self.reflow.append(fish)
        tracker.mark_reflow(fish, tick, reason)
        return fish

    def iter_lanes(self):
        for spec in self.specs:
            for bucket in BUCKETS:
                yield spec, bucket, self.queues[spec][bucket]


class BoxPlanner:
    def find_plan(self, lanes: SortingLanes, spec: str) -> BoxPlan | None:
        q = lanes.queues[spec]
        if lanes.total_in_spec(spec) < min(SPECS[spec]["counts"]):
            return None

        p_small = prefix_weights(q["small"])
        p_medium = prefix_weights(q["medium"])
        p_large = prefix_weights(q["large"])
        best: BoxPlan | None = None
        best_score = float("inf")

        for count in SPECS[spec]["counts"]:
            for a in range(min(len(q["small"]), count) + 1):
                for b in range(min(len(q["medium"]), count - a) + 1):
                    c = count - a - b
                    if c > len(q["large"]):
                        continue
                    weight = p_small[a] + p_medium[b] + p_large[c]
                    if not (TARGET_MIN <= weight <= TARGET_MAX):
                        continue
                    score = abs(weight - TARGET_MID)
                    if a == 0 or b == 0 or c == 0:
                        score += 1.2
                    if score < best_score:
                        best = BoxPlan(
                            spec=spec,
                            count=count,
                            weight=weight,
                            parts={"small": a, "medium": b, "large": c},
                        )
                        best_score = score
        return best


# ---------------------------------------------------------------------------
# 调度引擎
# ---------------------------------------------------------------------------
class SchedulerEngine:
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
    ):
        self.seed = seed
        self.interval = interval
        self.specs = specs
        self.move_timeout = move_timeout
        self.cap_factor = cap_factor
        self.verbose = verbose
        self.log_every = log_every

        self.batch = batch_records or load_or_generate_batch(seed=seed)
        self.total_fish = len(self.batch)
        self._cursor = 0

        self.lanes = SortingLanes(specs=specs)
        self.planner = BoxPlanner()
        self.tracker = FishTracker()
        self.stats = Stats()
        self.cartons: list[BoxPlan] = []
        self.tick = 0
        self.finished = False
        self.events: list[dict] = []
        self.history: list[dict] = []
        self.timeout_reflow_log: list[dict] = []

    def _event(self, kind: str, msg: str, **extra) -> None:
        evt = {"tick": self.tick, "kind": kind, "msg": msg, **extra}
        self.events.append(evt)
        if len(self.events) > 300:
            self.events.pop(0)

    def get_snapshot(self) -> dict:
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
        carton_records = [
            {
                "seq": idx + 1,
                "spec": c.spec,
                "count": c.count,
                "weight": c.weight,
                "parts": dict(c.parts),
                "fish_ids": [f.id for f in c.fish],
                "fish_weights": [f.weight for f in c.fish],
                "fish": carton_fish_detail(c),
            }
            for idx, c in enumerate(self.cartons)
        ]
        remaining_fish = (
            self.tracker.remaining_records(end_tick=self.tick) if self.finished else []
        )
        return {
            "tick": self.tick,
            "finished": self.finished,
            "seed": self.seed,
            "move_timeout": self.move_timeout,
            "enabled_specs": list(self.specs),
            "total_fish": self.total_fish,
            "input_count": self.stats.input_count,
            "cartons": self.stats.cartons,
            "packed_fish": self.stats.packed_fish,
            "outside_count": self.stats.outside_count,
            "reflow_count": self.stats.reflow_count,
            "timeout_reflow": self.stats.timeout_reflow,
            "overflow_reflow": self.stats.overflow_reflow,
            "unmatched_count": self.stats.unmatched_count,
            "tail_count": self.stats.tail_count,
            "reflow_queue": len(self.lanes.reflow),
            "outside_queue": len(self.lanes.outside),
            "modules": modules,
            "recent_cartons": recent_cartons,
            "carton_records": carton_records,
            "remaining_fish": remaining_fish,
            "remaining_count": len(remaining_fish),
            "events": self.events[-40:],
            "timeout_reflow_log": self.timeout_reflow_log,
            "history": self.history[-120:],
            "rounds_top": dict(sorted(rounds_dist.items(), key=lambda x: int(x[0]))[:12]),
            "target": {"min": TARGET_MIN, "max": TARGET_MAX},
        }

    def _log(self, msg: str, force: bool = False, kind: str = "info", **extra) -> None:
        if kind != "info" or force:
            self._event(kind, msg, **extra)
        if self.verbose or force:
            print(f"[t={self.tick:05d}s] {msg}")

    def _try_pack_all(self) -> int:
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
        remaining: list[Fish] = []
        for fish in self.lanes.reflow:
            if not self.lanes.try_enqueue_reflow(fish, self.tick, self.tracker):
                remaining.append(fish)
        self.lanes.reflow = remaining

    def _anti_block(self) -> None:
        for spec in self.specs:
            if self.planner.find_plan(self.lanes, spec):
                continue
            cap = lane_capacity(spec, self.cap_factor)
            for bucket in BUCKETS:
                lane = self.lanes.queues[spec][bucket]
                if not lane:
                    continue
                if len(lane) > cap:
                    fish = self.lanes.divert_head(spec, bucket, self.tick, "overflow", self.tracker)
                    if fish:
                        self.stats.reflow_count += 1
                        self.stats.overflow_reflow += 1
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
                    fish = self.lanes.divert_head(spec, bucket, self.tick, "timeout", self.tracker)
                    if fish:
                        self.stats.reflow_count += 1
                        self.stats.timeout_reflow += 1
                        self.timeout_reflow_log.append(
                            {
                                "tick": self.tick,
                                "fish_id": fish.id,
                                "weight": fish.weight,
                                "spec": spec,
                                "bucket": bucket,
                                "dwell_s": dwell,
                                "threshold_s": self.move_timeout,
                                "rounds": fish.rounds,
                            }
                        )
                        self._log(
                            f"回流: #{fish.id} {spec.upper()}-{BUCKET_LABEL[bucket]} "
                            f"超时 {dwell}s(阈值{self.move_timeout}s) → 第{fish.rounds}轮",
                            kind="reflow",
                            reason="timeout",
                            fish_id=fish.id,
                            rounds=fish.rounds,
                            dwell_s=dwell,
                            threshold_s=self.move_timeout,
                        )
                    return

    def _record_history(self) -> None:
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
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "fish_id",
                    "weight",
                    "spec",
                    "rounds",
                    "status",
                    "tail_cause",
                    "reflow_summary",
                    "had_timeout",
                    "had_overflow",
                    "dwell_time",
                    "reflow_reasons",
                ],
            )
            writer.writeheader()
            for row in self.tracker.remaining_records(end_tick=self.tick):
                writer.writerow(
                    {
                        **row,
                        "had_timeout": int(row["had_timeout"]),
                        "had_overflow": int(row["had_overflow"]),
                        "dwell_time": row["dwell_time"] if row["dwell_time"] is not None else "",
                        "reflow_reasons": "|".join(row["reflow_reasons"]),
                    }
                )

    def process_one(self) -> bool:
        if self._cursor >= self.total_fish:
            return False

        self.tick += 1
        record = self.batch[self._cursor]
        self._cursor += 1
        self.stats.input_count += 1

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

        self._process_reflow_intake()
        self._try_pack_all()
        self._anti_block()
        self._record_history()

        if self.stats.input_count % self.log_every == 0:
            self._log(
                f"进度 {self.stats.input_count}/{self.total_fish} | "
                f"成盒 {self.stats.cartons} | 装箱鱼 {self.stats.packed_fish} | "
                f"回流 {self.stats.reflow_count} | 规格外 {self.stats.outside_count}",
                force=True,
            )
        return True

    def finish_batch(self) -> None:
        for _ in range(5000):
            before = self.stats.cartons
            self._process_reflow_intake()
            self._try_pack_all()
            if self.stats.cartons == before and not self.lanes.reflow:
                break
            self._anti_block()
            self.tick += 1

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
        self.tracker.save_report(report_path)
        self._save_cartons_csv(cartons_path)
        self._save_remaining_csv(remaining_path)
        self._event(
            "done",
            "批处理完成，报告已保存",
            report=str(report_path),
            cartons=str(cartons_path),
            remaining=str(remaining_path),
        )

    def print_report(self) -> None:
        rounds_dist: dict[int, int] = {}
        for t in self.tracker.traces.values():
            rounds_dist[t.rounds] = rounds_dist.get(t.rounds, 0) + 1
        report_path = _root / "data" / f"run_report_seed_{self.seed}.csv"

        print("\n" + "=" * 60)
        print("智能分拣汇总")
        print("=" * 60)
        print(f"  批次种子     : {self.seed}")
        print(f"  入料总数     : {self.stats.input_count}")
        print(f"  成功装盒数   : {self.stats.cartons}")
        print(f"  成功装箱鱼   : {self.stats.packed_fish}")
        print(f"  规格外       : {self.stats.outside_count}")
        print(f"  回流总次数   : {self.stats.reflow_count}")
        print(f"    超时回流   : {self.stats.timeout_reflow}")
        print(f"    超容回流   : {self.stats.overflow_reflow}")
        print(f"  未匹配/尾料  : {self.stats.unmatched_count}")
        print(f"    批末尾料   : {self.stats.tail_count}")
        print(f"  模拟时长     : {self.tick}s")
        if self.cartons:
            weights = [c.weight for c in self.cartons]
            print(f"  盒重范围     : {min(weights)}g ~ {max(weights)}g")
            print(f"  盒重均值     : {sum(weights) / len(weights):.0f}g")
        print("  轮数分布     :", dict(sorted(rounds_dist.items())))
        print(f"  明细报告     : {report_path}")
        print("=" * 60)

    def run(self, realtime: bool = True) -> None:
        print("智能分拣引擎启动")
        print(f"  批次       : fish_seed_{self.seed}.csv ({self.total_fish} 条)")
        print(f"  分拣规格   : {len(self.specs)} 个 ({', '.join(s.upper() for s in self.specs[:3])} …)")
        print(f"  模块批次   : A/B/C 共 {len(MODULE_SPECS)} 组")
        print(f"  盒重目标   : {TARGET_MIN}-{TARGET_MAX}g")
        print(f"  移动超时   : {self.move_timeout}s (超出则回流重新入库)")
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
