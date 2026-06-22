#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project : kaomam_project
@File : 计算需求.py
@Author : 18k
@Date : 2026/6/1 17:50
@Description: 18 规格装箱动态需求计算 — 根据当前箱内状态计算下一条可进重量，并判断 incoming 鱼是否可行
"""

from __future__ import annotations

from dataclasses import dataclass, field

TARGET_MIN = 4980   # 盒重下限（克）
TARGET_MAX = 5030   # 盒重上限（克）
TARGET_MID = 5005   # 盒重中心值（克）

# SPECS：各规格的重量区间与合法装箱尾数
SPECS: dict[str, dict] = {
    "15p": {"range": (566, 700), "counts": (7, 8，9)},
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


@dataclass
class DemandOption:
    """某一种目标尾数下的装箱方案需求。"""

    target_count: int                              # 目标装箱尾数（如 7 或 8）
    remaining_count: int                           # 还需几条鱼
    remaining_weight: tuple[int, int]              # 剩余总重需落在的区间 (min, max)
    next_fish_ranges: list[tuple[int, int]]        # 下一条鱼可接受的克重区间列表

    def to_dict(self) -> dict:
        """转为可序列化字典。"""
        return {
            "target_count": self.target_count,
            "remaining_count": self.remaining_count,
            "remaining_weight": list(self.remaining_weight),
            "next_fish_ranges": [list(r) for r in self.next_fish_ranges],
        }


@dataclass
class SpecDemand:
    """单规格当前的装箱需求状态。"""

    spec: str                                          # 规格名
    current_count: int                                 # 当前已进鱼条数
    current_weight: int                                # 当前已进鱼总重（克）
    options: list[DemandOption] = field(default_factory=list)       # 各目标尾数方案
    next_fish_ranges: list[tuple[int, int]] = field(default_factory=list)  # 合并后的下一条需求区间
    complete: bool = False                             # 是否已达标可封箱
    meets_requirement: bool = False                    # 是否满足盒重+尾数要求
    message: str = ""                                  # 人类可读状态描述

    def to_dict(self) -> dict:
        """转为可序列化字典。"""
        return {
            "current_count": self.current_count,
            "current_weight": self.current_weight,
            "complete": self.complete,
            "meets_requirement": self.meets_requirement,
            "message": self.message,
            "options": [o.to_dict() for o in self.options],
            "next_fish_ranges": [list(r) for r in self.next_fish_ranges],
        }


@dataclass
class FishCheckResult:
    """单条 incoming 鱼对某一规格箱的判定结果。"""

    spec: str                                      # 规格名
    fish_weight: int                               # 待判定鱼的克重
    acceptable: bool                               # 是否可进
    next_fish_ranges: list[tuple[int, int]]        # 进鱼前的下一条需求区间
    before_count: int                              # 进鱼前箱内条数
    before_weight: int                             # 进鱼前箱内总重
    after_count: int                               # 进鱼后箱内条数
    after_weight: int                              # 进鱼后箱内总重
    meets_requirement: bool                        # 进鱼后是否达标
    message: str                                   # 判定说明

    def to_dict(self) -> dict:
        """转为可序列化字典。"""
        return {
            "fish_weight": self.fish_weight,
            "acceptable": self.acceptable,
            "next_fish_ranges": [list(r) for r in self.next_fish_ranges],
            "before_count": self.before_count,
            "before_weight": self.before_weight,
            "after_count": self.after_count,
            "after_weight": self.after_weight,
            "meets_requirement": self.meets_requirement,
            "message": self.message,
        }


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠的重量区间。"""
    if not intervals:
        return []
    sorted_iv = sorted(intervals)
    merged = [sorted_iv[0]]
    for lo, hi in sorted_iv[1:]:
        pl, ph = merged[-1]
        if lo <= ph + 1:
            merged[-1] = (pl, max(ph, hi))
        else:
            merged.append((lo, hi))
    return merged


