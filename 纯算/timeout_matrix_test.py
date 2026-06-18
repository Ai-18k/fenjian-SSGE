#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
超时阈值矩阵批量测试 — 三模块 × 多档超时 → CSV 对比

测试规格组:
  module-a  15p ~ 40p
  module-b  45p ~ 90p
  module-c  100p ~ 150p

默认超时档位: 180, 240, 300, 360, 420, 480, 540, 600（每 60 一档）

用法:
  python timeout_matrix_test.py
  python timeout_matrix_test.py --seed 42 -w 10
  python timeout_matrix_test.py -n 3000 --timeout-min 180 --timeout-max 600
  python timeout_matrix_test.py --output-dir data/timeout_matrix_seed42

仅标准库，无第三方依赖。
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from Scheduler_EngineV1 import (  # noqa: E402
    DEFAULT_SEED,
    DEFAULT_STOP_WEIGHT_G,
    DEFAULT_TOTAL,
    MODULE_SPECS,
    STOP_MODE_COUNT,
    STOP_MODE_WEIGHT,
    TIMEOUT_CLOCK_INTAKE,
    TIMEOUT_CLOCK_REAL,
    SchedulerEngine,
    batch_total_for_run,
    load_or_generate_batch,
    normalize_enabled_specs,
)

MODULE_GROUPS: dict[str, tuple[str, ...]] = {
    "15p-40p": tuple(MODULE_SPECS["A"]),
    "45p-90p": tuple(MODULE_SPECS["B"]),
    "100p-150p": tuple(MODULE_SPECS["C"]),
}

BUCKET_CN = {"small": "小", "medium": "中", "large": "大"}

SUMMARY_FIELDS = [
    "模块", "规格", "超时阈值", "计时方式", "种子",
    "入料条数", "入料吨数", "成盒数", "装箱条数", "装箱率%",
    "超时合计", "料道超时", "暂存超时", "批末尾料",
    "暂存峰值", "暂存容量", "暂存峰值%", "仿真步数",
]

TIMEOUT_DETAIL_FIELDS = [
    "模块", "超时阈值", "计时方式", "鱼ID", "重量g", "规格", "分区", "分区en",
    "来源", "首次入系统步", "出局步", "队首/暂存等待", "系统停留", "阈值", "轮次", "种子",
]

BY_SPEC_FIELDS = ["模块", "超时阈值", "规格", "超时条数", "超时总重g"]


