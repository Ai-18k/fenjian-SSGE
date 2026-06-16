#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
智能分拣 · 纯算法批量测试入口（无 Web / 无前端）

与 web_server.SimulationRunner.start + SchedulerEngine.process_one/finish_batch 同逻辑，
批量测试默认 exclude_outside_stats：规格外不计入料/结束条件，进度与汇总统计超时鱼。

用法（在项目根目录）：
  python src/batch_runner.py run --seed 42 -n 25000 --specs module-a
  python src/batch_runner.py run --seed 42 --weight 10 --specs module-c --speed 50
  python src/batch_runner.py preset --list
  python src/batch_runner.py preset module-a module-b module-c -n 25000 --seed 42
  python src/batch_runner.py matrix --file plan/batch_cases.example.json
  ython batch_runner.py run --seed 42 --weight 10 --specs module-c --move-timeout 180 --speed 50
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

SRC_DIR = Path(__file__).resolve().parent
ROOT = SRC_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from Scheduler_Engine import (  # noqa: E402
    ALL_SPECS,
    DEFAULT_CAP_FACTOR,
    DEFAULT_ENABLED_SPECS,
    DEFAULT_MOVE_TIMEOUT,
    DEFAULT_SEED,
    DEFAULT_STOP_WEIGHT_G,
    DEFAULT_STOP_WEIGHT_TONS,
    DEFAULT_TIMEOUT_CLOCK,
    DEFAULT_TOTAL,
    MODULE_SPECS,
    STOP_MODE_COUNT,
    STOP_MODE_WEIGHT,
    TIMEOUT_CLOCK_INTAKE,
    TIMEOUT_CLOCK_REAL,
    SchedulerEngine,
    _root,
    batch_total_for_run,
    load_or_generate_batch,
    normalize_enabled_specs,
)

# ---------------------------------------------------------------------------
# 预设规格组（与 md/数据记录.md 模块划分一致）
# ---------------------------------------------------------------------------
SPEC_PRESETS: dict[str, tuple[str, ...]] = {
    "module-a": tuple(MODULE_SPECS["A"]),
    "module-b": tuple(MODULE_SPECS["B"]),
    "module-c": tuple(MODULE_SPECS["C"]),
    "default": DEFAULT_ENABLED_SPECS,
    "all": ALL_SPECS,
}


def resolve_specs(raw: str | list[str] | None) -> tuple[str, ...]:
    """解析 CLI/JSON 中的规格：预设名 module-a 或逗号分隔 15p,20p。"""
    if raw is None:
        return DEFAULT_ENABLED_SPECS
    if isinstance(raw, str):
        key = raw.strip().lower()
        if key in SPEC_PRESETS:
            return SPEC_PRESETS[key]
        parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
        return normalize_enabled_specs(parts)
    return normalize_enabled_specs(raw)


# 进料速率（条/秒）；0=不限速（批量默认，与 web_server 的 sleep 逻辑一致）
DEFAULT_BATCH_SPEED = 20.0


@dataclass
class RunConfig:
    name: str = "run"
    seed: int = DEFAULT_SEED
    stop_mode: str = STOP_MODE_COUNT
    stop_count: int = DEFAULT_TOTAL
    stop_weight_g: int = DEFAULT_STOP_WEIGHT_G
    enabled_specs: tuple[str, ...] = DEFAULT_ENABLED_SPECS
    move_timeout: int = DEFAULT_MOVE_TIMEOUT
    cap_factor: int = DEFAULT_CAP_FACTOR
    timeout_clock: str = DEFAULT_TIMEOUT_CLOCK
    speed: float = DEFAULT_BATCH_SPEED
    verbose: bool = False