def meets_box_requirement(
    count: int,
    weight: int,
    allowed_counts: tuple[int, ...],
    target_min: int = TARGET_MIN,
    target_max: int = TARGET_MAX,
) -> bool:
    """箱内尾数在允许范围且总重在目标区间，视为达标。"""
    return count in allowed_counts and target_min <= weight <= target_max


def intersect_interval(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int] | None:
    """计算两个闭区间的交集，无交集返回 None。"""
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    if lo > hi:
        return None
    return lo, hi


def next_fish_range_for_remaining(
    rem_min: int,
    rem_max: int,
    remaining_count: int,
    spec_lo: int,
    spec_hi: int,
) -> list[tuple[int, int]]:
    """
    计算「下一条鱼」可接受的重量区间。

    remaining_count: 还需几条鱼（含即将进来的这条）
    rem_min/rem_max: 剩余总重需落在的区间
    """
    if remaining_count <= 0:
        return []

    spec_range = (spec_lo, spec_hi)

    if remaining_count == 1:
        need = intersect_interval((rem_min, rem_max), spec_range)
        return [need] if need else []

    # 下一条 w，其余 (remaining_count-1) 条各在 [spec_lo, spec_hi]
    k = remaining_count - 1
    w_lo = max(spec_lo, rem_min - k * spec_hi)
    w_hi = min(spec_hi, rem_max - k * spec_lo)
    need = intersect_interval((w_lo, w_hi), spec_range)
    return [need] if need else []


