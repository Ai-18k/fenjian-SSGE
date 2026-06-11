#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project : kaomam_project
@File : 随机种子生成.py
@Author : 18k
@Date : 2026/6/1
@Description: 按前端目标条数与启用规格重量区间随机生成批次，约 1% 为超规鱼（不在启用规格范围内）
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 成品规格表：重量范围 g（与 Scheduler_Engine.SPECS range 一致）
SPECS: dict[str, tuple[int, int]] = {
    "15p": (566, 700),
    "20p": (446, 565),
    "25p": (366, 445),
    "30p": (306, 365),
    "35p": (266, 305),
    "40p": (231, 265),
    "45p": (211, 230),
    "50p": (183, 210),
    "60p": (153, 182),
    "70p": (133, 152),
    "80p": (116, 132),
    "90p": (106, 115),
    "100p": (96, 105),
    "110p": (87, 95),
    "120p": (80, 86),
    "130p": (74, 79),
    "140p": (69, 73),
    "150p": (65, 68),
}

VALID_MIN = 65
VALID_MAX = 700
DEFAULT_TOTAL = 25000
DEFAULT_OUTSIDE_RATE = 0.01
DEFAULT_SEED = 42

SPEC_NAMES = list(SPECS.keys())


@dataclass
class FishRecord:
    id: int
    weight: int
    spec: str | None = None
    outside: bool = False


@dataclass
class BatchSummary:
    total: int = 0
    total_weight: int = 0
    outside_count: int = 0
    inside_count: int = 0
    spec_counts: dict[str, int] = field(default_factory=dict)
    seed: int = DEFAULT_SEED
    enabled_specs: list[str] = field(default_factory=list)

    @property
    def avg_weight(self) -> float:
        return self.total_weight / self.total if self.total else 0.0

    @property
    def outside_rate(self) -> float:
        return self.outside_count / self.total if self.total else 0.0


def normalize_enabled_specs(
    enabled_specs: tuple[str, ...] | list[str] | None,
) -> list[str]:
    """解析启用规格列表，默认全规格。"""
    if not enabled_specs:
        return list(SPEC_NAMES)
    active = [s for s in enabled_specs if s in SPECS]
    return active or list(SPEC_NAMES)


def enabled_ranges(enabled_specs: list[str]) -> list[tuple[int, int]]:
    return [SPECS[s] for s in enabled_specs if s in SPECS]


def classify_spec(
    weight: int,
    enabled_specs: list[str] | None = None,
) -> str | None:
    """在启用规格范围内按克重归类；未命中返回 None。"""
    for name in normalize_enabled_specs(enabled_specs):
        lo, hi = SPECS[name]
        if lo <= weight <= hi:
            return name
    return None


def is_outside_weight(
    weight: int,
    enabled_specs: tuple[str, ...] | list[str] | None = None,
) -> bool:
    """相对启用规格：不在任一启用重量区间内视为超规。"""
    return classify_spec(weight, normalize_enabled_specs(enabled_specs)) is None


def is_true_outside_weight(weight: int) -> bool:
    """全局 65–700g 之外（兼容旧批次校验）。"""
    return weight < VALID_MIN or weight > VALID_MAX


def _random_outside_weight(
    rng: random.Random,
    enabled_specs: list[str],
) -> int:
    """生成不在启用规格范围内的超规重量。"""
    ranges = sorted(enabled_ranges(enabled_specs))
    union_lo = min(lo for lo, _ in ranges)
    union_hi = max(hi for _, hi in ranges)

    strategies: list[str] = []
    if union_lo > 30:
        strategies.append("below")
    strategies.append("above")
    disabled = [s for s in SPEC_NAMES if s not in enabled_specs]
    if disabled:
        strategies.append("disabled_spec")

    pick = rng.choice(strategies)
    if pick == "below":
        return rng.randint(30, union_lo - 1)
    if pick == "above":
        return rng.randint(union_hi + 1, 850)
    spec = rng.choice(disabled)
    lo, hi = SPECS[spec]
    return rng.randint(lo, hi)


def _random_inside_weight(
    rng: random.Random,
    enabled_specs: list[str],
) -> tuple[int, str]:
    """在启用规格中随机选一档，再在其重量区间内取克重。"""
    spec = rng.choice(enabled_specs)
    lo, hi = SPECS[spec]
    return rng.randint(lo, hi), spec


def _finalize_batch_records(
    records: list[FishRecord],
    spec_counts: dict[str, int],
    seed: int,
    active_specs: list[str],
    rng: random.Random,
) -> tuple[list[FishRecord], BatchSummary]:
    rng.shuffle(records)
    for seq, fish in enumerate(records, start=1):
        fish.id = seq
    total = len(records)
    total_weight = sum(f.weight for f in records)
    actual_outside = sum(1 for f in records if f.outside)
    summary = BatchSummary(
        total=total,
        total_weight=total_weight,
        outside_count=actual_outside,
        inside_count=total - actual_outside,
        spec_counts={k: v for k, v in spec_counts.items() if v > 0},
        seed=seed,
        enabled_specs=active_specs,
    )
    return records, summary


