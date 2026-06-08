#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project : kaomam_project
@File : web_server.py
@Description: 智能分拣 Web 可视化服务 — 启动后浏览器打开 http://127.0.0.1:8765

=============================================================================
工作流程说明
=============================================================================

一、快速开始
-----------------------------------------------------------------------------
  启动：
    python src/web_server.py
    或双击 src/start_web.bat
  默认地址：http://127.0.0.1:8765  （环境变量 WEB_PORT 可改端口）

  两个页面（共用同一后端 SimulationRunner）：
    /         统计面板 index.html（?embed=1 可嵌入监控页）
    /monitor  FIFO 动画 + 内嵌统计标签页 fifo_monitor.html（推荐单页，避免切换丢动画状态）

  典型操作顺序：
    1. 打开 / ，设置种子/条数/倍速，点「开始模拟」
    2. 需要动画时打开 /monitor（或点页头链接），状态与入料进度自动同步
    3. 跑完后在 / 点「成盒数据」「尾料数据」查看，或导出 CSV
    4. 「下载报告」获取全批次 fish 追踪明细

二、系统架构
-----------------------------------------------------------------------------
  web_server.py
    └─ SimulationRunner（后台线程）
         └─ SchedulerEngine（Scheduler_Engine.py）
              · process_one()  每条鱼入料 → 料道 → 封箱 → 回流
              · finish_batch() 批末扫尾、写 CSV、标记尾料

  前端轮询/控制：
    GET  /api/state     每 200ms 读快照（入料、装盒、模块库存…）
    POST /api/start     开始  {seed, total, move_timeout, speed}
    POST /api/pause|resume|stop|speed

  统计面板与 FIFO 动画共用上述 API：
    · 入料进度：input_count / total_fish（两页数字一致）
    · 启停状态：running / paused / finished（两页同步）
    · FIFO 动画按 input_count 逐条 spawn，不批量补鱼

三、成盒鱼数据 — 如何记录与查看
-----------------------------------------------------------------------------
  【何时产生】
    引擎 _try_pack_all() 每成功封一箱（4980~5030g）：
      · engine.cartons 追加 BoxPlan（spec/count/weight/parts/fish）
      · stats.cartons、stats.packed_fish 累加
      · events 追加 kind=pack

  【批末持久化】finish_batch() 自动写：
    data/cartons_seed_{seed}.csv
    字段：carton_seq, spec, count, weight, small, medium, large, fish_ids

  【在线 / 导出】
    GET /api/cartons?seed=42         JSON，字段 records[]
    GET /api/cartons.csv?seed=42     下载 CSV
    GET /api/state                   运行中可读 carton_records、recent_cartons

  【统计面板操作】
    按钮「成盒数据」→ 弹窗在线查看（按当前种子）
    弹窗「导出 CSV」→ cartons_seed_{seed}.csv
    （模拟进行中可读内存；历史批次可读 data/ 下 CSV）

  【若要扩展字段】例如操作员、封箱时间：
    1. Scheduler_Engine._try_pack_all() 封箱处写入
    2. _save_cartons_csv() 增列
    3. get_snapshot() 的 carton_records 增字段
    4. web_server._cartons_from_disk() 同步 JSON 映射

四、25000 跑完后 — 未成盒尾料
-----------------------------------------------------------------------------
  【何时产生】
    入料 total 条（默认 25000）结束后，SimulationRunner 调用 finish_batch()：
      · 继续扫尾封箱（处理 reflow 队列）
      · 料道/回流/规格外仍未装箱 → tracker.unmatched

  【自动导出】
    data/remaining_seed_{seed}.csv   未成盒尾料明细
    data/run_report_seed_{seed}.csv    全批次每条鱼生命周期
    data/cartons_seed_{seed}.csv       成盒明细（见第三节）

  【在线 / 导出】
    GET /api/remaining?seed=42        JSON，字段 fish[]
    GET /api/remaining.csv?seed=42    下载尾料 CSV
    GET /api/report?seed=42           下载全量追踪 CSV
    GET /api/state                    finished=true 时含 remaining_fish、remaining_count

  【统计面板操作】
    按钮「尾料数据」→ 弹窗在线查看
    弹窗「导出 CSV」→ remaining_seed_{seed}.csv

  【status 含义】
    unmatched_tail     批末料道剩余
    unmatched_reflow   回流后仍未再装

    unmatched_outside  规格外