class BoxDemandCalculator:
    """
    单箱需求计算器：给定规格 + 已进鱼重量，返回下一条鱼的可接受重量范围。

    属性:
        spec: 规格名
        weights: 已进鱼克重列表
        target_min/target_max: 盒重目标区间
        spec_lo/spec_hi: 规格单尾克重区间
        allowed_counts: 合法装箱尾数元组
    """

    def __init__(
        self,
        spec: str,
        weights: list[int],
        target_min: int = TARGET_MIN,
        target_max: int = TARGET_MAX,
    ):
        if spec not in SPECS:
            raise ValueError(f"未知规格: {spec}")
        self.spec = spec
        self.weights = list(weights)
        self.target_min = target_min
        self.target_max = target_max
        self.spec_lo, self.spec_hi = SPECS[spec]["range"]
        self.allowed_counts = SPECS[spec]["counts"]

    @property
    def current_count(self) -> int:
        """当前箱内鱼条数。"""
        return len(self.weights)

    @property
    def current_weight(self) -> int:
        """当前箱内鱼总重（克）。"""
        return sum(self.weights)

    def calc(self) -> SpecDemand:
        """计算当前箱的动态需求，返回 SpecDemand。"""
        total = self.current_weight
        count = self.current_count
        compliant = meets_box_requirement(
            count, total, self.allowed_counts, self.target_min, self.target_max
        )

        if count == 0:
            all_next: list[tuple[int, int]] = []
            for target_count in self.allowed_counts:
                ranges = next_fish_range_for_remaining(
                    self.target_min,
                    self.target_max,
                    target_count,
                    self.spec_lo,
                    self.spec_hi,
                )
                all_next.extend(ranges)
            merged = merge_intervals(all_next)
            msg = "空箱，等待首条鱼"
            if merged:
                msg += f"；首条可进 {format_ranges(merged)}"
            return SpecDemand(
                spec=self.spec,
                current_count=0,
                current_weight=0,
                next_fish_ranges=merged,
                meets_requirement=False,
                message=msg,
            )

        if compliant:
            return SpecDemand(
                spec=self.spec,
                current_count=count,
                current_weight=total,
                complete=True,
                meets_requirement=True,
                message=f"已达标：{count}尾 / {total}g",
            )

        if count >= max(self.allowed_counts):
            return SpecDemand(
                spec=self.spec,
                current_count=count,
                current_weight=total,
                meets_requirement=False,
                message=f"已超允许尾数 {max(self.allowed_counts)}，需调整",
            )

        options: list[DemandOption] = []
        all_next: list[tuple[int, int]] = []

        for target_count in self.allowed_counts:
            if target_count <= count:
                continue

            remaining = target_count - count
            rem_min = self.target_min - total
            rem_max = self.target_max - total

            ranges = next_fish_range_for_remaining(
                rem_min, rem_max, remaining, self.spec_lo, self.spec_hi
            )

            # 剩余总重超出规格可达范围时，仍记录方案供诊断
            if rem_max < self.spec_lo * remaining or rem_min > self.spec_hi * remaining:
                continue

            opt = DemandOption(
                target_count=target_count,
                remaining_count=remaining,
                remaining_weight=(rem_min, rem_max),
                next_fish_ranges=ranges,
            )
            options.append(opt)
            all_next.extend(ranges)

        merged = merge_intervals(all_next)
        msg = self._build_message(options, merged, count, total)

        return SpecDemand(
            spec=self.spec,
            current_count=count,
            current_weight=total,
            options=options,
            next_fish_ranges=merged,
            meets_requirement=False,
            message=msg,
        )

    def check_incoming_fish(self, weight: int) -> FishCheckResult:
        """
        基于当前箱内状态，判断 incoming 鱼是否可进。

        判定依据（满足其一即可）：
          1. 重量落在当前计算出的「下一条可进」区间内（已在 566-700 等规格范围内）
          2. 加入后箱内尾数达标且总重在 4980-5030g
        """
        before = self.calc()
        lo, hi = self.spec_lo, self.spec_hi

        if not (lo <= weight <= hi):
            return FishCheckResult(
                spec=self.spec,
                fish_weight=weight,
                acceptable=False,
                next_fish_ranges=before.next_fish_ranges,
                before_count=before.current_count,
                before_weight=before.current_weight,
                after_count=before.current_count,
                after_weight=before.current_weight,
                meets_requirement=before.meets_requirement,
                message=f"重量 {weight}g 不在规格范围 {lo}-{hi}g",
            )

        if before.complete:
            return FishCheckResult(
                spec=self.spec,
                fish_weight=weight,
                acceptable=False,
                next_fish_ranges=before.next_fish_ranges,
                before_count=before.current_count,
                before_weight=before.current_weight,
                after_count=before.current_count,
                after_weight=before.current_weight,
                meets_requirement=True,
                message="箱已达标，不可再进",
            )

        after = BoxDemandCalculator(self.spec, self.weights + [weight]).calc()
        in_demand = any(
            rlo <= weight <= rhi for rlo, rhi in before.next_fish_ranges
        )
        acceptable = after.meets_requirement or in_demand

        if after.meets_requirement:
            msg = f"加入后达标：{after.current_count}尾 / {after.current_weight}g"
        elif in_demand:
            msg = (
                f"可进：{weight}g 落在下一条需求 "
                f"{format_ranges(before.next_fish_ranges)}；"
                f"加入后 {after.current_count}尾 / {after.current_weight}g"
            )
        else:
            msg = (
                f"不可进：{weight}g 不在下一条需求 "
                f"{format_ranges(before.next_fish_ranges) or '无'}；"
                f"当前 {before.current_count}尾 / {before.current_weight}g"
            )

        return FishCheckResult(
            spec=self.spec,
            fish_weight=weight,
            acceptable=acceptable,
            next_fish_ranges=before.next_fish_ranges,
            before_count=before.current_count,
            before_weight=before.current_weight,
            after_count=after.current_count,
            after_weight=after.current_weight,
            meets_requirement=after.meets_requirement,
            message=msg,
        )

    def _build_message(
        self,
        options: list[DemandOption],
        merged: list[tuple[int, int]],
        count: int,
        total: int,
    ) -> str:
        """拼装未达标时的人类可读状态消息。"""
        cnt_range = f"{self.allowed_counts[0]}-{self.allowed_counts[-1]}"
        status = (
            f"未达标：{count}尾 / {total}g "
            f"(需 {cnt_range}尾, {self.target_min}-{self.target_max}g)"
        )
        if not options:
            return f"{status}；当前组合无法凑满目标盒重，需回流或换规格"
        parts = []
        for o in options:
            rs = "、".join(f"{a}-{b}g" for a, b in o.next_fish_ranges)
            parts.append(
                f"{o.target_count}尾方案(还需{o.remaining_count}条,剩余{o.remaining_weight[0]}-{o.remaining_weight[1]}g):下一条[{rs}]"
            )
        return f"{status} | " + " | ".join(parts)