def _run_single(
    *,
    module_label: str,
    specs: tuple[str, ...],
    move_timeout: int,
    seed: int,
    stop_mode: str,
    stop_count: int,
    stop_weight_g: int,
    timeout_clock: str,
) -> tuple[dict, list[dict]]:
    """跑单次仿真，返回 (汇总行, 超时鱼明细列表)。"""
    batch_total = batch_total_for_run(
        stop_mode, stop_count, stop_weight_g, enabled_specs=specs
    )
    records = load_or_generate_batch(
        seed=seed,
        total=batch_total,
        enabled_specs=specs,
        stop_mode=stop_mode,
        stop_weight_g=stop_weight_g,
    )
    engine = SchedulerEngine(
        batch_records=records,
        seed=seed,
        specs=specs,
        move_timeout=move_timeout,
        timeout_clock=timeout_clock,
        stop_mode=stop_mode,
        stop_count=stop_count,
        stop_weight_g=stop_weight_g,
        quiet=True,
        verbose=False,
        exclude_outside_stats=True,
        log_every=999_999_999,
    )
    while engine.process_one():
        pass
    engine._enforce_timeouts()
    engine.finish_batch()

    metrics = engine.build_final_metrics()
    pack_rate = (
        round(engine.stats.packed_fish / engine.stats.input_count * 100, 2)
        if engine.stats.input_count
        else 0.0
    )
    summary = {
        "模块": module_label,
        "规格": ",".join(specs),
        "超时阈值": move_timeout,
        "计时方式": timeout_clock,
        "种子": seed,
        "入料条数": engine.stats.input_count,
        "入料吨数": round(engine.stats.input_weight / 1_000_000, 3),
        "成盒数": engine.stats.cartons,
        "装箱条数": engine.stats.packed_fish,
        "装箱率%": pack_rate,
        "超时合计": metrics["timeout_total_count"],
        "料道超时": metrics["timeout_lane_count"],
        "暂存超时": metrics["timeout_storage_count"],
        "批末尾料": metrics["tail_batch_count"],
        "暂存峰值": metrics["storage_peak"],
        "暂存容量": metrics["storage_capacity"],
        "暂存峰值%": metrics["storage_peak_pct"],
        "仿真步数": engine.tick,
    }

    timeout_rows: list[dict] = []
    for row in engine.timeout_tail_log:
        timeout_rows.append(
            {
                "模块": module_label,
                "超时阈值": move_timeout,
                "计时方式": timeout_clock,
                "鱼ID": row["fish_id"],
                "重量g": row["weight"],
                "规格": row["spec"],
                "分区": BUCKET_CN.get(row.get("bucket", ""), row.get("bucket", "")),
                "分区en": row.get("bucket", ""),
                "来源": row.get("source", "lane"),
                "首次入系统步": row.get("first_in_time"),
                "出局步": row.get("tick"),
                "队首/暂存等待": row.get("lane_wait_s"),
                "系统停留": row.get("system_dwell_s"),
                "阈值": row.get("threshold_s"),
                "轮次": row.get("rounds"),
                "种子": row.get("batch_seed", seed),
            }
        )
    return summary, timeout_rows


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _build_matrix(
    summaries: list[dict],
    value_key: str,
    timeouts: list[int],
) -> list[dict]:
    """模块 × 超时阈值 矩阵行。"""
    modules = list(MODULE_GROUPS.keys())
    lookup = {(s["模块"], s["超时阈值"]): s[value_key] for s in summaries}
    rows: list[dict] = []
    for mod in modules:
        row: dict = {"模块": mod}
        for mt in timeouts:
            row[str(mt)] = lookup.get((mod, mt), "")
        rows.append(row)
    return rows


def _build_by_spec(timeout_details: list[dict]) -> list[dict]:
    """按模块/阈值/规格汇总超时条数与总重。"""
    agg: dict[tuple, list] = defaultdict(lambda: [0, 0])
    for row in timeout_details:
        key = (row["模块"], row["超时阈值"], row["规格"])
        agg[key][0] += 1
        agg[key][1] += row["重量g"]
    rows = [
        {
            "模块": k[0],
            "超时阈值": k[1],
            "规格": k[2],
            "超时条数": v[0],
            "超时总重g": v[1],
        }
        for k, v in sorted(agg.items(), key=lambda x: (x[0][0], x[0][1], -x[1][0]))
    ]
    return rows