五、FIFO 动画页（/monitor）要点
-----------------------------------------------------------------------------
  5.1 活跃需求为何应是 54 路（18 规格 × 小/中/大）
    地址格式：模块/规格/区段，如 A/15p/light
    collectDemands() 应遍历全部 54 路参与监控与广播；
    仅缺鱼或装箱 releaseRemaining>0 才入列会导致统计只有个位数（已修复）。

  5.2 装箱工位状态
    按 18 个规格展示色块：空闲 / 待装箱 / 进箱中 / 填箱中 / 封箱中
    （统计面板不含装箱动画工位，仅 FIFO 页展示）

  5.3 运行日志

    fifo_monitor.html 中 .log 控制日志区域高度（当前 360px）。

  5.4 与统计面板同步
    · 打开 /monitor 时读取 /api/state，batchIndex 对齐 input_count，不补历史动画
    · 运行中按 spawnTimer 逐条进料（batchIndex < serverInputCount 时每次一条）
    · 开始/暂停/继续/停止 均调用同一套 POST API

六、默认参数与数据文件
-----------------------------------------------------------------------------
  默认种子 seed=42，总条数 total=25000，移动超时 move_timeout=30s
  批次源：data/fish_seed_{seed}.csv（不存在则自动生成）

  输出目录 data/（批末生成）：
    fish_seed_{seed}.csv
    cartons_seed_{seed}.csv
    remaining_seed_{seed}.csv
    run_report_seed_{seed}.csv

七、API 一览
-----------------------------------------------------------------------------
  GET  /                    统计面板
  GET  /monitor             FIFO 动画
  GET  /api/state           模拟快照（核心）
  GET  /api/config          默认参数、模块规格表
  GET  /api/batch           种子批次 fish 列表（FIFO 动画用）
  GET  /api/cartons         成盒 JSON
  GET  /api/cartons.csv     成盒 CSV
  GET  /api/remaining       尾料 JSON
  GET  /api/remaining.csv   尾料 CSV
  GET  /api/report          全量追踪 CSV
  GET  /api/version         版本与路由列表
  POST /api/start           开始模拟
  POST /api/pause           暂停
  POST /api/resume          继续
  POST /api/stop            停止
  POST /api/speed           运行中调整倍速 {speed}

八、相关源码
-----------------------------------------------------------------------------
  Scheduler_Engine.py   分拣引擎、封箱、尾料、CSV 导出
  index.html            统计面板 UI
  fifo_monitor.html     FIFO 动画 UI
  data/                 批次与报告 CSV