class DemandEngine:
    """18 规格需求引擎：维护各规格当前装箱状态，实时输出动态需求。"""

    def __init__(self) -> None:
        # boxes：各规格当前箱内鱼克重列表 {spec: [weight, ...]}
        self.boxes: dict[str, list[int]] = {spec: [] for spec in SPECS}

    def reset(self, spec: str | None = None) -> None:
        """重置指定规格或全部规格的箱内状态。"""
        if spec:
            self.boxes[spec] = []
        else:
            for s in SPECS:
                self.boxes[s] = []

    def check_fish(self, fish_spec: str, weight: int) -> dict[str, FishCheckResult]:
        """incoming 鱼进入前，18 个箱子分别判定（仅同规格箱按动态需求计算）。"""
        results: dict[str, FishCheckResult] = {}
        for spec in SPECS:
            if spec != fish_spec:
                d = self.calc_spec(spec)
                results[spec] = FishCheckResult(
                    spec=spec,
                    fish_weight=weight,
                    acceptable=False,
                    next_fish_ranges=d.next_fish_ranges,
                    before_count=d.current_count,
                    before_weight=d.current_weight,
                    after_count=d.current_count,
                    after_weight=d.current_weight,
                    meets_requirement=d.meets_requirement,
                    message="规格不符",
                )
            else:
                results[spec] = BoxDemandCalculator(
                    spec, self.boxes[spec]
                ).check_incoming_fish(weight)
        return results

    def add_fish(self, spec: str, weight: int) -> dict[str, FishCheckResult]:
        """进一条鱼：先判定，写入对应箱，返回各箱判定结果（含进鱼前下一条需求）。"""
        if spec not in SPECS:
            raise ValueError(f"未知规格: {spec}")
        checks = self.check_fish(spec, weight)
        self.boxes[spec].append(weight)
        return checks

    def set_box(self, spec: str, weights: list[int]) -> SpecDemand:
        """直接设置某规格箱内鱼重量列表并返回需求。"""
        self.boxes[spec] = list(weights)
        return self.calc_spec(spec)

    def calc_spec(self, spec: str) -> SpecDemand:
        """计算单规格的当前需求。"""
        return BoxDemandCalculator(spec, self.boxes[spec]).calc()

    def calc_all(self) -> dict[str, dict]:
        """返回 18 规格各自的当前状态与是否达标。"""
        return {spec: self.calc_spec(spec).to_dict() for spec in SPECS}

    def calc_all_active(self) -> dict[str, list[list[int]]]:
        """
        简化输出：{规格: 下一条可进重量区间列表}
        例: {"15p": [[566, 610]], "20p": [[470, 480]]}
        """
        out = {}
        for spec in SPECS:
            d = self.calc_spec(spec)
            if d.next_fish_ranges:
                out[spec] = [list(r) for r in d.next_fish_ranges]
        return out

    def accept_fish(self, spec: str, weight: int) -> tuple[bool, FishCheckResult]:
        """判断 incoming 鱼是否符合当前下一条需求，符合则加入箱内。"""
        check = BoxDemandCalculator(spec, self.boxes[spec]).check_incoming_fish(weight)
        if check.acceptable:
            self.boxes[spec].append(weight)
        return check.acceptable, check


def format_ranges(ranges: list[tuple[int, int]]) -> str:
    """将重量区间列表格式化为可读字符串，如 [566-610g, 620-700g]。"""
    return "[" + ", ".join(f"{lo}-{hi}g" for lo, hi in ranges) + "]"


