import json
import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import redis
import logging

Fish = Dict[str, Any]


# 配置日志格式、级别和输出目标
logging.basicConfig(
    level=logging.INFO,  # 设置日志级别
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),  # 输出到控制台
        logging.FileHandler("app.log", encoding="utf-8")  # 可选：同时输出到文件
    ]
)

class Spec:
    def __init__(self, specs: Dict[str, Dict[str, Any]]):
        self.specs = specs
        self.logger = logging.getLogger(self.__class__.__name__)
        self.r = redis.Redis(
            host="localhost",
            port=10238,
            db=5,
            password="vi4*87taTZBel&DyWL)A",
            decode_responses=True,
        )

    def connect(self) -> bool:
        try:
            self.r.ping()
            return True
        except redis.ConnectionError:
            self.logger.info("Redis连接失败")
            return False

    def push_json(self, queue_name: str, data: Any) -> None:
        self.r.rpush(queue_name, json.dumps(data, ensure_ascii=False))

    def clear_queue(self, queue_name: str) -> None:
        self.r.delete(queue_name)

    def pop_from_queue(self, queue_name: str) -> Optional[Any]:
        try:
            result = self.r.lpop(queue_name)
            if result is None:
                return None
            try:
                return json.loads(result)
            except ValueError:
                return None
        except Exception as e:
            self.logger.info(f"从队列弹出数据失败: {e}")
            return None