"""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

SRC_DIR = Path(__file__).resolve().parent
ROOT_DIR = SRC_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from Scheduler_Engine import (
    ALL_SPECS,
    BUCKET_RANGES,
    DEFAULT_ENABLED_SPECS,
    DEFAULT_MOVE_TIMEOUT,
    DEFAULT_SEED,
    DEFAULT_TOTAL,
    FishTrace,
    MODULE_SPECS,
    SPECS,
    TARGET_MAX,
    TARGET_MIN,
    SchedulerEngine,
    describe_tail_trace,
    load_or_generate_batch,
    normalize_enabled_specs,
)


def bucket_ranges_for_api() -> dict[str, dict[str, list[int]]]:
    """各规格小/中/大重量区间，与 plan/细分规则.py、Scheduler_Engine 一致。"""
    return {
        spec: {
            "small": list(br.small),
            "medium": list(br.medium),
            "large": list(br.large),
        }
        for spec, br in BUCKET_RANGES.items()
    }


def parse_enabled_specs(raw) -> tuple[str, ...]:
    """解析启用规格；GET 查询 enabled_specs=60p,70p 时 parse_qs 常为 ['60p,70p'] 单元素。"""
    if raw is None:
        return normalize_enabled_specs(None)
    return normalize_enabled_specs(raw)


def normalize_path(raw: str) -> str:
    """统一 URI 路径，避免尾斜杠等导致 404。"""
    p = unquote(urlparse(raw).path or "/")
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return p


SERVER_VERSION = "2.2"


class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class SimulationRunner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.engine: SchedulerEngine | None = None
        self.running = False
        self.paused = False
        self.stop_flag = False
        self.thread: threading.Thread | None = None
        self.speed = 10.0  # 条/秒（与 fifo_monitor 速度输入一致）
        self.error: str | None = None

    def get_state(self) -> dict:
        with self.lock:
            if self.engine is None:
                return {
                    "status": "idle",
                    "running": False,
                    "paused": False,
                    "error": self.error,
                }
            snap = self.engine.get_snapshot()
            if self.running and not self.paused and not self.engine.finished:
                self.engine._sync_tick()
                snap["tick"] = self.engine.tick
            snap["status"] = "finished" if self.engine.finished else ("paused" if self.paused else "running")
            snap["running"] = self.running
            snap["paused"] = self.paused
            snap["speed"] = self.speed
            snap["error"] = self.error
            return snap

    def start(
        self,
        seed: int = DEFAULT_SEED,
        total: int = DEFAULT_TOTAL,
        move_timeout: int = DEFAULT_MOVE_TIMEOUT,
        speed: float = 10.0,
        enabled_specs: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        enabled = parse_enabled_specs(enabled_specs)
        with self.lock:
            if self.running:
                raise RuntimeError("模拟已在运行")
            self.stop_flag = False
            self.paused = False
            self.error = None
            self.speed = max(0.1, speed)
            records = load_or_generate_batch(seed=seed, total=total, enabled_specs=enabled)
            self.engine = SchedulerEngine(
                batch_records=records,
                seed=seed,
                move_timeout=move_timeout,
                specs=enabled,
                log_every=max(50, total // 50),
            )
            self.running = True

        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        try:
            while True:
                with self.lock:
                    if self.stop_flag or self.engine is None:
                        break
                    if self.paused:
                        eng = None
                    else:
                        eng = self.engine
                if eng is None:
                    time.sleep(0.1)
                    continue
                has_more = eng.process_one()
                if not has_more:
                    with self.lock:
                        if self.engine:
                            self.engine.finish_batch()
                    break
                time.sleep(1.0 / self.speed)
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
        finally:
            with self.lock:
                self.running = False

    def pause(self) -> None:
        with self.lock:
            self.paused = True
            if self.engine is not None:
                self.engine.pause_clock()

    def resume(self) -> None:
        with self.lock:
            self.paused = False
            if self.engine is not None:
                self.engine.resume_clock()

    def set_speed(self, speed: float) -> None:
        with self.lock:
            self.speed = max(0.1, float(speed))

    def stop(self) -> None:
        with self.lock:
            self.stop_flag = True
            self.paused = False


RUNNER = SimulationRunner()


def _read_csv_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    import csv

    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _parse_pipe_ints(raw: str) -> list[int]:
    return [int(x) for x in (raw or "").split("|") if x.strip().isdigit()]


def _cartons_from_disk(seed: int) -> dict:
    rows = _read_csv_rows(ROOT_DIR / "data" / f"cartons_seed_{seed}.csv")
    records = []
    for r in rows:
        fish_ids = _parse_pipe_ints(r.get("fish_ids") or "")
        fish_weights = _parse_pipe_ints(r.get("fish_weights") or "")
        fish_buckets = [b for b in (r.get("fish_buckets") or "").split("|") if b != ""]
        fish = []
        for i, fid in enumerate(fish_ids):
            fish.append(
                {
                    "id": fid,
                    "weight": fish_weights[i] if i < len(fish_weights) else None,
                    "bucket": fish_buckets[i] if i < len(fish_buckets) else "",
                }
            )
        records.append(
            {
                "seq": int(r["carton_seq"]),
                "spec": r["spec"],
                "count": int(r["count"]),
                "weight": int(r["weight"]),
                "parts": {
                    "small": int(r.get("small") or 0),
                    "medium": int(r.get("medium") or 0),
                    "large": int(r.get("large") or 0),
                },
                "fish_ids": fish_ids,
                "fish_weights": fish_weights,
                "fish": fish,
            }
        )
    return {"seed": seed, "total": len(records), "records": records}


def _remaining_from_disk(seed: int) -> dict:
    rows = _read_csv_rows(ROOT_DIR / "data" / f"remaining_seed_{seed}.csv")
    fish = []
    for r in rows:
        reasons = [x for x in (r.get("reflow_reasons") or "").split("|") if x]
        item = {
            "fish_id": int(r["fish_id"]),
            "weight": int(r["weight"]),
            "spec": r.get("spec") or "",
            "rounds": int(r.get("rounds") or 1),
            "status": r.get("status") or "",
            "reflow_reasons": reasons,
        }
        if r.get("tail_cause"):
            item["tail_cause"] = r["tail_cause"]
            item["reflow_summary"] = r.get("reflow_summary") or ""
            item["had_timeout"] = str(r.get("had_timeout") or "0") in ("1", "True", "true")
            item["had_overflow"] = str(r.get("had_overflow") or "0") in ("1", "True", "true")
            dwell = r.get("dwell_time")
            item["dwell_time"] = int(dwell) if dwell not in (None, "") else None
        else:
            trace = FishTrace(
                fish_id=item["fish_id"],
                weight=item["weight"],
                spec=item["spec"] or None,
                rounds=item["rounds"],
                status=item["status"],
                reflow_reasons=reasons,
            )
            item.update(describe_tail_trace(trace))
        fish.append(item)
    return {"seed": seed, "finished": True, "total": len(fish), "fish": fish}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args) -> None:
        pass

    def _json(self, data: dict, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = normalize_path(self.path)
        qs = parse_qs(urlparse(self.path).query)

        if path in ("/", "/index.html"):
            return self._file(SRC_DIR / "index.html")

        if path in ("/monitor", "/monitor.html", "/fifo_monitor.html", "/fifo", "/fifo.html"):
            return self._file(SRC_DIR / "fifo_monitor.html")

        if path == "/api/version":
            return self._json(
                {
                    "version": SERVER_VERSION,
                    "routes": [
                        "/",
                        "/monitor",
                        "/api/batch",
                        "/api/config",
                        "/api/state",
                        "/api/cartons",
                        "/api/remaining",
                        "/api/report",
                        "/api/cartons.csv",
                        "/api/remaining.csv",
                    ],
                    "monitor_file": str(SRC_DIR / "fifo_monitor.html"),
                    "monitor_exists": (SRC_DIR / "fifo_monitor.html").is_file(),
                }
            )

        if path == "/api/batch":
            seed = int(qs.get("seed", [DEFAULT_SEED])[0])
            total = int(qs.get("total", [DEFAULT_TOTAL])[0])
            enabled = parse_enabled_specs(qs.get("enabled_specs"))
            records = load_or_generate_batch(seed=seed, total=total, enabled_specs=enabled)
            payload = [
                {
                    "id": r.id,
                    "weight": r.weight,
                    "spec": r.spec or "",
                    "outside": bool(r.outside),
                }
                for r in records[:total]
            ]
            return self._json(
                {
                    "seed": seed,
                    "total": len(payload),
                    "enabled_specs": list(enabled),
                    "fish": payload,
                }
            )

        if path == "/api/config":
            return self._json(
                {
                    "default_seed": DEFAULT_SEED,
                    "default_total": DEFAULT_TOTAL,
                    "default_move_timeout": DEFAULT_MOVE_TIMEOUT,
                    "default_enabled_specs": list(DEFAULT_ENABLED_SPECS),
                    "all_specs": list(ALL_SPECS),
                    "spec_ranges": {k: list(v["range"]) for k, v in SPECS.items()},
                    "bucket_ranges": bucket_ranges_for_api(),
                    "bucket_rules_source": "plan/细分规则.py (calc_bucket_split / bucket_of)",
                    "target_min": TARGET_MIN,
                    "target_max": TARGET_MAX,
                    "modules": {k: list(v) for k, v in MODULE_SPECS.items()},
                }
            )

        if path == "/api/state":
            return self._json(RUNNER.get_state())

        if path == "/api/cartons":
            seed = int(qs.get("seed", [DEFAULT_SEED])[0])
            state = RUNNER.get_state()
            if state.get("seed") == seed and state.get("carton_records"):
                return self._json(
                    {
                        "seed": seed,
                        "total": state.get("cartons", 0),
                        "records": state.get("carton_records", []),
                    }
                )
            disk = _cartons_from_disk(seed)
            if disk["records"]:
                return self._json(disk)
            return self._json({"seed": seed, "total": 0, "records": []})

        if path == "/api/remaining":
            seed = int(qs.get("seed", [DEFAULT_SEED])[0])
            state = RUNNER.get_state()
            if state.get("seed") == seed and state.get("finished"):
                return self._json(
                    {
                        "seed": seed,
                        "finished": True,
                        "total": state.get("remaining_count", 0),
                        "fish": state.get("remaining_fish", []),
                    }
                )
            disk = _remaining_from_disk(seed)
            if disk["fish"]:
                return self._json(disk)
            return self._json({"seed": seed, "finished": False, "total": 0, "fish": []})

        if path == "/api/report":
            seed = int(qs.get("seed", [DEFAULT_SEED])[0])
            for name in (f"run_report_seed_{seed}.csv",):
                report = ROOT_DIR / "data" / name
                if report.is_file():
                    return self._file(report)
            return self._json({"error": "报告尚未生成"}, 404)

        if path == "/api/cartons.csv":
            seed = int(qs.get("seed", [DEFAULT_SEED])[0])
            report = ROOT_DIR / "data" / f"cartons_seed_{seed}.csv"
            if not report.is_file():
                return self._json({"error": "成盒记录尚未生成"}, 404)
            return self._file(report)

        if path == "/api/remaining.csv":
            seed = int(qs.get("seed", [DEFAULT_SEED])[0])
            report = ROOT_DIR / "data" / f"remaining_seed_{seed}.csv"
            if not report.is_file():
                return self._json({"error": "尾料记录尚未生成"}, 404)
            return self._file(report)

        static = ROOT_DIR / "data" / path.lstrip("/")
        if path.startswith("/data/") and static.is_file():
            return self._file(static)

        static = SRC_DIR / path.lstrip("/")
        if static.is_file():
            return self._file(static)

        self._json(
            {
                "error": "Not Found",
                "path": path,
                "hint": "请通过 web_server.py 启动服务，不要直接打开 html 文件",
                "routes": ["/", "/monitor", "/api/batch", "/api/config", "/api/state"],
            },
            404,
        )

    def do_POST(self) -> None:
        path = normalize_path(self.path)
        body = self._read_json()

        if path == "/api/start":
            try:
                RUNNER.start(
                    seed=int(body.get("seed", DEFAULT_SEED)),
                    total=int(body.get("total", DEFAULT_TOTAL)),
                    move_timeout=int(body.get("move_timeout", DEFAULT_MOVE_TIMEOUT)),
                    speed=float(body.get("speed", 10)),
                    enabled_specs=body.get("enabled_specs"),
                )
                return self._json({"ok": True, "state": RUNNER.get_state()})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)

        if path == "/api/pause":
            RUNNER.pause()
            return self._json({"ok": True, "state": RUNNER.get_state()})

        if path == "/api/resume":
            RUNNER.resume()
            return self._json({"ok": True, "state": RUNNER.get_state()})

        if path == "/api/stop":
            RUNNER.stop()
            return self._json({"ok": True, "state": RUNNER.get_state()})

        if path == "/api/speed":
            try:
                RUNNER.set_speed(float(body.get("speed", 10)))
                return self._json({"ok": True, "state": RUNNER.get_state()})
            except (TypeError, ValueError) as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)

        self._json({"error": "Not Found", "path": path}, 404)


def main() -> None:
    import os

    port = int(os.environ.get("WEB_PORT", "8765"))
    monitor = SRC_DIR / "fifo_monitor.html"
    index = SRC_DIR / "index.html"
    if not index.is_file() or not monitor.is_file():
        print(f"错误: 缺少页面文件\n  index: {index}\n  monitor: {monitor}")
        sys.exit(1)

    try:
        server = ReusableHTTPServer(("0.0.0.0", port), Handler)
    except OSError as exc:
        print(f"错误: 端口 {port} 已被占用 ({exc})")
        print("请先关闭旧的 web_server 进程，再重新启动。")
        print("PowerShell: Get-NetTCPConnection -LocalPort 8765 | Select OwningProcess")
        print("           taskkill /PID <进程号> /F")
        sys.exit(1)

    print(f"web_server v{SERVER_VERSION}  工作目录: {SRC_DIR}")
    print(f"统计面板: http://0.0.0.0:{port}/")
    print(f"FIFO 动画: http://0.0.0.0:{port}/monitor")
    print(f"版本检查: http://0.0.0.0:{port}/api/version")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        RUNNER.stop()


if __name__ == "__main__":
    main()