def demo() -> None:
    """运行动态需求计算演示用例。"""
    print("=" * 64)
    print("装箱动态需求计算 Demo")
    print("=" * 64)

    # 文档示例：15p 已进 6 条 570g（剩余重 1560-1610g，但单条最大 700g 无法单独凑 7 尾）
    calc = BoxDemandCalculator("15p", [570] * 6)
    d = calc.calc()
    print(f"\n[示例1] 15P 已进 6 条 x 570g = {d.current_weight}g")
    print(f"  状态: {d.message}")
    print(f"  下一条可进: {format_ranges(d.next_fish_ranges) or '无'}")
    for o in d.options:
        print(
            f"    -> {o.target_count}尾: 还需 {o.remaining_count} 条, "
            f"剩余总重 {o.remaining_weight[0]}-{o.remaining_weight[1]}g, "
            f"下一条 {format_ranges(o.next_fish_ranges) or '无(超重/偏轻)'}"
        )

    # 可凑满的 6 条案例
    calc1b = BoxDemandCalculator("15p", [620] * 6)
    d1b = calc1b.calc()
    print(f"\n[示例1b] 15P 已进 6 条 x 620g = {d1b.current_weight}g")
    print(f"  下一条可进: {format_ranges(d1b.next_fish_ranges)}")

    # 15p 已进 5 条，混合重量（用户场景）
    weights_5 = [612, 650, 570, 569, 690]
    calc2 = BoxDemandCalculator("15p", weights_5)
    d2 = calc2.calc()
    print(f"\n[示例2] 15P 已进 5 条 {weights_5} = {d2.current_weight}g")
    print(f"  下一条可进: {format_ranges(d2.next_fish_ranges)}")
    for w in [650, 580, 720]:
        chk = calc2.check_incoming_fish(w)
        mark = "可进" if chk.acceptable else "不可"
        print(f"  incoming {w}g -> [{mark}] {chk.message}")

    # 15p 已进 5 条，均匀重量
    calc2b = BoxDemandCalculator("15p", [580, 590, 600, 610, 620])
    d2b = calc2b.calc()
    print(f"\n[示例2b] 15P 已进 5 条 x 均匀 = {d2b.current_weight}g")
    print(f"  下一条可进: {format_ranges(d2b.next_fish_ranges)}")

    # 20p 示例
    calc3 = BoxDemandCalculator("20p", [470] * 8)
    d3 = calc3.calc()
    print(f"\n[示例3] 20P 已进 8 条 x 470g = {d3.current_weight}g")
    print(f"  下一条可进: {format_ranges(d3.next_fish_ranges)}")

    # 18 规格引擎
    print("\n[示例4] DemandEngine 多规格并行")
    engine = DemandEngine()
    engine.set_box("15p", [570] * 6)
    engine.set_box("20p", [480] * 9)
    engine.set_box("25p", [400] * 11)
    active = engine.calc_all_active()
    for spec, ranges in active.items():
        print(f"  {spec.upper()}: {ranges}")

    print("\n[示例5] 进一条鱼，先算下一条需求再判定")
    engine2 = DemandEngine()
    engine2.set_box("15p", [612, 650, 570, 569, 690])
    demand = engine2.calc_spec("15p")
    print(f"  当前 15P: {demand.current_count}尾 / {demand.current_weight}g")
    print(f"  下一条可进: {format_ranges(demand.next_fish_ranges)}")
    for w in [650, 580, 720]:
        ok, chk = engine2.accept_fish("15p", w)
        mark = "可进" if ok else "不可"
        print(f"  incoming {w}g -> [{mark}] {chk.message}")
        nd = engine2.calc_spec("15p")
        print(f"    进后状态: {nd.current_count}尾 / {nd.current_weight}g，下一条 {format_ranges(nd.next_fish_ranges) or '无'}")


if __name__ == "__main__":
    demo()