TARGET_WEIGHT = 10_000_000 # 目标总重量
FIXED_START="100p" # 固定开始规格
TIMEOUT = 6
TIMEOUT_WINDOWS = (240,300,360,420,480,540,600)# 超时统计窗口
class ImprovedSpecLineOptimization(Spec):
    """
    改动点：
    1. 不再使用 raw_queue 和规格产线 Redis 队列；生成一条鱼就立刻分拣并进入对应规格算法。
    2. 低规格直接进入缓存池 DP 配重；高规格直接进入候选盒路线判断。
    3. 回流鱼保存在内存延迟队列中，模拟回流到后方 30、50、100 个处理位置。
    4. 超时统计：按分拣口全局序号登记；某条鱼出来后，后续再出来 180 条仍未成箱/未完成则记录。
    """

    SPECS: Dict[str, Dict[str, Any]] = {
        "15p": {"range": (566, 700), "counts": [7,8,9], "cache_number": 25, "box_number": 1},
        "20p": {"range": (446, 565), "counts": [9,10,11], "cache_number": 26, "box_number": 1},   
        "25p": {"range": (366, 445), "counts": [11,12,13], "cache_number": 28, "box_number": 1},  
        "30p": {"range": (306, 365), "counts": [14,15,16], "cache_number": 30, "box_number": 1},  
        "35p": {"range": (266, 305), "counts": [16,17,18], "cache_number": 30, "box_number": 1},  
        "40p": {"range": (231, 265), "counts": [19,20,21], "cache_number": 35, "box_number": 1},  
        "45p": {"range": (211, 230), "counts": [22,23], "cache_number": 35, "box_number": 1},     
        "50p": {"range": (183, 210), "counts": [24,25,26], "cache_number": 35, "box_number": 1},
        "60p": {"range": (153, 182), "counts": [29,30,31], "cache_number": 35, "box_number": 1},
         "70p": {"range": (133, 152), "counts": [34,35, 36], "cache_number": 20, "box_number": 1, "algorithm": "high_hybrid"},
        "80p": {"range": (116, 132), "counts": [39,40, 41], "cache_number": 20, "box_number": 1, "algorithm": "high_hybrid"},
        "90p": {"range": (106, 115), "counts": [44,45, 46], "cache_number": 20, "box_number": 1, "algorithm": "high_hybrid"},
        "100p": {"range": (96, 105), "counts": [49,50, 51], "cache_number": 30, "box_number": 1, "algorithm": "high_hybrid"},
        "110p": {"range": (87, 95), "counts": [54,55, 56], "cache_number": 30, "box_number": 1, "algorithm": "high_hybrid"},
        "120p": {"range": (80, 86), "counts": [60, 61], "cache_number": 30, "box_number": 1, "algorithm": "high_hybrid"},
        "130p": {"range": (74, 79), "counts": [64,65, 66], "cache_number": 30, "box_number": 1, "algorithm": "high_hybrid"},
        "140p": {"range": (69, 73), "counts": [69,70, 71], "cache_number": 30, "box_number": 1, "algorithm": "high_hybrid"},
        "150p": {"range": (65, 68), "counts": [74,75, 76], "cache_number": 30, "box_number": 1, "algorithm": "high_hybrid"},
    }
    TARGET_MID = 5005
    TARGET_RANGE = (4980, 5030)
    SELECTED_SPEC_COUNT = 6 # 选择规格数

    # 同一条鱼最多允许释放回流 3 次；第 4 次进入未完成鱼队列。
    MAX_RELEASE_COUNT = 5

    # 回流插入位置：不是队尾，而是模拟真实产线回流到后面某个位置。
    REFLOW_INSERT_POSITIONS = (25, 35, 50)


    # 分拣口后续再出来 180 条鱼仍未有最终归宿，就记录一次超时。
    TIMEOUT_FOLLOWING_FISH_COUNT = TIMEOUT
    TIMEOUT_WINDOWS = TIMEOUT_WINDOWS # 超时统计窗口


    MAIN_SPEC_RATIO = 0.99

    # 高规格一箱需要 50-75 条鱼。先锁定候选箱归属，再继续凑真实成箱重量，
    # 避免鱼已经稳定进入某个箱却在超时统计里长期显示未归属。
    HIGH_PRELOAD_MIN_RATIO = 0.70
    HIGH_PRELOAD_MAX_RATIO = 0.80


    other_queue = "fish_other"
    result_prefix = "fish_results"
    unfinished_prefix = "fish_results_unfinished"
    timeout_prefix = "fish_timeout"

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        super().__init__(self.SPECS)

        self.logger = logging.getLogger(self.__class__.__name__)

        self.stats_lock = threading.Lock()

        self.stats: Dict[str, int] = {
            "generated_fish": 0,
            "generated_weight": 0,
            "sorter_to_other_fish": 0,
            "algorithm_to_other_fish": 0,
            "low_finished_boxes": 0,
            "high_finished_boxes": 0,
            "finished_boxes": 0,
            "unfinished_boxes": 0,
            "unfinished_box_fish": 0,
            "total_reflow_fish" : 0,
            "low_released_fish": 0,
            "high_released_fish": 0,
            "release_limit_fish": 0,
            "timeout_fish": 0,
            "timeout_fish_300": 0,
            "timeout_fish_480": 0,
            "reflow_insert_30": 0,
            "reflow_insert_50": 0,
            "reflow_insert_100": 0,
        }
        self.spec_stats: Dict[str, Dict[str, int]] = {}
        self.current_specs: Dict[str, Dict[str, Any]] = {}
        self.sort_seq_by_spec: Dict[str, int] = {}
        self.line_seq_by_spec: Dict[str, int] = {}
        self.spec_runtime: Dict[str, Dict[str, Any]] = {}
        self.reflow_pending_by_spec: Dict[str, Dict[int, List[Fish]]] = {}
        self.timeout_lock = threading.Lock()
        self.sorter_outlet_seq = 0
        self.timeout_checked_until_seq = 0
        self.fish_status_by_id: Dict[str, Dict[str, Any]] = {}
        self.timeout_deadlines: Dict[int, List[Tuple[str, int]]] = {}

    # ---------- 队列名 ----------

    def result_queue_name(self, spec_key: str) -> str:
        return f"{self.result_prefix}:{spec_key}"

    def unfinished_queue_name(self, spec_key: str) -> str:
        return f"{self.unfinished_prefix}:{spec_key}"

    def timeout_queue_name(self, spec_key: str) -> str:
        return f"{self.timeout_prefix}:{spec_key}"

    # ---------- 统计 ----------

    def ensure_spec_stats(self, spec_key: str) -> None:
        if spec_key in self.spec_stats:
            return
        self.spec_stats[spec_key] = {
            "sorter_in": 0,
            "finished_boxes": 0,
            "finished_fish": 0,
            "unfinished_boxes": 0,
            "unfinished_box_fish": 0,
            "released_fish": 0,
            "release_limit_fish": 0,
            "timeout_fish": 0,
            "timeout_fish_300": 0,
            "timeout_fish_480": 0,
            "algorithm_to_other_fish": 0,
        }

    def inc_stat(self, key: str, amount: int = 1) -> None:
        with self.stats_lock:
            self.stats[key] = self.stats.get(key, 0) + amount

    def inc_spec_stat(self, spec_key: str, key: str, amount: int = 1) -> None:
        with self.stats_lock:
            self.ensure_spec_stats(spec_key)
            self.spec_stats[spec_key][key] = self.spec_stats[spec_key].get(key, 0) + amount

    def inc_both(self, global_key: str, spec_key: str, spec_key_name: str, amount: int = 1) -> None:
        with self.stats_lock:
            self.stats[global_key] = self.stats.get(global_key, 0) + amount
            self.ensure_spec_stats(spec_key)
            self.spec_stats[spec_key][spec_key_name] = self.spec_stats[spec_key].get(spec_key_name, 0) + amount

    def reset_timeout_state(self) -> None:
        with self.timeout_lock:
            self.sorter_outlet_seq = 0
            self.timeout_checked_until_seq = 0
            self.fish_status_by_id.clear()
            self.timeout_deadlines.clear()

    # ---------- 清理 ----------

    def clear_runtime_queues(self) -> None:
        # 旧版本遗留的中转队列只清理，不再写入。
        keys = ["fish_raw_queue", self.other_queue]
        for spec_key in self.SPECS:
            keys.append(self.result_queue_name(spec_key))
            keys.append(self.unfinished_queue_name(spec_key))
            keys.append(self.timeout_queue_name(spec_key))
            keys.append(f"fish_specs_assembly:{spec_key}")
            keys.append(f"fish_sorted:{spec_key}")
        self.r.delete(*keys)

    # ---------- 生成与分拣 ----------

    def setup_selected_specs(self, fixed_start: Optional[str] = None) -> List[str]:
        all_specs = list(self.SPECS.keys())
        if fixed_start is not None:
            if fixed_start not in all_specs:
                raise ValueError(f"规格 {fixed_start} 不存在")
            start_index = all_specs.index(fixed_start)
            selected_specs = []
            for i in range(self.SELECTED_SPEC_COUNT):
                selected_specs.append(all_specs[(start_index + i) % len(all_specs)])
            return selected_specs

        max_start_index = len(all_specs) - self.SELECTED_SPEC_COUNT
        start_index = random.randint(0, max_start_index)
        return all_specs[start_index:start_index + self.SELECTED_SPEC_COUNT]

    def init_spec_runtime(self, selected_specs: List[str]) -> None:
        self.spec_runtime = {}
        self.reflow_pending_by_spec = {}
        self.sort_seq_by_spec = {}
        self.line_seq_by_spec = {}
        for worker_id, spec_key in enumerate(selected_specs):
            self.ensure_spec_stats(spec_key)
            spec_data = self.SPECS[spec_key]
            box_number = int(spec_data.get("box_number", 0) or 0)
            self.sort_seq_by_spec[spec_key] = 0
            self.line_seq_by_spec[spec_key] = 0
            self.reflow_pending_by_spec[spec_key] = {}
            self.spec_runtime[spec_key] = {
                "worker_id": worker_id,
                "cache_pool": [],
                "boxes": [[] for _ in range(max(1, box_number))],
            }

    def make_fish(self, fish_count: int, selected_specs: List[str], all_specs: List[str]) -> Fish:
        if random.random() < self.MAIN_SPEC_RATIO:
            source_spec = random.choice(selected_specs)
        else:
            source_spec = random.choice(all_specs)

        min_w, max_w = self.SPECS[source_spec]["range"]
        fish_weight = random.randint(min_w, max_w)
        return {
            "id": f"Fish_{fish_count}",
            "weight": fish_weight,
            "source_spec": source_spec,
            "range": self.SPECS[source_spec]["range"],
            "created_at": time.time(),
            "release_count": 0,
            "timeout_recorded": False,
        }

    def generate_and_process_fish(
        self,
        target_weight: int = 10_000_000,
        fixed_start: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> List[str]:
        if seed is not None:
            random.seed(seed)

        selected_specs = self.setup_selected_specs(fixed_start=fixed_start)
        all_specs = list(self.SPECS.keys())
        total_generated_weight = 0
        fish_count = 0

        self.current_specs = {}
        for spec_key in selected_specs:
            self.current_specs[spec_key] = self.SPECS[spec_key].copy()
        self.init_spec_runtime(selected_specs)

        while total_generated_weight < target_weight:
            fish_count += 1
            fish = self.make_fish(fish_count, selected_specs, all_specs)
            total_generated_weight += self.fish_weight(fish)
            self.route_generated_fish(fish)

            if self.verbose and fish_count % 500 == 0:
                self.logger.info(f"已生成 {fish_count} 条，累计重量 {total_generated_weight}g / {target_weight}g")

        self.finish_all_specs()

        with self.stats_lock:
            self.stats["generated_fish"] = fish_count
            self.stats["generated_weight"] = total_generated_weight

        self.logger.info(f"生成完成：共 {fish_count} 条鱼，总重量 {total_generated_weight}g")
        return selected_specs

    def find_selected_spec(self, weight: int) -> Optional[str]:
        for spec_key, spec_data in self.current_specs.items():
            min_weight, max_weight = spec_data["range"]
            if min_weight <= weight <= max_weight:
                return spec_key
        return None

    def route_generated_fish(self, fish: Fish) -> None:
        self.sorter_outlet_seq += 1
        current_sorter_outlet_seq = self.sorter_outlet_seq
        fish["sorter_outlet_seq"] = current_sorter_outlet_seq
        fish["sorter_outlet_at"] = time.time()

        weight = int(fish.get("weight", 0))
        spec_key = self.find_selected_spec(weight)

        if spec_key is None:
            fish["reason"] = "not_in_selected_specs"
            fish["other_at"] = time.time()
            self.push_json(self.other_queue, fish)
            self.inc_stat("sorter_to_other_fish")
            return

        self.sort_seq_by_spec[spec_key] = self.sort_seq_by_spec.get(spec_key, 0) + 1
        if self.sort_seq_by_spec["100p"]==50:
            pass
        seq = self.sort_seq_by_spec[spec_key]
        fish["matched_spec"] = spec_key
        fish["sorted_at"] = time.time()
        fish["sorted_seq_in_spec"] = seq
        fish["first_sort_out"] = seq == 1

        self.register_timeout_fish(spec_key, fish)
        self.inc_spec_stat(spec_key, "sorter_in")
        self.process_fish_for_spec(spec_key, fish)
        self.process_due_reflow(spec_key)
        self.record_due_timeout_fish(current_sorter_outlet_seq)

    # ---------- 通用工具 ----------

    def fish_weight(self, fish: Fish) -> int:
        return int(fish["weight"])

    def box_weights(self, box: List[Fish]) -> List[int]:
        return [self.fish_weight(fish) for fish in box]

    def remove_fish_from_pool(self, pool: List[Fish], selected: List[Fish]) -> None:
        selected_ids = {fish.get("id") for fish in selected}
        pool[:] = [fish for fish in pool if fish.get("id") not in selected_ids]

    # ---------- 鱼最终状态与180条到期记录 ----------

    def register_timeout_fish(self, spec_key: str, fish: Fish) -> None:
        fish_id = str(fish.get("id", ""))
        sorter_outlet_seq = int(fish.get("sorter_outlet_seq", 0) or 0)
        sorted_seq_in_spec = int(fish.get("sorted_seq_in_spec", 0) or 0)
        if not fish_id or sorter_outlet_seq <= 0:
            return

        timeout_recorded_by_window = {
            int(window): False for window in self.TIMEOUT_WINDOWS
        }
        timeout_deadline_by_window = {
            int(window): sorter_outlet_seq + int(window)
            for window in self.TIMEOUT_WINDOWS
        }
        primary_deadline_seq = timeout_deadline_by_window.get(
            self.TIMEOUT_FOLLOWING_FISH_COUNT,
            sorter_outlet_seq + self.TIMEOUT_FOLLOWING_FISH_COUNT,
        )
        fish["timeout_deadline_sorter_outlet_seq"] = primary_deadline_seq
        fish["timeout_deadlines_by_window"] = timeout_deadline_by_window
        with self.timeout_lock:
            self.fish_status_by_id[fish_id] = {
                "fish_id": fish_id,
                "spec_key": spec_key,
                "weight": fish.get("weight"),
                "sorter_outlet_seq": sorter_outlet_seq,
                "sorted_seq_in_spec": sorted_seq_in_spec,
                "deadline_sorter_outlet_seq": primary_deadline_seq,
                "timeout_deadlines_by_window": timeout_deadline_by_window,
                "timeout_recorded_by_window": timeout_recorded_by_window,
                "final": False,
                "final_type": "",
                "timeout_recorded": False,
                "release_count": int(fish.get("release_count", 0) or 0),
            }
            for window, deadline_seq in timeout_deadline_by_window.items():
                self.timeout_deadlines.setdefault(deadline_seq, []).append((fish_id, window))

    def mark_fish_final(self, spec_key: str, fish: Fish, final_type: str) -> None:
        fish_id = str(fish.get("id", ""))
        if not fish_id:
            return

        with self.timeout_lock:
            status = self.fish_status_by_id.setdefault(fish_id, {
                "fish_id": fish_id,
                "spec_key": spec_key,
                "weight": fish.get("weight"),
                "sorter_outlet_seq": int(fish.get("sorter_outlet_seq", 0) or 0),
                "sorted_seq_in_spec": int(fish.get("sorted_seq_in_spec", 0) or 0),
                "deadline_sorter_outlet_seq": int(
                    fish.get("timeout_deadline_sorter_outlet_seq", 0) or 0
                ),
                "timeout_deadlines_by_window": fish.get("timeout_deadlines_by_window", {}),
                "timeout_recorded_by_window": {
                    int(window): False for window in self.TIMEOUT_WINDOWS
                },
                "final": False,
                "final_type": "",
                "timeout_recorded": bool(fish.get("timeout_recorded", False)),
                "release_count": int(fish.get("release_count", 0) or 0),
            })
            status["final"] = True
            status["final_type"] = final_type
            status["final_at"] = time.time()
            status["release_count"] = int(fish.get("release_count", status.get("release_count", 0)) or 0)
            fish["final"] = True
            fish["final_type"] = final_type

    def update_fish_release_status(self, spec_key: str, fish: Fish) -> None:
        fish_id = str(fish.get("id", ""))
        if not fish_id:
            return
        with self.timeout_lock:
            status = self.fish_status_by_id.get(fish_id)
            if status is None:
                return
            status["release_count"] = int(fish.get("release_count", 0) or 0)
            status["last_release_reason"] = fish.get("release_reason", "")
            status["last_release_mode"] = fish.get("release_mode", "")
            status["last_release_at"] = fish.get("released_at")

    def record_due_timeout_fish(self, current_sorter_outlet_seq: int) -> None:
        records: List[Dict[str, Any]] = []

        with self.timeout_lock:
            if current_sorter_outlet_seq <= self.timeout_checked_until_seq:
                return

            for deadline_seq in range(self.timeout_checked_until_seq + 1, current_sorter_outlet_seq + 1):
                due_items = self.timeout_deadlines.pop(deadline_seq, [])
                for due_item in due_items:
                    if isinstance(due_item, tuple):
                        fish_id, timeout_window = due_item
                    else:
                        fish_id = due_item
                        timeout_window = self.TIMEOUT_FOLLOWING_FISH_COUNT

                    status = self.fish_status_by_id.get(fish_id)
                    if status is None:
                        continue
                    if status.get("final"):
                        continue
                    timeout_recorded_by_window = status.setdefault(
                        "timeout_recorded_by_window",
                        {int(window): False for window in self.TIMEOUT_WINDOWS},
                    )
                    timeout_window = int(timeout_window)
                    if timeout_recorded_by_window.get(timeout_window):
                        continue

                    sorter_outlet_seq = int(status.get("sorter_outlet_seq", 0) or 0)
                    following_count = current_sorter_outlet_seq - sorter_outlet_seq
                    timeout_recorded_by_window[timeout_window] = True
                    if timeout_window == self.TIMEOUT_FOLLOWING_FISH_COUNT:
                        status["timeout_recorded"] = True
                    status["timeout_at"] = time.time()
                    status["timeout_current_sorter_outlet_seq"] = current_sorter_outlet_seq
                    status["timeout_following_fish_count"] = following_count

                    records.append({
                        "record_type": f"single_fish_sorter_outlet_wait_{timeout_window}",
                        "timeout_window": timeout_window,
                        "spec_key": status.get("spec_key"),
                        "fish_id": status.get("fish_id"),
                        "weight": status.get("weight"),
                        "sorter_outlet_seq": sorter_outlet_seq,
                        "sorted_seq_in_spec": status.get("sorted_seq_in_spec", 0),
                        "deadline_sorter_outlet_seq": deadline_seq,
                        "current_sorter_outlet_seq": current_sorter_outlet_seq,
                        "following_fish_count": following_count,
                        "final": False,
                        "final_type": "",
                        "release_count": status.get("release_count", 0),
                        "created_at": time.time(),
                    })

            self.timeout_checked_until_seq = current_sorter_outlet_seq

        for record in records:
            record_spec_key = str(record.get("spec_key", ""))
            if not record_spec_key:
                continue
            self.push_json(self.timeout_queue_name(record_spec_key), record)
            timeout_window = int(record.get("timeout_window", self.TIMEOUT_FOLLOWING_FISH_COUNT) or 0)
            self.inc_both(f"timeout_fish_{timeout_window}", record_spec_key, f"timeout_fish_{timeout_window}")
            if timeout_window == self.TIMEOUT_FOLLOWING_FISH_COUNT:
                self.inc_both("timeout_fish", record_spec_key, "timeout_fish")

    # ---------- 保存结果 ----------

    def save_box(
        self,
        worker_id: int,
        spec_key: str,
        box: List[Fish],
        finished: bool,
        close_reason: str,
        algorithm: str,
    ) -> None:
        if not box:
            return

        weights = self.box_weights(box)
        result = {
            "worker_id": worker_id,
            "spec_key": spec_key,
            "algorithm": algorithm,
            "fish_ids": [fish.get("id", "") for fish in box],
            "weights": weights,
            "box_size": len(box),
            "total_weight": sum(weights),
            "avg_weight": round(sum(weights) / len(box), 2),
            "finished": finished,
            "close_reason": close_reason,
            "target_range": self.TARGET_RANGE,
            "created_at": time.time(),
        }

        if finished:
            for fish in box:
                self.mark_fish_final(spec_key, fish, "finished_box")
            self.push_json(self.result_queue_name(spec_key), result)
            if algorithm == "low_dp_cache":
                self.inc_stat("low_finished_boxes")
            else:
                self.inc_stat("high_finished_boxes")
            self.inc_both("finished_boxes", spec_key, "finished_boxes")
            self.inc_spec_stat(spec_key, "finished_fish", len(box))
        else:
            for fish in box:
                self.mark_fish_final(spec_key, fish, "unfinished_box")
            self.push_json(self.unfinished_queue_name(spec_key), result)
            self.inc_both("unfinished_boxes", spec_key, "unfinished_boxes")
            self.inc_both("unfinished_box_fish", spec_key, "unfinished_box_fish", len(box))

    # ---------- 回流 ----------

    def release_fish_to_spec_position(
        self,
        fish: Fish,
        spec_key: str,
        reason: str,
        mode: str,
    ) -> bool:
        """
        返回 True 表示成功回流；False 表示超过回流次数，进入未完成鱼队列。
        """
        fish["release_reason"] = reason
        fish["release_spec"] = spec_key
        fish["release_mode"] = mode
        fish["release_count"] = int(fish.get("release_count", 0)) + 1
        fish["released_at"] = time.time()

        if fish["release_count"] > self.MAX_RELEASE_COUNT:
            fish["unfinished_reason"] = "release_limit_reached"
            fish["unfinished_at"] = time.time()
            fish["unfinished_type"] = "single_fish_release_limit"
            self.mark_fish_final(spec_key, fish, "release_limit")
            self.push_json(self.unfinished_queue_name(spec_key), fish)
            self.inc_both("release_limit_fish", spec_key, "release_limit_fish")
            return False

        insert_position = random.choice(self.REFLOW_INSERT_POSITIONS)
        fish["reflow_insert_position"] = insert_position
        current_line_seq = int(self.line_seq_by_spec.get(spec_key, 0) or 0)
        fish["reflow_due_line_seq"] = current_line_seq + insert_position
        self.update_fish_release_status(spec_key, fish)
        self.reflow_pending_by_spec.setdefault(spec_key, {}).setdefault(
            fish["reflow_due_line_seq"],
            [],
        ).append(fish)

        self.inc_stat(f"reflow_insert_{insert_position}")
        self.inc_spec_stat(spec_key, "released_fish")
        return True

    # ---------- 回流（低级） ----------
    def evict_one_low_pool_fish(self, pool: List[Fish], counts: List[int], spec_key: str) -> None:
        if not pool:
            return
        
        # 多驱逐策略：优先驱逐离理想重量最远的 + 最老的鱼
        ideal_count = min(counts, key=lambda c: abs(c - len(pool)))
        ideal_w_per_fish = self.TARGET_MID / ideal_count
        
        # 综合打分驱逐
        def eviction_score(fish, idx):
            w_diff = abs(self.fish_weight(fish) - ideal_w_per_fish)
            age = idx  # 越老越优先驱逐
            return w_diff + age * 0.5
        
        evict_idx = max(range(len(pool)), key=lambda i: eviction_score(pool[i], i))
        fish = pool.pop(evict_idx)
        
        requeued = self.release_fish_to_spec_position(fish, spec_key, "low_cache_full", "low_dp_cache")
        if requeued:
            self.inc_stat("low_released_fish")
            self.inc_stat("total_reflow_fish")
    # ---------- 回流（高级） ----------
    def release_all_high_boxes(
        self,
        boxes: List[List[Fish]],
        spec_key: str,
        current_fish: Fish,
    ) -> None:
        released_count = 0
        requeued_current = self.release_fish_to_spec_position(
            fish=current_fish,
            spec_key=spec_key,
            reason="high_all_boxes_failed_current_fish",
            mode="high_best_route",
        )
        if requeued_current:
            released_count += 1

        for box in boxes:
            for fish in box:
                requeued = self.release_fish_to_spec_position(
                    fish=fish,
                    spec_key=spec_key,
                    reason="high_all_boxes_failed_box_fish",
                    mode="high_best_route",
                )
                if requeued:
                    released_count += 1
            box.clear()

        self.inc_stat("high_released_fish", released_count)
        self.inc_stat("total_reflow_fish")

    def release_high_box_fish(
        self,
        boxes: List[List[Fish]],
        spec_key: str,
        reason: str,
        mode: str,
    ) -> None:
        released_count = 0
        for box in boxes:
            for fish in box:
                requeued = self.release_fish_to_spec_position(
                    fish=fish,
                    spec_key=spec_key,
                    reason=reason,
                    mode=mode,
                )
                if requeued:
                    released_count += 1
            box.clear()

        if released_count:
            self.inc_stat("high_released_fish", released_count)
            self.inc_stat("total_reflow_fish", released_count)

    # ---------- 低规格 DP ----------

    def find_box_combination_dp_for_range(
        self,
        pool: List[Fish],
        counts: List[int],
        target_range: Optional[Tuple[int, int]] = None,
        target_mid: Optional[int] = None,
    ) -> Optional[List[Fish]]:
        if not pool:
            return None
        
        min_target, max_target = target_range or self.TARGET_RANGE
        if target_mid is None:
            target_mid = int(round((min_target + max_target) / 2))
        weights = [self.fish_weight(f) for f in pool]
        n = len(weights)
        
        # 优先尝试最接近目标条数的 count
        preferred = int(round(target_mid / (sum(weights)/n))) if weights else counts[0]
        ordered_counts = sorted(counts, key=lambda c: (abs(c - preferred), c))
        
        for k in ordered_counts:
            if k > n:
                continue
                
            # 允许轻微超重（业务通常可接受 5030+一点）
            dp = [{} for _ in range(k + 1)]  # dp[c][sum] = mask
            dp[0][0] = 0
            
            for i, w in enumerate(weights):
                for c in range(min(k, i+1), 0, -1):
                    for prev_sum, mask in list(dp[c-1].items()):
                        new_sum = prev_sum + w
                        if new_sum > max_target + 100:  # 允许少量超
                            continue
                        if new_sum not in dp[c] or bin(mask).count('1') < bin(dp[c][new_sum]).count('1'):  # 优先鱼数少的
                            dp[c][new_sum] = mask | (1 << i)
            
            # 优先找最接近 TARGET_MID 的
            best_sum = None
            best_mask = None
            best_diff = float('inf')
            
            for s in dp[k]:
                diff = abs(s - target_mid)
                if diff < best_diff and min_target - 50 <= s <= max_target + 150:
                    best_diff = diff
                    best_sum = s
                    best_mask = dp[k][s]
            
            if best_mask is not None:
                return [pool[i] for i in range(n) if best_mask & (1 << i)]
        
        return None

    def find_box_combination_dp(self, pool: List[Fish], counts: List[int]) -> Optional[List[Fish]]:
        return self.find_box_combination_dp_for_range(
            pool=pool,
            counts=counts,
            target_range=self.TARGET_RANGE,
            target_mid=self.TARGET_MID,
        )


    # ---------- 高规格路线判断 ----------

    def best_route(
        self,
        weight_range: Tuple[int, int],
        count_range: List[int],
        fish_weight_range: Tuple[int, int],
        current_box_weights: List[int],
    ) -> Dict[str, Any]:
        target_min, target_max = weight_range
        fish_min, fish_max = fish_weight_range
        current_count = len(current_box_weights)
        current_weight = sum(current_box_weights)

        if current_count in count_range and target_min <= current_weight <= target_max:
            return {
                "save": True,
                "failed": False,
                "reason": None,
                "current_count": current_count,
                "current_weight": current_weight,
                "current_box": current_box_weights[:],
                "need": [],
                "next_weight_range": None,
                "overall_expected_range": None,
            }

        if current_count >= max(count_range):
            return {
                "save": False,
                "failed": True,
                "reason": "max_count_reached_but_weight_not_ok",
                "current_count": current_count,
                "current_weight": current_weight,
                "current_box": current_box_weights[:],
                "need": [],
                "next_weight_range": None,
                "overall_expected_range": None,
            }

        if current_weight > target_max:
            return {
                "save": False,
                "failed": True,
                "reason": "weight_over_max",
                "current_count": current_count,
                "current_weight": current_weight,
                "current_box": current_box_weights[:],
                "need": [],
                "next_weight_range": None,
                "overall_expected_range": None,
            }

        need = []
        valid_next_ranges = []

        for target_count in count_range:
            need_count = target_count - current_count
            if need_count <= 0:
                continue

            need_min = target_min - current_weight
            need_max = target_max - current_weight
            if need_max <= 0:
                continue

            remaining_min_possible = need_count * fish_min
            remaining_max_possible = need_count * fish_max
            route_possible = not (
                remaining_max_possible < need_min or remaining_min_possible > need_max
            )
            if not route_possible:
                need.append({
                    "target_count": target_count,
                    "need_count": need_count,
                    "valid": False,
                    "reason": "remaining_fish_total_range_not_intersect_target",
                    "need_weight_range": (need_min, need_max),
                    "remaining_possible_weight_range": (remaining_min_possible, remaining_max_possible),
                    "next_fish_range": None,
                    "avg_weight_range": None,
                    "expected_weight_range": None,
                })
                continue

            remain_after_next = need_count - 1
            next_min = target_min - current_weight - remain_after_next * fish_max
            next_max = target_max - current_weight - remain_after_next * fish_min
            expected_min = max(next_min, fish_min)
            expected_max = min(next_max, fish_max)

            if expected_min > expected_max:
                need.append({
                    "target_count": target_count,
                    "need_count": need_count,
                    "valid": False,
                    "reason": "next_fish_has_no_feasible_weight_range",
                    "need_weight_range": (need_min, need_max),
                    "remaining_possible_weight_range": (remaining_min_possible, remaining_max_possible),
                    "next_fish_range": None,
                    "avg_weight_range": (round(need_min / need_count, 2), round(need_max / need_count, 2)),
                    "expected_weight_range": None,
                })
                continue

            route = {
                "target_count": target_count,
                "need_count": need_count,
                "valid": True,
                "reason": None,
                "need_weight_range": (need_min, need_max),
                "remaining_possible_weight_range": (remaining_min_possible, remaining_max_possible),
                "avg_weight_range": (round(need_min / need_count, 2), round(need_max / need_count, 2)),
                "next_fish_range": (int(expected_min), int(expected_max)),
                "expected_weight_range": (int(expected_min), int(expected_max)),
            }
            need.append(route)
            valid_next_ranges.append(route["next_fish_range"])

        if valid_next_ranges:
            next_weight_range = (
                min(item[0] for item in valid_next_ranges),
                max(item[1] for item in valid_next_ranges),
            )
        else:
            next_weight_range = None

        return {
            "save": False,
            "failed": next_weight_range is None,
            "reason": None if next_weight_range else "no_valid_route",
            "current_count": current_count,
            "current_weight": current_weight,
            "current_box": current_box_weights[:],
            "need": need,
            "next_weight_range": next_weight_range,
            "overall_expected_range": next_weight_range,
        }

    def choose_best_high_box(
        self,
        boxes: List[List[Fish]],
        fish: Fish,
        count_range: List[int],
        fish_weight_range: Tuple[int, int],
    ) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
        fish_weight = self.fish_weight(fish)
        best_index = None
        best_verification = None
        best_score = None

        for index, box in enumerate(boxes):
            test_weights = self.box_weights(box) + [fish_weight]
            verification = self.best_route(
                weight_range=self.TARGET_RANGE,
                count_range=count_range,
                fish_weight_range=fish_weight_range,
                current_box_weights=test_weights,
            )
            if verification.get("failed"):
                continue
            if verification.get("save"):
                return index, verification

            next_range = verification.get("next_weight_range")
            if next_range is None:
                continue

            current_weight = sum(test_weights)
            current_count = len(test_weights)
            range_width = next_range[1] - next_range[0]
            score = (
                0 if box else 1,
                -current_count,
                abs(self.TARGET_MID - current_weight),
                range_width,
            )
            if best_score is None or score < best_score:
                best_score = score
                best_index = index
                best_verification = verification

        return best_index, best_verification

    # ---------- 即时配重 ----------

    def process_fish_for_spec(self, spec_key: str, fish: Fish) -> None:
        spec_data = self.current_specs[spec_key]
        fish_min, fish_max = spec_data["range"]
        weight = self.fish_weight(fish)
        if not (fish_min <= weight <= fish_max):
            fish["reason"] = "not_in_algorithm_spec_range"
            fish["algorithm_spec"] = spec_key
            fish["other_at"] = time.time()
            self.mark_fish_final(spec_key, fish, "algorithm_other")
            self.push_json(self.other_queue, fish)
            self.inc_both("algorithm_to_other_fish", spec_key, "algorithm_to_other_fish")
            return

        self.line_seq_by_spec[spec_key] = int(self.line_seq_by_spec.get(spec_key, 0) or 0) + 1

        algorithm = str(spec_data.get("algorithm", "") or "")
        if algorithm == "high_hybrid":
            self.process_high_hybrid_fish(spec_key, fish)
        elif spec_data.get("cache_number", 0) > 0:
            self.process_low_spec_fish(spec_key, fish)
        else:
            self.process_high_spec_fish(spec_key, fish)

    def process_low_spec_fish(self, spec_key: str, fish: Fish) -> None:
        runtime = self.spec_runtime[spec_key]
        spec_data = self.current_specs[spec_key]
        count_range = spec_data["counts"]
        cache_number = spec_data.get("cache_number", 0)
        cache_pool: List[Fish] = runtime["cache_pool"]

        cache_pool.append(fish)

        while len(cache_pool) >= min(count_range):
            selected = self.find_box_combination_dp(cache_pool, count_range)
            if selected is None:
                break

            self.remove_fish_from_pool(cache_pool, selected)
            self.save_box(
                worker_id=runtime["worker_id"],
                spec_key=spec_key,
                box=selected,
                finished=True,
                close_reason="low_cache_dp_finished",
                algorithm="low_dp_cache",
            )

        if len(cache_pool) >= cache_number:
            self.evict_one_low_pool_fish(
                pool=cache_pool,
                counts=count_range,
                spec_key=spec_key,
            )

    def process_high_spec_fish(self, spec_key: str, fish: Fish) -> None:
        runtime = self.spec_runtime[spec_key]
        spec_data = self.current_specs[spec_key]
        count_range = spec_data["counts"]
        fish_weight_range = spec_data["range"]
        boxes: List[List[Fish]] = runtime["boxes"]

        best_index, verification = self.choose_best_high_box(
            boxes=boxes,
            fish=fish,
            count_range=count_range,
            fish_weight_range=fish_weight_range,
        )

        if best_index is None:
            requeued = self.release_fish_to_spec_position(
                fish=fish,
                spec_key=spec_key,
                reason="high_current_fish_has_no_box_route",
                mode="high_best_route",
            )
            if requeued:
                self.inc_stat("high_released_fish")
                self.inc_stat("total_reflow_fish")
            return

        boxes[best_index].append(fish)
        if verification and verification.get("save"):
            self.save_box(
                worker_id=runtime["worker_id"],
                spec_key=spec_key,
                box=boxes[best_index][:],
                finished=True,
                close_reason="high_best_route_finished",
                algorithm="high_best_route",
            )
            boxes[best_index].clear()

    def process_high_hybrid_fish(self, spec_key: str, fish: Fish) -> None:
        runtime = self.spec_runtime[spec_key]
        spec_data = self.current_specs[spec_key]
        count_range = spec_data["counts"]
        fish_weight_range = spec_data["range"]
        boxes: List[List[Fish]] = runtime["boxes"]
        cache_pool: List[Fish] = runtime["cache_pool"]
        main_box = boxes[0]
        preload_min = int(round(self.TARGET_RANGE[0] * self.HIGH_PRELOAD_MIN_RATIO))
        preload_max = int(round(self.TARGET_RANGE[1] * self.HIGH_PRELOAD_MAX_RATIO))

        if main_box:
            main_weight = sum(self.box_weights(main_box))
            main_count = len(main_box)
            if main_weight >= self.TARGET_RANGE[0] and main_count in count_range:
                if main_weight <= self.TARGET_RANGE[1]:
                    self.save_box(
                        worker_id=runtime["worker_id"],
                        spec_key=spec_key,
                        box=main_box[:],
                        finished=True,
                        close_reason="high_hybrid_main_box_finished",
                        algorithm="high_hybrid",
                    )
                    main_box.clear()
                    self._finish_high_hybrid_from_cache(spec_key, runtime, count_range, fish_weight_range)
                    return
            if main_weight > self.TARGET_RANGE[1] or main_count > max(count_range):
                self.release_high_box_fish(
                    boxes=[main_box],
                    spec_key=spec_key,
                    reason="high_hybrid_main_box_over_limit",
                    mode="high_hybrid",
                )
                return

        if not main_box:
            main_box.append(fish)
        else:
            main_weight = sum(self.box_weights(main_box))
            if main_weight < preload_min:
                main_box.append(fish)
            else:
                cache_pool.append(fish)
                self._evict_high_cache_if_full(cache_pool, count_range, spec_key)

        self._try_complete_high_hybrid_box(
            spec_key=spec_key,
            runtime=runtime,
            count_range=count_range,
            fish_weight_range=fish_weight_range,
            preload_min=preload_min,
            preload_max=preload_max,
        )

    def _try_complete_high_hybrid_box(
        self,
        spec_key: str,
        runtime: Dict[str, Any],
        count_range: List[int],
        fish_weight_range: Tuple[int, int],
        preload_min: int,
        preload_max: int,
    ) -> None:
        main_box: List[Fish] = runtime["boxes"][0]
        cache_pool: List[Fish] = runtime["cache_pool"]
        if not main_box:
            return

        main_weight = sum(self.box_weights(main_box))
        main_count = len(main_box)

        if main_weight > self.TARGET_RANGE[1] or main_count > max(count_range):
            self.release_high_box_fish(
                boxes=[main_box],
                spec_key=spec_key,
                reason="high_hybrid_main_box_over_limit",
                mode="high_hybrid",
            )
            return

        if self.TARGET_RANGE[0] <= main_weight <= self.TARGET_RANGE[1] and main_count in count_range:
            self.save_box(
                worker_id=runtime["worker_id"],
                spec_key=spec_key,
                box=main_box[:],
                finished=True,
                close_reason="high_hybrid_main_box_direct_finished",
                algorithm="high_hybrid",
            )
            main_box.clear()
            self._finish_high_hybrid_from_cache(spec_key, runtime, count_range, fish_weight_range)
            return

        if main_weight < preload_min:
            return

        self._finish_high_hybrid_from_cache(spec_key, runtime, count_range, fish_weight_range)

    def _finish_high_hybrid_from_cache(
        self,
        spec_key: str,
        runtime: Dict[str, Any],
        count_range: List[int],
        fish_weight_range: Tuple[int, int],
    ) -> None:
        main_box: List[Fish] = runtime["boxes"][0]
        cache_pool: List[Fish] = runtime["cache_pool"]
        if not main_box:
            return

        main_weight = sum(self.box_weights(main_box))
        main_count = len(main_box)
        remaining_counts = [c - main_count for c in count_range if c > main_count]
        if not remaining_counts or not cache_pool:
            return

        remaining_range = (
            max(0, self.TARGET_RANGE[0] - main_weight),
            max(0, self.TARGET_RANGE[1] - main_weight),
        )
        selected = self.find_box_combination_dp_for_range(
            pool=cache_pool,
            counts=remaining_counts,
            target_range=remaining_range,
            target_mid=max(0, self.TARGET_MID - main_weight),
        )
        if selected is None:
            return

        self.remove_fish_from_pool(cache_pool, selected)
        finished_box = main_box[:] + selected
        self.save_box(
            worker_id=runtime["worker_id"],
            spec_key=spec_key,
            box=finished_box,
            finished=True,
            close_reason="high_hybrid_cache_finished",
            algorithm="high_hybrid",
        )
        main_box.clear()
        if cache_pool:
            self._try_start_next_high_hybrid_box(spec_key, runtime, count_range, fish_weight_range)

    def _try_start_next_high_hybrid_box(
        self,
        spec_key: str,
        runtime: Dict[str, Any],
        count_range: List[int],
        fish_weight_range: Tuple[int, int],
    ) -> None:
        main_box: List[Fish] = runtime["boxes"][0]
        cache_pool: List[Fish] = runtime["cache_pool"]
        if main_box or not cache_pool:
            return

        selected = self.find_box_combination_dp_for_range(
            pool=cache_pool,
            counts=count_range,
            target_range=self.TARGET_RANGE,
            target_mid=self.TARGET_MID,
        )
        if selected is None:
            return

        self.remove_fish_from_pool(cache_pool, selected)
        self.save_box(
            worker_id=runtime["worker_id"],
            spec_key=spec_key,
            box=selected,
            finished=True,
            close_reason="high_hybrid_cache_start_finished",
            algorithm="high_hybrid",
        )

    def _evict_high_cache_if_full(
        self,
        cache_pool: List[Fish],
        count_range: List[int],
        spec_key: str,
    ) -> None:
        cache_number = int(self.current_specs[spec_key].get("cache_number", 0) or 0)
        if cache_number <= 0 or len(cache_pool) < cache_number:
            return

        ideal_count = min(count_range, key=lambda c: abs(c - len(cache_pool)))
        ideal_w_per_fish = self.TARGET_MID / ideal_count

        def eviction_score(fish, idx):
            w_diff = abs(self.fish_weight(fish) - ideal_w_per_fish)
            age = idx
            return w_diff + age * 0.5

        evict_idx = max(range(len(cache_pool)), key=lambda i: eviction_score(cache_pool[i], i))
        fish = cache_pool.pop(evict_idx)
        requeued = self.release_fish_to_spec_position(
            fish,
            spec_key,
            "high_cache_full",
            "high_hybrid",
        )
        if requeued:
            self.inc_stat("high_released_fish")
            self.inc_stat("total_reflow_fish")

    def process_due_reflow(self, spec_key: str) -> None:
        pending = self.reflow_pending_by_spec.setdefault(spec_key, {})
        current_line_seq = int(self.line_seq_by_spec.get(spec_key, 0) or 0)
        due_sequences = sorted(seq for seq in pending if seq <= current_line_seq)
        for due_seq in due_sequences:
            due_fish = pending.pop(due_seq, [])
            for fish in due_fish:
                self.process_fish_for_spec(spec_key, fish)

    def flush_reflow_until_idle(self, spec_key: str) -> None:
        pending = self.reflow_pending_by_spec.setdefault(spec_key, {})
        while pending:
            next_due_seq = min(pending)
            if self.line_seq_by_spec.get(spec_key, 0) < next_due_seq:
                self.line_seq_by_spec[spec_key] = next_due_seq
            self.process_due_reflow(spec_key)

    def finish_spec(self, spec_key: str) -> None:
        self.flush_reflow_until_idle(spec_key)

        runtime = self.spec_runtime[spec_key]
        spec_data = self.current_specs[spec_key]
        count_range = spec_data["counts"]
        algorithm = str(spec_data.get("algorithm", "") or "")

        if algorithm == "high_hybrid":
            fish_weight_range = spec_data["range"]
            self._finish_high_hybrid_from_cache(spec_key, runtime, count_range, fish_weight_range)
            boxes: List[List[Fish]] = runtime["boxes"]
            main_box: List[Fish] = boxes[0]
            cache_pool: List[Fish] = runtime["cache_pool"]
            if main_box:
                self.save_box(
                    worker_id=runtime["worker_id"],
                    spec_key=spec_key,
                    box=main_box[:],
                    finished=False,
                    close_reason="generation_done_high_hybrid_main_remaining",
                    algorithm="high_hybrid",
                )
                main_box.clear()
            if cache_pool:
                self.save_box(
                    worker_id=runtime["worker_id"],
                    spec_key=spec_key,
                    box=cache_pool[:],
                    finished=False,
                    close_reason="generation_done_high_hybrid_cache_remaining",
                    algorithm="high_hybrid",
                )
                cache_pool.clear()
            return

        if spec_data.get("cache_number", 0) > 0:
            cache_pool: List[Fish] = runtime["cache_pool"]
            while len(cache_pool) >= min(count_range):
                selected = self.find_box_combination_dp(cache_pool, count_range)
                if selected is None:
                    break
                self.remove_fish_from_pool(cache_pool, selected)
                self.save_box(
                    worker_id=runtime["worker_id"],
                    spec_key=spec_key,
                    box=selected,
                    finished=True,
                    close_reason="generation_done_low_dp_finished",
                    algorithm="low_dp_cache",
                )

            if cache_pool:
                self.save_box(
                    worker_id=runtime["worker_id"],
                    spec_key=spec_key,
                    box=cache_pool[:],
                    finished=False,
                    close_reason="generation_done_low_cache_remaining",
                    algorithm="low_dp_cache",
                )
                cache_pool.clear()
            return

        boxes: List[List[Fish]] = runtime["boxes"]
        for box_index, box in enumerate(boxes):
            if not box:
                continue
            self.save_box(
                worker_id=runtime["worker_id"],
                spec_key=spec_key,
                box=box[:],
                finished=False,
                close_reason=f"generation_done_high_box_{box_index}_remaining",
                algorithm="high_best_route",
            )
            box.clear()

    def finish_all_specs(self) -> None:
        for spec_key in list(self.current_specs.keys()):
            self.finish_spec(spec_key)

    # ---------- 输出统计 ----------
    def selected_cache_summary(self, selected_specs: List[str]) -> int:  # 修改返回类型为 int
        items = []
        for spec_key in selected_specs:
            cache_number = int(self.SPECS[spec_key].get("cache_number", 0))
            if cache_number > 0:
                items.append(cache_number)

        sum_cache = sum(items)
        return sum_cache if items else 0 

    def selected_box_summary(self, selected_specs: List[str]) -> int:

        items = []
        for spec_key in selected_specs:
            cache_number = int(self.SPECS[spec_key].get("box_number", 0))
            if cache_number > 0:
                items.append(cache_number)

        sum_cache = sum(items)
        return sum_cache if items else 0 
        # return ", ".join(
        #     f"{spec_key}={int(self.SPECS[spec_key].get('box_number', 0))}"
        #     for spec_key in selected_specs
        # )

    def print_summary(self, selected_specs: List[str]) -> None:

        
        self.logger.info("\n========== 总数据统计 ==========")
        self.logger.info(f"超时窗口: {self.TIMEOUT_FOLLOWING_FISH_COUNT}s")
        self.logger.info(
            "超时窗口分布: "
            + ", ".join(
                f"{window}s={self.stats.get(f'timeout_fish_{window}', 0)}"
                for window in self.TIMEOUT_WINDOWS
            )
        )
        self.logger.info(f"生成鱼数量: {self.stats.get('generated_fish', 0)}")
        self.logger.info(f"生成总重量: {self.stats.get('generated_weight', 0)}g")
        self.logger.info(f"分拣到其他队列: {self.stats.get('sorter_to_other_fish', 0)}")
        self.logger.info(f"低规格缓存格个数: {self.selected_cache_summary(selected_specs)}")
        self.logger.info(f"盒子数量: {self.selected_box_summary(selected_specs)}")
        self.logger.info(f"算法异常到其他队列: {self.stats.get('algorithm_to_other_fish', 0)}")
        self.logger.info(f"完成箱总数: {self.stats.get('finished_boxes', 0)}")
        self.logger.info(f"低规格完成箱: {self.stats.get('low_finished_boxes', 0)}")
        self.logger.info(f"高规格完成箱: {self.stats.get('high_finished_boxes', 0)}")
        self.logger.info(f"未完成盒数量: {self.stats.get('unfinished_boxes', 0)}")
        self.logger.info(f"未完成盒鱼条数: {self.stats.get('unfinished_box_fish', 0)}")
        self.logger.info(f"回流鱼总数: {self.stats.get('total_reflow_fish', 0)}")
        self.logger.info(f"低规格成功回流鱼: {self.stats.get('low_released_fish', 0)}")
        self.logger.info(f"高规格成功回流鱼: {self.stats.get('high_released_fish', 0)}")
        self.logger.info(f"超过释放次数进入未完成鱼: {self.stats.get('release_limit_fish', 0)}")
        self.logger.info(f"超时未完成记录: {self.stats.get('timeout_fish', 0)}")
        self.logger.info(
            "回流插入位置统计: "
            + ", ".join(
                f"{position}={self.stats.get(f'reflow_insert_{position}', 0)}"
                for position in self.REFLOW_INSERT_POSITIONS
            )
        )

        self.logger.info("\n========== 规格统计 ==========")
        for spec_key in selected_specs:
            ss = self.spec_stats.get(spec_key, {})
            self.logger.info(
                f"{spec_key}: "
                f"算法接收={ss.get('sorter_in', 0)}, "
                f"完成箱={ss.get('finished_boxes', 0)}, "
                f"完成鱼={ss.get('finished_fish', 0)}, "
                f"未完成盒={ss.get('unfinished_boxes', 0)}, "
                f"未完成盒鱼={ss.get('unfinished_box_fish', 0)}, "
                f"成功回流鱼={ss.get('released_fish', 0)}, "
                f"超限鱼={ss.get('release_limit_fish', 0)}, "
                f"超时鱼={ss.get('timeout_fish', 0)}, "
                + ", ".join(
                    f"超时{window}s鱼={ss.get(f'timeout_fish_{window}', 0)}"
                    for window in self.TIMEOUT_WINDOWS
                )
                + ", "
                f"结果队列={self.r.llen(self.result_queue_name(spec_key))}, "
                f"未完成队列={self.r.llen(self.unfinished_queue_name(spec_key))}, "
                f"超时队列={self.r.llen(self.timeout_queue_name(spec_key))}"
            )

    def run(
        self,
        target_weight: int = 10_000_000,
        fixed_start: Optional[str] = "20p",
        seed: Optional[int] = None,
    ) -> None:
        self.clear_runtime_queues()
        self.reset_timeout_state()
        selected_specs = self.generate_and_process_fish(
            target_weight=target_weight,
            fixed_start=fixed_start,
            seed=seed,
        )
        self.logger.info(f"选中的连续规格: {selected_specs}")

        self.print_summary(selected_specs)




if __name__ == "__main__":
    optimizer = ImprovedSpecLineOptimization(verbose=False)
    if optimizer.connect():
        optimizer.run(
            target_weight=TARGET_WEIGHT,
            fixed_start=FIXED_START,
            seed=None,
        )




"""

2026-06-17 10:13:03 [INFO] 生成完成：共 120814 条鱼，总重量 10000017g
2026-06-17 10:13:03 [INFO] 选中的连续规格: ['100p', '110p', '120p', '130p', '140p', '150p']
2026-06-17 10:13:03 [INFO] 
========== 总数据统计 ==========
2026-06-17 10:13:03 [INFO] 超时窗口: 600s
2026-06-17 10:13:03 [INFO] 超时窗口分布: 240s=40288, 300s=22343, 360s=9437, 420s=2593, 480s=414, 540s=27, 600s=0
2026-06-17 10:13:03 [INFO] 生成鱼数量: 120814
2026-06-17 10:13:03 [INFO] 生成总重量: 10000017g
2026-06-17 10:13:03 [INFO] 分拣到其他队列: 827
2026-06-17 10:13:03 [INFO] 低规格缓存格个数: 180
2026-06-17 10:13:03 [INFO] 盒子数量: 6
2026-06-17 10:13:03 [INFO] 算法异常到其他队列: 0
2026-06-17 10:13:03 [INFO] 完成箱总数: 1958
2026-06-17 10:13:03 [INFO] 低规格完成箱: 0
2026-06-17 10:13:03 [INFO] 高规格完成箱: 1958
2026-06-17 10:13:03 [INFO] 未完成盒数量: 8
2026-06-17 10:13:03 [INFO] 未完成盒鱼条数: 226
2026-06-17 10:13:03 [INFO] 回流鱼总数: 0
2026-06-17 10:13:03 [INFO] 低规格成功回流鱼: 0
2026-06-17 10:13:03 [INFO] 高规格成功回流鱼: 0
2026-06-17 10:13:03 [INFO] 超过释放次数进入未完成鱼: 0
2026-06-17 10:13:03 [INFO] 超时未完成记录: 0
2026-06-17 10:13:03 [INFO] 回流插入位置统计: 25=0, 35=0, 50=0
2026-06-17 10:13:03 [INFO] 
========== 规格统计 ==========
2026-06-17 10:13:03 [INFO] 100p: 算法接收=19867, 完成箱=400, 完成鱼=19849, 未完成盒=1, 未完成盒鱼=18, 成功回流鱼=0, 超限鱼=0, 超时鱼=0, 超时240s鱼=3716, 超时300s鱼=880, 超时360s鱼=63, 超时420s鱼=0, 超时480s鱼=0, 超时540s鱼=0, 超时600s鱼=0, 结果队列=400, 未完成队列=1, 超时队列=4659
2026-06-17 10:13:03 [INFO] 110p: 算法接收=20041, 完成箱=365, 完成鱼=19987, 未完成盒=2, 未完成盒鱼=54, 成功回流鱼=0, 超限鱼=0, 超时鱼=0, 超时240s鱼=5136, 超时300s鱼=1868, 超时360s鱼=275, 超时420s鱼=16, 超时480s鱼=0, 超时540s鱼=0, 超时600s鱼=0, 结果队列=365, 未完成队列=2, 超时队列=7295
2026-06-17 10:13:03 [INFO] 120p: 算法接收=19901, 完成箱=331, 完成鱼=19860, 未完成盒=1, 未完成盒鱼=41, 成功回流鱼=0, 超限鱼=0, 超时鱼=0, 超时240s鱼=6448, 超时300s鱼=3234, 超时360s鱼=841, 超时420s鱼=89, 超时480s鱼=5, 超时540s鱼=0, 超时600s鱼=0, 结果队列=331, 未完成队列=1, 超时队列=10617
2026-06-17 10:13:03 [INFO] 130p: 算法接收=20250, 完成箱=311, 完成鱼=20210, 未完成盒=1, 未完成盒鱼=40, 成功回流鱼=0, 超限鱼=0, 超时鱼=0, 超时240s鱼=7339, 超时300s鱼=4262, 超时360s鱼=1551, 超时420s鱼=201, 超时480s鱼=15, 超时540s鱼=0, 超时600s鱼=0, 结果队列=311, 未完成队列=1, 超时队列=13368
2026-06-17 10:13:03 [INFO] 140p: 算法接收=19931, 完成箱=284, 完成鱼=19874, 未完成盒=2, 未完成盒鱼=57, 成功回流鱼=0, 超限鱼=0, 超时鱼=0, 超时240s鱼=8364, 超时300s鱼=5496, 超时360s鱼=2797, 超时420s鱼=807, 超时480s鱼=106, 超时540s鱼=6, 超时600s鱼=0, 结果队列=284, 未完成队列=2, 超时队列=17576
2026-06-17 10:13:03 [INFO] 150p: 算法接收=19997, 完成箱=267, 完成鱼=19981, 未完成盒=1, 未完成盒鱼=16, 成功回流鱼=0, 超限鱼=0, 超时鱼=0, 超时240s鱼=9285, 超时300s鱼=6603, 超时360s鱼=3910, 超时420s鱼=1480, 超时480s鱼=288, 超时540s鱼=21, 超时600s鱼=0, 结果队列=267, 未完成队列=1, 超时队列=21587


"""