def export_csv(
    summaries: list[dict],
    timeout_details: list[dict],
    output_dir: Path,
    timeouts: list[int],
) -> dict[str, Path]:
    """写入 CSV 文件集，返回路径映射。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    matrix_cols = ["模块"] + [str(t) for t in timeouts]

    paths = {
        "summary": output_dir / "运行汇总.csv",
        "timeout_matrix": output_dir / "超时数量矩阵.csv",
        "pack_rate_matrix": output_dir / "装箱率矩阵.csv",
        "storage_peak_matrix": output_dir / "暂存峰值矩阵.csv",
        "timeout_detail": output_dir / "超时鱼明细.csv",
        "by_spec": output_dir / "按规格统计.csv",
    }

    _write_csv(paths["summary"], SUMMARY_FIELDS, summaries)
    _write_csv(
        paths["timeout_matrix"],
        matrix_cols,
        _build_matrix(summaries, "超时合计", timeouts),
    )
    _write_csv(
        paths["pack_rate_matrix"],
        matrix_cols,
        _build_matrix(summaries, "装箱率%", timeouts),
    )
    _write_csv(
        paths["storage_peak_matrix"],
        matrix_cols,
        _build_matrix(summaries, "暂存峰值%", timeouts),
    )
    _write_csv(paths["timeout_detail"], TIMEOUT_DETAIL_FIELDS, timeout_details)
    if timeout_details:
        _write_csv(paths["by_spec"], BY_SPEC_FIELDS, _build_by_spec(timeout_details))

    return paths


def main() -> None:
    parser = argparse.ArgumentParser(
        description="三模块 × 超时阈值矩阵批量测试，导出 CSV 对比",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子")
    parser.add_argument("--timeout-min", type=int, default=180, help="超时阈值下限")
    parser.add_argument("--timeout-max", type=int, default=600, help="超时阈值上限")
    parser.add_argument("--timeout-step", type=int, default=60, help="超时阈值步长")
    parser.add_argument(
        "--timeout-clock",
        choices=[TIMEOUT_CLOCK_INTAKE, TIMEOUT_CLOCK_REAL],
        default=TIMEOUT_CLOCK_INTAKE,
        help="超时计时：intake=进料步；real=墙钟秒",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="输出目录（默认 data/timeout_matrix_seed{seed}_{时间}/）",
    )
    stop_g = parser.add_mutually_exclusive_group()
    stop_g.add_argument("-n", "--count", type=int, default=None, help="按条数结束")
    stop_g.add_argument("-w", "--weight", type=float, default=10.0, help="按总重结束（吨）")
    args = parser.parse_args()

    timeouts = list(range(args.timeout_min, args.timeout_max + 1, args.timeout_step))
    if not timeouts:
        parser.error("超时档位为空，请检查 --timeout-min/max/step")

    if args.count is not None:
        stop_mode = STOP_MODE_COUNT
        stop_count = args.count
        stop_weight_g = DEFAULT_STOP_WEIGHT_G
        stop_label = f"n{stop_count}"
    else:
        stop_mode = STOP_MODE_WEIGHT
        stop_count = DEFAULT_TOTAL
        stop_weight_g = int(args.weight * 1_000_000)
        stop_label = f"w{args.weight}t"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir or (
        _ROOT / "data" / f"timeout_matrix_seed{args.seed}_{stop_label}_{ts}"
    )

    total_runs = len(MODULE_GROUPS) * len(timeouts)
    print("=" * 64)
    print("超时矩阵批量测试")
    print("=" * 64)
    print(f"  种子: {args.seed} | 结束: {stop_label} | 计时: {args.timeout_clock}")
    print(f"  模块: {', '.join(MODULE_GROUPS)}")
    print(f"  超时档位: {timeouts}")
    print(f"  总跑次: {total_runs} | 输出目录: {output_dir}")
    print("=" * 64)

    summaries: list[dict] = []
    timeout_details: list[dict] = []
    run_idx = 0
    t_all = time.perf_counter()

    for module_label, specs in MODULE_GROUPS.items():
        specs = normalize_enabled_specs(specs)
        for mt in timeouts:
            run_idx += 1
            print(
                f"[{run_idx}/{total_runs}] {module_label} | 超时={mt} ... ",
                end="",
                flush=True,
            )
            t0 = time.perf_counter()
            summary, rows = _run_single(
                module_label=module_label,
                specs=specs,
                move_timeout=mt,
                seed=args.seed,
                stop_mode=stop_mode,
                stop_count=stop_count,
                stop_weight_g=stop_weight_g,
                timeout_clock=args.timeout_clock,
            )
            elapsed = time.perf_counter() - t0
            summaries.append(summary)
            timeout_details.extend(rows)
            print(
                f"超时 {summary['超时合计']} | 装箱 {summary['装箱率%']}% | {elapsed:.1f}s"
            )

    paths = export_csv(summaries, timeout_details, output_dir, timeouts)
    print("=" * 64)
    print(f"完成，总耗时 {time.perf_counter() - t_all:.1f}s")
    print(f"输出目录: {output_dir}")
    for name, path in paths.items():
        print(f"  {path.name}")


if __name__ == "__main__":
    main()