@dataclass
class RunSummary:
    name: str
    seed: int
    stop_mode: str
    stop_target: str
    enabled_specs: str
    input_count: int
    input_weight_tons: float
    cartons: int
    packed_fish: int
    pack_rate_pct: float
    outside_count: int
    timeout_total: int
    timeout_lane: int
    reflow_count: int
    timeout_tail: int
    overflow_reflow: int
    storage_in: int
    storage_to_lane: int
    storage_packed: int
    storage_max: int
    storage_capacity: int
    storage_timeout_tail: int
    storage_batch_tail: int
    tail_batch_total: int
    tail_lane_batch: int
    tail_storage_batch: int
    tail_reflow_batch: int
    storage_peak_pct: float
    unmatched_count: int
    tail_count: int
    sim_tick: int
    wall_seconds: float
    carton_weight_min: int | None = None
    carton_weight_max: int | None = None
    carton_weight_avg: float | None = None

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def config_from_dict(data: dict[str, Any], default_name: str = "case") -> RunConfig:
    """从 JSON 用例 dict 构建 RunConfig。"""
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
    """与 web_server.SimulationRunner.start 相同的批次加载与引擎构造。"""
    enabled = cfg.enabled_specs
    stop_count = max(1, cfg.stop_count)
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
    """批末扫尾未配盒尾料（不含运行中超时/箱满/规格外）。"""
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
    s = engine.stats
    if cfg.stop_mode == STOP_MODE_WEIGHT:
        target = f"{cfg.stop_weight_g / 1_000_000:.1f}t"
    else:
        target = str(cfg.stop_count)
    weights = [c.weight for c in engine.cartons]
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
    """快速跑完一批：process_one 循环 → finish_batch → 汇总。"""
    engine = build_engine(cfg, quiet=quiet)
    t0 = time.perf_counter()
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
    """将 data/*_seed_{seed}.csv 复制到批量输出目录，避免用例互相覆盖。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir = _root / "data"
    safe = case_name.replace("/", "-").replace("\\", "-")
    for suffix in ("run_report", "cartons", "remaining", "timeout_tail"):
        src = data_dir / f"{suffix}_seed_{seed}.csv"
        if src.is_file():
            shutil.copy2(src, out_dir / f"{safe}_{suffix}.csv")


def print_tail_storage_summary(s: RunSummary) -> None:
    print(
        f"  批末未配盒 : {s.tail_batch_total} "
        f"(料道{s.tail_lane_batch} · 暂存{s.tail_storage_batch} · 回流{s.tail_reflow_batch})"
    )
    print(
        f"  暂存箱峰值 : {s.storage_max} / {s.storage_capacity} "
        f"({s.storage_peak_pct}%) · 入{s.storage_in} 回道{s.storage_to_lane} 成盒{s.storage_packed}"
    )


def print_summary_line(s: RunSummary) -> None:
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
    widths = [
        max(len(h), *(len(r[i]) for r in table_rows)) for i, h in enumerate(headers)
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in table_rows:
        print(fmt.format(*row))


def save_summary_csv(rows: list[RunSummary], path: Path) -> None:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases": [s.as_row() for s in rows],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_matrix_file(path: Path) -> list[RunConfig]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases", data)
    if not isinstance(cases, list):
        raise ValueError("矩阵文件需包含 cases 数组")
    return [config_from_dict(c, default_name=f"case_{i}") for i, c in enumerate(cases)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _add_common_run_args(p: argparse.ArgumentParser) -> None:
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
    if args.weight is not None:
        return STOP_MODE_WEIGHT, DEFAULT_TOTAL, int(args.weight * 1_000_000)
    count = args.count if args.count is not None else DEFAULT_TOTAL
    return STOP_MODE_COUNT, count, DEFAULT_STOP_WEIGHT_G


def cmd_run(args: argparse.Namespace) -> int:
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
    parser = argparse.ArgumentParser(
        description="智能分拣纯算法批量测试（无 Web 前端）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python src/batch_runner.py run -n 25000 --specs module-a --seed 42
  python src/batch_runner.py run -w 10 --specs module-c --move-timeout 180 --speed 50
  python src/batch_runner.py preset module-a module-b module-c -n 25000
  python src/batch_runner.py matrix --file plan/batch_cases.example.json
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
