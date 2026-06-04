#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project : kaomam_project
@File : 随机种子生成.py
@Author : 18k
@Date : 2026/6/1
@Description: 随机生成 25000 条鱼的质量数据，其中 1% 不在规格范围（65-700g）内
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 成品规格表：重量范围 g
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

    @property
    def avg_weight(self) -> float:
        return self.total_weight / self.total if self.total else 0.0

    @property
    def outside_rate(self) -> float:
        return self.outside_count / self.total if self.total else 0.0


def classify_spec(weight: int) -> str | None:
    for name, (lo, hi) in SPECS.items():
        if lo <= weight <= hi:
            return name
    return None


def is_valid_weight(weight: int) -> bool:
    return VALID_MIN <= weight <= VALID_MAX


def is_true_outside_weight(weight: int) -> bool:
    """仅 65–700g 之外视为规格外（与 1% 规格外定义一致）。"""
    return weight < VALID_MIN or weight > VALID_MAX


def _random_outside_weight(rng: random.Random) -> int:
    """生成不在 65-700g 范围内的重量。"""
    if rng.random() < 0.5:
        return rng.randint(30, VALID_MIN - 1)
    return rng.randint(VALID_MAX + 1, 850)


def _random_inside_weight(
    rng: random.Random,
    spec_names: list[str] | None = None,
) -> tuple[int, str]:
    """随机选一个规格，再在该规格范围内取重量。"""
    names = spec_names or SPEC_NAMES
    spec = rng.choice(names)
    lo, hi = SPECS[spec]
    return rng.randint(lo, hi), spec


def generate_fish_batch(
    total: int = DEFAULT_TOTAL,
    outside_rate: float = DEFAULT_OUTSIDE_RATE,
    seed: int = DEFAULT_SEED,
    enabled_specs: tuple[str, ...] | list[str] | None = None,
) -> tuple[list[FishRecord], BatchSummary]:
    """
    生成一批鱼的质量数据。

    - total: 总条数，默认 25000
    - outside_rate: 规格外比例，默认 1%（250 条）
    - seed: 随机种子，保证可复现
    """
    rng = random.Random(seed)
    outside_count = round(total * outside_rate)
    inside_count = total - outside_count
    active_specs = [s for s in (enabled_specs or SPEC_NAMES) if s in SPECS]
    if not active_specs:
        active_specs = list(SPEC_NAMES)

    records: list[FishRecord] = []
    spec_counts: dict[str, int] = {s: 0 for s in SPEC_NAMES}

    for i in range(1, total + 1):
        if i <= outside_count:
            weight = _random_outside_weight(rng)
            records.append(FishRecord(id=i, weight=weight, spec=None, outside=True))
        else:
            weight, spec = _random_inside_weight(rng, active_specs)
            records.append(FishRecord(id=i, weight=weight, spec=spec, outside=False))
            spec_counts[spec] += 1

    rng.shuffle(records)
    for seq, fish in enumerate(records, start=1):
        fish.id = seq

    total_weight = sum(f.weight for f in records)
    actual_outside = sum(1 for f in records if f.outside)
    summary = BatchSummary(
        total=total,
        total_weight=total_weight,
        outside_count=actual_outside,
        inside_count=total - actual_outside,
        spec_counts={k: v for k, v in spec_counts.items() if v > 0},
        seed=seed,
    )
    return records, summary


def format_summary(summary: BatchSummary) -> str:
    lines = [
        f"随机种子: {summary.seed}",
        f"总条数  : {summary.total}",
        f"总重量  : {summary.total_weight:,}g ({summary.total_weight / 1000:.2f}kg)",
        f"均重    : {summary.avg_weight:.1f}g",
        f"规格内  : {summary.inside_count} 条",
        f"规格外  : {summary.outside_count} 条 ({summary.outside_rate * 100:.2f}%)",
        "",
        "各规格条数:",
    ]
    for spec in SPEC_NAMES:
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
    parser = argparse.ArgumentParser(description="随机生成鱼质量数据（默认 25000 条，1% 规格外）")
    parser.add_argument("-n", "--total", type=int, default=DEFAULT_TOTAL, help="总条数")
    parser.add_argument(
        "-r", "--outside-rate", type=float, default=DEFAULT_OUTSIDE_RATE, help="规格外比例"
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子")
    parser.add_argument(
        "-o", "--output", type=Path, default=None, help="CSV 输出路径（默认 data/fish_seed_{seed}.csv）"
    )
    parser.add_argument("--json", type=Path, default=None, help="可选 JSON 输出路径")
    args = parser.parse_args()

    records, summary = generate_fish_batch(
        total=args.total,
        outside_rate=args.outside_rate,
        seed=args.seed,
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