def generate_fish_batch(
    total: int = DEFAULT_TOTAL,
    outside_rate: float = DEFAULT_OUTSIDE_RATE,
    seed: int = DEFAULT_SEED,
    enabled_specs: tuple[str, ...] | list[str] | None = None,
) -> tuple[list[FishRecord], BatchSummary]:
    """
    生成一批鱼的质量数据。

    - total: 目标条数（前端传入）
    - outside_rate: 超规比例，默认 1%
    - enabled_specs: 启用规格；规格内鱼仅在其重量区间内随机
    - seed: 随机种子，保证可复现
    """
    rng = random.Random(seed)
    active_specs = normalize_enabled_specs(enabled_specs)
    outside_count = round(total * outside_rate)
    inside_count = total - outside_count

    records: list[FishRecord] = []
    spec_counts: dict[str, int] = {s: 0 for s in active_specs}

    for i in range(1, total + 1):
        if i <= outside_count:
            weight = _random_outside_weight(rng, active_specs)
            records.append(FishRecord(id=i, weight=weight, spec=None, outside=True))
        else:
            weight, spec = _random_inside_weight(rng, active_specs)
            records.append(FishRecord(id=i, weight=weight, spec=spec, outside=False))
            spec_counts[spec] += 1

    return _finalize_batch_records(records, spec_counts, seed, active_specs, rng)


def generate_fish_batch_by_weight(
    target_weight_g: int,
    outside_rate: float = DEFAULT_OUTSIDE_RATE,
    seed: int = DEFAULT_SEED,
    enabled_specs: tuple[str, ...] | list[str] | None = None,
    max_fish: int = DEFAULT_TOTAL,
) -> tuple[list[FishRecord], BatchSummary]:
    """
    按目标总重（克）生成批次，累计重量达到 target_weight_g 后停止。

    - target_weight_g: 目标总重（克），如 10 吨 = 10_000_000
    - max_fish: 安全上限条数，防止异常配置无限生成
    """
    rng = random.Random(seed)
    active_specs = normalize_enabled_specs(enabled_specs)
    target_weight_g = max(1, int(target_weight_g))
    max_fish = max(1, int(max_fish))

    records: list[FishRecord] = []
    spec_counts: dict[str, int] = {s: 0 for s in active_specs}
    total_weight = 0

    while total_weight < target_weight_g and len(records) < max_fish:
        if rng.random() < outside_rate:
            weight = _random_outside_weight(rng, active_specs)
            records.append(FishRecord(id=0, weight=weight, spec=None, outside=True))
        else:
            weight, spec = _random_inside_weight(rng, active_specs)
            records.append(
                FishRecord(id=0, weight=weight, spec=spec, outside=False)
            )
            spec_counts[spec] += 1
        total_weight += weight

    return _finalize_batch_records(records, spec_counts, seed, active_specs, rng)


def format_summary(summary: BatchSummary) -> str:
    lines = [
        f"随机种子: {summary.seed}",
        f"启用规格: {', '.join(summary.enabled_specs)}",
        f"总条数  : {summary.total}",
        f"总重量  : {summary.total_weight:,}g ({summary.total_weight / 1000:.2f}kg)",
        f"均重    : {summary.avg_weight:.1f}g",
        f"规格内  : {summary.inside_count} 条",
        f"超规    : {summary.outside_count} 条 ({summary.outside_rate * 100:.2f}%)",
        "",
        "各规格条数:",
    ]
    for spec in summary.enabled_specs:
        cnt = summary.spec_counts.get(spec, 0)
        if cnt:
            lo, hi = SPECS[spec]
            lines.append(f"  {spec:>5s} ({lo:3d}-{hi:3d}g): {cnt:5d} 条")
    return "\n".join(lines)


def save_csv(records: list[FishRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "weight", "spec", "outside"])
        writer.writeheader()
        for fish in records:
            writer.writerow(
                {
                    "id": fish.id,
                    "weight": fish.weight,
                    "spec": fish.spec or "",
                    "outside": int(fish.outside),
                }
            )


def save_json(records: list[FishRecord], summary: BatchSummary, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": {
            **asdict(summary),
            "avg_weight": summary.avg_weight,
            "outside_rate": summary.outside_rate,
        },
        "fish": [asdict(f) for f in records],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def default_output_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按启用规格重量区间随机生成鱼批次（默认 25000 条，1% 超规）"
    )
    parser.add_argument("-n", "--total", type=int, default=DEFAULT_TOTAL, help="目标条数（与 --target-weight-g 二选一）")
    parser.add_argument(
        "--target-weight-g",
        type=int,
        default=0,
        help="目标总重（克）；指定后按总重生成，忽略 -n",
    )
    parser.add_argument(
        "-r", "--outside-rate", type=float, default=DEFAULT_OUTSIDE_RATE, help="超规比例"
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子")
    parser.add_argument(
        "--enabled-specs",
        type=str,
        default="",
        help="启用规格，逗号分隔，如 15p,20p,25p",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None, help="CSV 输出路径（默认 data/fish_seed_{seed}.csv）"
    )
    parser.add_argument("--json", type=Path, default=None, help="可选 JSON 输出路径")
    args = parser.parse_args()

    enabled = None
    if args.enabled_specs.strip():
        enabled = [s.strip() for s in args.enabled_specs.split(",") if s.strip()]

    if args.target_weight_g > 0:
        records, summary = generate_fish_batch_by_weight(
            target_weight_g=args.target_weight_g,
            outside_rate=args.outside_rate,
            seed=args.seed,
            enabled_specs=enabled,
            max_fish=max(args.total, DEFAULT_TOTAL),
        )
    else:
        records, summary = generate_fish_batch(
            total=args.total,
            outside_rate=args.outside_rate,
            seed=args.seed,
            enabled_specs=enabled,
        )

    out_dir = default_output_dir()
    csv_path = args.output or out_dir / f"fish_seed_{args.seed}.csv"
    save_csv(records, csv_path)

    print(format_summary(summary))
    print(f"\n已保存 CSV: {csv_path}")

    if args.json:
        save_json(records, summary, args.json)
        print(f"已保存 JSON: {args.json}")


if __name__ == "__main__":
    main()
