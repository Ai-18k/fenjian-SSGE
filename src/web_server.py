#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project : kaomam_project
@File : web_server.py
@Description: 智能分拣 Web 可视化服务 — 启动后浏览器打开 http://127.0.0.1:8765
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
    DEFAULT_MOVE_TIMEOUT,
    DEFAULT_SEED,
    DEFAULT_TOTAL,
    MODULE_SPECS,
    TARGET_MAX,
    TARGET_MIN,
    SchedulerEngine,
    load_or_generate_batch,
)


def normalize_path(raw: str) -> str:
    """统一 URI 路径，避免尾斜杠等导致 404。"""
    p = unquote(urlparse(raw).path or "/")
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return p


SERVER_VERSION = "2.1"


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
        self.interval = 0.05
        self.speed = 10.0
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
    ) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError("模拟已在运行")
            self.stop_flag = False
            self.paused = False
            self.error = None
            self.speed = max(0.1, speed)
            records = load_or_generate_batch(seed=seed, total=total)
            self.engine = SchedulerEngine(
                batch_records=records,
                seed=seed,
                move_timeout=move_timeout,
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
                time.sleep(self.interval / self.speed)
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
        finally:
            with self.lock:
                self.running = False

    def pause(self) -> None:
        with self.lock:
            self.paused = True

    def resume(self) -> None:
        with self.lock:
            self.paused = False

    def stop(self) -> None:
        with self.lock:
            self.stop_flag = True
            self.paused = False


RUNNER = SimulationRunner()


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
                    "routes": ["/", "/monitor", "/api/batch", "/api/config", "/api/state"],
                    "monitor_file": str(SRC_DIR / "fifo_monitor.html"),
                    "monitor_exists": (SRC_DIR / "fifo_monitor.html").is_file(),
                }
            )

        if path == "/api/batch":
            seed = int(qs.get("seed", [DEFAULT_SEED])[0])
            total = int(qs.get("total", [DEFAULT_TOTAL])[0])
            records = load_or_generate_batch(seed=seed, total=total)
            payload = [
                {
                    "id": r.id,
                    "weight": r.weight,
                    "spec": r.spec or "",
                    "outside": bool(r.outside),
                }
                for r in records[:total]
            ]
            return self._json({"seed": seed, "total": len(payload), "fish": payload})

        if path == "/api/config":
            return self._json(
                {
                    "default_seed": DEFAULT_SEED,
                    "default_total": DEFAULT_TOTAL,
                    "default_move_timeout": DEFAULT_MOVE_TIMEOUT,
                    "target_min": TARGET_MIN,
                    "target_max": TARGET_MAX,
                    "modules": {k: list(v) for k, v in MODULE_SPECS.items()},
                }
            )

        if path == "/api/state":
            return self._json(RUNNER.get_state())

        if path == "/api/report":
            seed = int(qs.get("seed", [DEFAULT_SEED])[0])
            report = ROOT_DIR / "data" / f"run_report_seed_{seed}.csv"
            if not report.is_file():
                return self._json({"error": "报告尚未生成"}, 404)
            return self._file(report)

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
