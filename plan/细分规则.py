#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project : kaomam_project
@File : 细分规则.py
@Author : 18k
@Date : 2026/6/1 14:51
@Description: 15p 规格（566-700g）小/中/大三区段划分与 5kg 装箱计算
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# 566g至700g范围，这盒需要在7尾, 8尾，9尾且总质量要在5kg左右（4980g-5030g）
TARGET_MIN = 4980
TARGET_MAX = 5030
TARGET_MID = 5005

SPEC_15P = {
    "range": (566, 700),
    "counts": (7, 8, 9),
    "primary_count": 8,  # 中区锚定 8 尾 ≈ 5kg
}


@dataclass(frozen=True)
class BucketRange:
    small: tuple[int, int]
    medium: tuple[int, int]
    large: tuple[int, int]

    def as_dict(self) -> dict[str, tuple[int, int]]:
        return {"small": self.small, "medium": self.medium, "large": self.large}


BucketName = Literal["small", "medium", "large"]


def avg_weight_for_box(count: int, target: int = TARGET_MID) -> float:
    """给定尾数与目标总重，计算单尾理想均重。"""
    return target / count


def per_fish_range_for_target(
    count: int,
    target_min: int = TARGET_MIN,
    target_max: int = TARGET_MAX,
) -> tuple[float, float]:
    """给定尾数，计算单尾重量需落在哪个区间才能凑满目标盒重。"""
    return target_min / count, target_max / count


def calc_mid_bucket(
    spec_range: tuple[int, int],
    primary_count: int = SPEC_15P["primary_count"],
    target_min: int = TARGET_MIN,
    target_max: int = TARGET_MAX,
) -> tuple[int, int]:
    """
    以主力尾数（默认 8 尾）锚定中区：单尾落在 [target_min/count, target_max/count]。
    15p 下约为 622-628g，均重 625g。
    """
    lo, hi = spec_range
    mid_lo = max(lo, math.ceil(target_min / primary_count))
    mid_hi = min(hi, math.floor(target_max / primary_count))
    if mid_lo > mid_hi:
        raise ValueError(
            f"规格 {spec_range} 无法用 {primary_count} 尾纯中区凑满 "
            f"{target_min}-{target_max}g"
        )
    return mid_lo, mid_hi


def calc_bucket_split(
    spec_range: tuple[int, int] | None = None,
    primary_count: int = SPEC_15P["primary_count"],
    target_min: int = TARGET_MIN,
    target_max: int = TARGET_MAX,
) -> BucketRange:
    """
    将规格区间划分为小/中/大三段，中区保证 primary_count 尾总量落在目标区间。

    15p 推荐结果：
      小: 566-621g
      中: 622-628g  (8 尾 ≈ 4980-5024g)
      大: 629-700g
    """
    lo, hi = spec_range or SPEC_15P["range"]
    mid_lo, mid_hi = calc_mid_bucket((lo, hi), primary_count, target_min, target_max)
    return BucketRange(
        small=(lo, mid_lo - 1),
        medium=(mid_lo, mid_hi),
        large=(mid_hi + 1, hi),
    )


def bucket_of(weight: int, buckets: BucketRange | None = None) -> BucketName:
    """按重量归入小/中/大区段。"""
    buckets = buckets or calc_bucket_split()
    for name in ("small", "medium", "large"):
        lo, hi = getattr(buckets, name)
        if lo <= weight <= hi:
            return name
    raise ValueError(f"重量 {weight}g 不在任何区段内")


def box_weight_range(count: int, bucket: tuple[int, int]) -> tuple[int, int]:
    """同一区段、同一尾数下，盒重的最小值与最大值。"""
    return count * bucket[0], count * bucket[1]


def fits_target(total: int, target_min: int = TARGET_MIN, target_max: int = TARGET_MAX) -> bool:
    """盒重是否落在目标区间。"""
    return target_min <= total <= target_max


def verify_pure_bucket_boxes(
    buckets: BucketRange | None = None,
    counts: tuple[int, ...] = SPEC_15P["counts"],
    target_min: int = TARGET_MIN,
    target_max: int = TARGET_MAX,
) -> dict[tuple[int, str], dict]:
    """
    验证各尾数 × 纯区段装箱是否可达目标盒重。
    返回示例：{(8, 'medium'): {'min': 4976, 'max': 5024, 'ok': True}, ...}
    """
    buckets = buckets or calc_bucket_split()
    labels = {"small": buckets.small, "medium": buckets.medium, "large": buckets.large}
    result = {}
    for count in counts:
        for name, rng in labels.items():
            w_min, w_max = box_weight_range(count, rng)
            result[(count, name)] = {
                "min": w_min,
                "max": w_max,
                "ok": w_max >= target_min and w_min <= target_max,
            }
    return result


def format_bucket_report(buckets: BucketRange | None = None) -> str:
    """输出可读的区段划分与验证报告。"""
    buckets = buckets or calc_bucket_split()
    lines = [
        f"目标盒重: {TARGET_MIN}-{TARGET_MAX}g (中心 {TARGET_MID}g)",
        f"小: {buckets.small[0]}-{buckets.small[1]}g",
        f"中: {buckets.medium[0]}-{buckets.medium[1]}g "
        f"(均重 {(buckets.medium[0] + buckets.medium[1]) / 2:.0f}g, "
        f"{SPEC_15P['primary_count']}尾 ≈ "
        f"{box_weight_range(SPEC_15P['primary_count'], buckets.medium)[0]}-"
        f"{box_weight_range(SPEC_15P['primary_count'], buckets.medium)[1]}g)",
        f"大: {buckets.large[0]}-{buckets.large[1]}g",
        "",
        "纯区段装箱验证:",
    ]
    for (count, name), info in sorted(verify_pure_bucket_boxes(buckets).items()):
        mark = "OK" if info["ok"] else "NO"
        lines.append(f"  {count}尾-{name}: {info['min']}-{info['max']}g [{mark}]")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_bucket_report())
