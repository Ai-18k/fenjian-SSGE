# -*- coding: utf-8 -*-
"""
ImprovedFishBoxing501 的 Python 迁移版。

目标：尽量保持 Java 原实现的变量结构、函数流程、装箱策略和边界判断一致。
默认入口与 Java main 一致：main_function(100, 5011, 140)。

注意：Java 原代码中的 saveLogs 数据库写入在 mainFunction 中是注释状态；
Python 版保留 save_logs 方法，但数据库连接信息改为环境变量，避免硬编码敏感信息。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Iterable, List, Optional, Tuple


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ImprovedFishBoxing501")


@dataclass(eq=False)
class Fish:
    """等价于 Java kaoman.bean.Fish。eq=False 用对象身份模拟 Java 默认对象相等语义。"""

    id: int
    weight: int
    status: int
    spec: str

    def get_print_str(self) -> str:
        """对应 Java Fish#getPrintStr。"""
        return f"{self.id}={self.weight}g/{self.spec}"


@dataclass(eq=False)
class BoxConfig:
    """等价于 Java kaoman.bean.BoxConfig。"""

    spec: str
    min_fish_count: int
    max_fish_count: int
    min_fish_weight: int
    max_fish_weight: int
    spec_list: List[str] = field(default_factory=list)


@dataclass(eq=False)
class Box:
    """等价于 Java kaoman.bean.Box。"""

    spec: str
    fish_list: List[Fish]
    weight: int
    fish_count: int


# ==================== Java 静态字段的 Python 对应 ====================

limit_weight: int = 10 * 1000 * 1000 - 100 * 1000

MAX_WEIGHT: int = 5030
MIN_WEIGHT: int = 4980
BUFFER_SIZE_LIMIT: int = 120
CACHE_BOX_PER_SPEC: int = 4

FISH_MAX_ROUND: int = 600
out_time_fish: Dict[int, Fish] = {}

FISH_MIN_WEIGHT: int = 0
FISH_MAX_WEIGHT: int = 0

# Java LinkedHashMap：Python 使用 OrderedDict 保持插入顺序
buffer_map: "OrderedDict[str, List[Fish]]" = OrderedDict()
total_buffered: int = 0

reflow_fish: List[Fish] = []

cache_boxes: List[List[List[Fish]]] = []
sum_weights: List[List[int]] = []
configs: List[BoxConfig] = []
box_threshold: List[int] = []

last_no_match_time: List[int] = []
last_no_match_hash: List[int] = []

FISH_SIZE: int = 0
MAX_BUFFER_SIZE: int = 0
BOXED_FISH_COUNT: int = 0

stop_reason: Optional[str] = None
box_list: List[Box] = []
fail_box_list: List[Box] = []

total_fish_weight: int = 0

error_interval: Dict[int, List[Tuple[int, int]]] = {}
min_possible_next_weight: List[int] = []
calculate_size: int = 0

result: List[str] = []
out_time_fish_size: List[int] = []


def _now_millis() -> int:
    return int(time.time() * 1000)


def _half_up(value: Decimal | int | float | str, scale: int) -> Decimal:
    q = Decimal("1") if scale == 0 else Decimal("1").scaleb(-scale)
    return Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP)


def _divide_half_up(numerator: int | Decimal, denominator: int | Decimal, scale: int) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        Decimal("1").scaleb(-scale), rounding=ROUND_HALF_UP
    )


def _java_int(value: int) -> int:
    """模拟 Java int 32 位有符号溢出。"""
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


def _java_string_hashcode(s: str) -> int:
    h = 0
    for ch in s:
        h = _java_int(31 * h + ord(ch))
    return h


# ==================== 主流程 ====================


def main() -> None:
    # Java main 中实际启用的是这一行
    main_function(100, 5011, 140)
    print("====================最终记录结果=======================")
    for s in result:
        print(s)
    if out_time_fish_size:
        avg = sum(out_time_fish_size) / len(out_time_fish_size)
        print(f"平均超时鱼: {_half_up(avg, 2)}")
    else:
        print("平均超时鱼: 0.00")


def main_function(round_: int, test_number: int, buffer_size_limit: int) -> None:
    global BUFFER_SIZE_LIMIT, total_buffered

    size = 1
    while size <= round_:
        batch_uuid = str(uuid.uuid4())
        log.info("===============================测试批次开始：%s===============================", batch_uuid)
        init_box_config()
        BUFFER_SIZE_LIMIT = buffer_size_limit
        fish_count = 25000
        start = _now_millis()
        simulate_fish_flow(fish_count)
        end = _now_millis()

        time_consuming = (end - start) // 1000
        log.info(
            "===============================第%s次计算结束，耗时：%s秒===============================",
            size,
            (end - start) / 1000.0,
        )

        print_limit = min(20, len(box_list))
        for i in range(print_limit):
            info = box_list[i]
            log.info(
                "  箱%s：规格=%s，条数=%s，总重=%sg，鱼详情=%s",
                i,
                info.spec,
                info.fish_count,
                info.weight,
                get_box_info(info),
            )
        if len(box_list) > 20:
            log.info("  ... 中间省略 %s 箱 ...", len(box_list) - 25)
            for i in range(max(20, len(box_list) - 5), len(box_list)):
                info = box_list[i]
                log.info(
                    "  箱%s：规格=%s，条数=%s，总重=%sg，鱼详情=%s",
                    i,
                    info.spec,
                    info.fish_count,
                    info.weight,
                    get_box_info(info),
                )

        log.info("===============================各规格暂存箱余量===============================")
        total_in_boxes = 0
        remain_weight = 0
        for i in range(len(configs)):
            for j in range(CACHE_BOX_PER_SPEC):
                cache_box = cache_boxes[i][j]
                remain_weight += sum(f.weight for f in cache_box)
                total_in_boxes += len(cache_box)
                log.info(
                    "  规格[%s]：阈值=%s条，暂存箱总条数=%s，总重=%sg",
                    configs[i].spec,
                    box_threshold[i],
                    len(cache_box),
                    sum_weights[i][j],
                )

        log.info("===============================缓冲池各规格详情===============================")
        for k, v in buffer_map.items():
            reflow_str = "缓冲池鱼：" + " ".join(f.get_print_str() for f in v)
            if v:
                reflow_str += " "
            log.info("  规格%s：缓冲池%s条", k, len(v))
            log.info(reflow_str)

        log.info("===============================回流转存鱼详情===============================")
        reflow_str = "  回流转存鱼：" + " ".join(f.get_print_str() for f in reflow_fish)
        if reflow_fish:
            reflow_str += " "
        log.info(reflow_str)

        log.info("===============================【统计信息】===============================")
        log.info("  总鱼数量：%s", FISH_SIZE)
        log.info(
            "  总鱼重量：%sg/ %s kg / %st",
            total_fish_weight,
            total_fish_weight / 1000.0,
            total_fish_weight / 1000000.0,
        )
        log.info("  已装箱鱼数量：%s", BOXED_FISH_COUNT)

        compelate_rate = _divide_half_up(BOXED_FISH_COUNT, FISH_SIZE, 2) * Decimal(100)
        log.info("  装箱完成率：%s%%", compelate_rate)
        log.info("  装箱完成箱数：%s", len(box_list))
        log.info("  剩余暂存箱数：%s", total_in_boxes)
        log.info("  失败暂存箱数：%s", len(fail_box_list))
        log.info("  回流转存数量：%s", len(reflow_fish))

        # Java 原代码在这里重置 totalBuffered，再根据 bufferMap 重算
        total_buffered = 0
        buffer_json: Dict[str, List[str]] = {}
        for k, v in buffer_map.items():
            total_buffered += len(v)
            remain_weight += sum(f.weight for f in v)
            buffer_json[k] = [f.get_print_str() for f in v]

        log.info("  缓冲池剩余鱼数量：%s", total_buffered)
        log.info("  缓冲池历史最大数量：%s", MAX_BUFFER_SIZE)

        for fish in reflow_fish:
            remain_weight += fish.weight

        divide = _divide_half_up(remain_weight, 1000, 2)
        log.info("剩余重量：%skg", divide)
        percent = _divide_half_up(remain_weight, total_fish_weight, 4)
        log.info("剩余率：%s%%，成功率：%s%%", percent, Decimal(100) - percent)
        log.info("  结束原因：%s", stop_reason)
        log.info("  计算次数：%s", calculate_size)
        log.info("超时的鱼：%s条", len(out_time_fish))
        out_time_fish_size.append(len(out_time_fish))

        final_record = (
            f"最终记录 缓存箱：{BUFFER_SIZE_LIMIT} 完成率：{compelate_rate}（数量）/ "
            f"{Decimal(100) - percent}（重量） 剩余：{divide}kg ，计算次数：{calculate_size}，总样本：{FISH_SIZE}"
        )
        log.info(final_record)
        result.append(final_record)

        # Java 原代码中的保存日志调用为注释状态，Python 版保持不主动调用。
        # save_logs(test_number, batch_uuid, size, None, fish_count, None,
        #           total_buffered, json.dumps(buffer_json, ensure_ascii=False), time_consuming,
        #           len(fail_box_list), json.dumps([box_to_dict(b) for b in fail_box_list], ensure_ascii=False))
        _ = (test_number, buffer_json, time_consuming)  # 保留变量语义，避免未使用告警
        size += 1


# ==================== 可选数据库日志 ====================


def save_logs(
    test_number: int,
    batch_uuid: str,
    batch_size: int,
    specs: Optional[str],
    simple_total: int,
    buffer_detail: Optional[str],
    max_buffer_remaining_size: int,
    buffer_remaining_detail: str,
    time_consuming: int,
    fail_box_size: Optional[int],
    fail_box_detail: Optional[str],
) -> None:
    """
    对应 Java saveLogs。

    Java 原代码硬编码了 JDBC 地址、账号和密码；Python 版为了安全改为环境变量：
      FISH_DB_HOST, FISH_DB_PORT, FISH_DB_NAME, FISH_DB_USER, FISH_DB_PASSWORD
    该函数默认不被 main_function 调用，与 Java 源码中注释状态一致。
    """
    try:
        import pymysql  # type: ignore
    except ImportError:
        log.warning("未安装 pymysql，跳过数据库保存。安装：pip install pymysql")
        return

    host = os.getenv("FISH_DB_HOST")
    port = int(os.getenv("FISH_DB_PORT", "3306"))
    db_name = os.getenv("FISH_DB_NAME", "fish_test")
    user = os.getenv("FISH_DB_USER")
    password = os.getenv("FISH_DB_PASSWORD")
    if not host or not user or not password:
        log.warning("数据库环境变量不完整，跳过数据库保存。")
        return

    table_name = f"{test_number}_boxing_test_logs_{BUFFER_SIZE_LIMIT}"
    create_sql = f"""
    CREATE TABLE IF NOT EXISTS `{table_name}` (
        `id` INT UNSIGNED NOT NULL AUTO_INCREMENT,
        `batch_size` INT NOT NULL,
        `specs` VARCHAR(500) DEFAULT NULL,
        `stop_reason` VARCHAR(255) DEFAULT NULL,
        `simple_total` INT DEFAULT NULL,
        `actual_total` INT NOT NULL,
        `reflow_total` INT NOT NULL,
        `max_buffer_size` INT NOT NULL,
        `box_size` INT NOT NULL,
        `fail_box_size` INT NOT NULL,
        `max_buffer_remaining_size` INT NOT NULL,
        `time_consuming` BIGINT NOT NULL,
        `created_datetime` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        `uuid` VARCHAR(36) NOT NULL,
        `buffer_detail` LONGTEXT,
        `buffer_remaining_detail` LONGTEXT,
        `cache_boxes_detail` LONGTEXT,
        `reflow_fish_detail` LONGTEXT,
        `box_list_detail` LONGTEXT,
        `fail_box_list_detail` LONGTEXT,
        PRIMARY KEY (`id`),
        KEY `idx_created` (`created_datetime`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='鱼群装箱测试日志表'
    """
    insert_sql = f"""
    INSERT INTO `{table_name}`
    (batch_size, specs, stop_reason, simple_total, actual_total, reflow_total,
     max_buffer_size, buffer_detail, box_size, max_buffer_remaining_size,
     buffer_remaining_detail, time_consuming, created_datetime, uuid, fail_box_size, fail_box_list_detail)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
    """

    conn = pymysql.connect(host=host, port=port, user=user, password=password, database=db_name, charset="utf8mb4")
    try:
        with conn.cursor() as cursor:
            cursor.execute(create_sql)
            cursor.execute(
                insert_sql,
                (
                    batch_size,
                    specs,
                    stop_reason,
                    limit_weight,
                    total_fish_weight,
                    len(reflow_fish),
                    MAX_BUFFER_SIZE,
                    buffer_detail,
                    len(box_list),
                    max_buffer_remaining_size,
                    buffer_remaining_detail,
                    time_consuming,
                    batch_uuid,
                    fail_box_size,
                    fail_box_detail,
                ),
            )
        conn.commit()
        print("✅ 数据保存成功！")
    except Exception:
        conn.rollback()
        log.exception("❌ 数据保存失败")
    finally:
        conn.close()


# ==================== 模拟鱼流 ====================


def generate_random_fish(id_: int) -> Fish:
    global FISH_SIZE

    # 1. 均匀随机选规格
    cfg = configs[random.randrange(len(configs))]
    FISH_SIZE += 1
    # 2. 在 [minWeight, maxWeight] 内均匀随机生成重量
    weight = random.randint(cfg.min_fish_weight, cfg.max_fish_weight)

    spec_config = get_spec(weight)
    if spec_config is None:
        raise ValueError(f"重量 {weight} 没有匹配规格")
    return Fish(id_, weight, 0, spec_config.spec)


def generate_fish(id_: int) -> Fish:
    global FISH_SIZE

    rand = random.Random()
    FISH_SIZE += 1
    weight = FISH_MIN_WEIGHT + rand.randrange(FISH_MAX_WEIGHT - FISH_MIN_WEIGHT + 1)
    spec_config = get_spec(weight)
    if spec_config is None:
        raise ValueError(f"重量 {weight} 没有匹配规格")
    return Fish(id_, weight, 0, spec_config.spec)


def simulate_fish_flow(fish_count: int) -> None:
    global total_fish_weight, MAX_BUFFER_SIZE, stop_reason

    last_log = _now_millis()
    i = 1
    # Java 原代码没有使用 fishCount，而是按 limitWeight 停止
    _ = fish_count
    while total_fish_weight < limit_weight:
        # fish = generate_fish(i)  # 随机
        fish = generate_random_fish(i)

        # 处理超时鱼
        for _, fishes in list(buffer_map.items()):
            for fish1 in fishes:
                if fish.id - fish1.id >= FISH_MAX_ROUND:
                    out_time_fish[fish1.id] = fish1

        total_fish_weight += fish.weight

        process_new_fish(fish)
        MAX_BUFFER_SIZE = max(MAX_BUFFER_SIZE, total_buffered)

        now = _now_millis()
        if i > 0 and (i % 10000 == 0 or now - last_log > 10000):
            progress = 100 * (total_fish_weight / limit_weight)
            log.info(
                "进度：%s/%s (%s%%)，缓冲池：%s，已装箱：%s箱/%s条，失败箱子：%s，回流：%s",
                total_fish_weight,
                "10t",
                _half_up(progress, 2),
                total_buffered,
                len(box_list),
                BOXED_FISH_COUNT,
                len(fail_box_list),
                len(reflow_fish),
            )
            last_log = now

        if reflow_fish is not None and (len(reflow_fish) % 100 == 0):
            array_list = list(reflow_fish)
            reflow_fish.clear()
            for new_fish in array_list:
                process_new_fish(new_fish)

        i += 1

    if reflow_fish is not None and len(reflow_fish) > 0:
        array_list = list(reflow_fish)
        reflow_fish.clear()
        for new_fish in array_list:
            process_new_fish(new_fish)

    finish_with_buffer()
    if stop_reason is None:
        stop_reason = "样本处理完成"


# ==================== 核心处理 ====================


def process_new_fish(fish: Fish) -> None:
    """
    处理新鱼流程：
    ① 优先尝试直接装箱（新鱼 + 暂存箱 + 缓冲池 FIFO 鱼 → 完成一箱）
    ② 装箱失败，检查暂存箱是否还有空间（未达阈值），有则放入暂存箱
    ③ 暂存箱都已满（达到阈值），则放入缓冲池
    """
    global calculate_size

    calculate_size += 1
    spec_idx = get_config_index(fish.spec)
    if spec_idx < 0:
        return

    boxed = try_direct_packing(spec_idx, fish)
    if boxed:
        return

    placed_in_cache = try_place_in_cache_box(spec_idx, fish)
    if placed_in_cache:
        check_and_pack_spec(spec_idx)
        return

    if total_buffered >= BUFFER_SIZE_LIMIT:
        add_to_buffer(fish)
        matched = try_all_specs_packing()
        if not matched:
            remove_from_buffer(fish)
            evicted = evict_oldest()
            if evicted is not None:
                reflow_fish.append(evicted)
            add_to_buffer(fish)
    else:
        add_to_buffer(fish)
        try_match_for_spec(fish.spec)


def try_direct_packing(spec_idx: int, fish: Fish) -> bool:
    cfg = configs[spec_idx]
    main_spec = cfg.spec
    spec_list = cfg.spec_list

    for bj in range(CACHE_BOX_PER_SPEC):
        cache_box = cache_boxes[spec_idx][bj]
        if len(cache_box) == 0:
            continue

        cur_cnt = len(cache_box) + 1
        cur_w = sum_weights[spec_idx][bj] + fish.weight

        if cur_cnt > cfg.max_fish_count:
            continue
        if cur_w > MAX_WEIGHT:
            continue

        # 情况A：暂存箱+新鱼 已经满足装箱条件
        if cur_cnt >= cfg.min_fish_count and MIN_WEIGHT <= cur_w <= MAX_WEIGHT:
            cache_box.append(fish)
            sum_weights[spec_idx][bj] += fish.weight
            complete_box(spec_idx, bj, [])
            return True

        # 情况B：需要从缓冲池补鱼
        need_low = MIN_WEIGHT - cur_w
        need_high = MAX_WEIGHT - cur_w
        min_add = max(1, cfg.min_fish_count - cur_cnt)
        max_add = cfg.max_fish_count - cur_cnt

        if need_high < 0 or max_add <= 0:
            continue

        # B1：纯同规格缓冲池鱼
        main_buf = buffer_map.get(main_spec, [])
        if main_buf:
            main_table = FreqTable(main_buf)
            for k in range(min_add, max_add + 1):
                res = main_table.find_combination(k, max(0, need_low), need_high)
                if res is not None:
                    cache_box.append(fish)
                    sum_weights[spec_idx][bj] += fish.weight
                    complete_box(spec_idx, bj, res)
                    return True

        # B2：1条相邻规格 + 其余同规格缓冲池鱼
        for adj_spec in spec_list:
            if adj_spec == main_spec:
                continue
            adj_buf = buffer_map.get(adj_spec, [])
            if not adj_buf:
                continue
            for adj in list(adj_buf):
                rem_low = need_low - adj.weight
                rem_high = need_high - adj.weight
                need_min2 = max(0, min_add - 1)
                need_max2 = max_add - 1
                if need_min2 <= need_max2 and main_buf:
                    reduced = FreqTable(main_buf)
                    # Java 原代码对 mainBuf 构建的 reduced.remove(adj)，adj 是相邻规格鱼，通常不会移除任何同规格鱼；这里照搬。
                    reduced.remove(adj)
                    for k in range(max(0, need_min2), need_max2 + 1):
                        res = reduced.find_combination(k, max(0, rem_low), rem_high)
                        if res is not None:
                            res.insert(0, adj)
                            cache_box.append(fish)
                            sum_weights[spec_idx][bj] += fish.weight
                            complete_box(spec_idx, bj, res)
                            return True
    return False


def try_place_in_cache_box(spec_idx: int, fish: Fish) -> bool:
    cfg = configs[spec_idx]

    min_next_w = min_possible_next_weight[spec_idx]
    # 优先放入已有鱼且不超重的暂存箱
    for j in range(CACHE_BOX_PER_SPEC):
        box = cache_boxes[spec_idx][j]
        if len(box) == 0:
            continue
        if len(box) >= box_threshold[spec_idx]:
            continue

        new_count = len(box) + 1
        new_weight = sum_weights[spec_idx][j] + fish.weight
        if new_count >= cfg.max_fish_count:
            continue
        if new_weight > MAX_WEIGHT:
            continue

        # 死区
        if is_in_error_interval(spec_idx, new_weight):
            return False

        is_complete = new_weight >= MIN_WEIGHT and new_count >= cfg.min_fish_count
        if not is_complete:
            remaining = MAX_WEIGHT - new_weight
            if remaining < min_next_w:
                return False

        box.append(fish)
        sum_weights[spec_idx][j] += fish.weight
        return True

    # 放入空暂存箱（空箱没有阈值限制）
    for j in range(CACHE_BOX_PER_SPEC):
        if len(cache_boxes[spec_idx][j]) == 0:
            cache_boxes[spec_idx][j].append(fish)
            sum_weights[spec_idx][j] += fish.weight
            return True

    return False


def check_and_pack_spec(spec_idx: int) -> None:
    loops = 0
    while True:
        packed = False
        for j in range(CACHE_BOX_PER_SPEC):
            cache_box = cache_boxes[spec_idx][j]
            if len(cache_box) >= box_threshold[spec_idx] or len(cache_box) >= configs[spec_idx].min_fish_count:
                if try_pack_from_cache(spec_idx, j):
                    packed = True
                    break

            if len(cache_box) == box_threshold[spec_idx]:
                cfg = configs[spec_idx]
                spec_list = cfg.spec_list
                weight = sum_weights[spec_idx][j]
                need_min_fish = MIN_WEIGHT - weight
                need_max_fish = MAX_WEIGHT - weight
                spec_min_fish = cfg.min_fish_weight
                spec_max_fish = cfg.max_fish_weight
                for spec in spec_list:
                    config_index = get_config_index(spec)
                    # Java 原代码是 configIndex > 0，不包含 0 号规格；这里照搬。
                    if config_index > 0:
                        config = configs[config_index]
                        spec_min_fish = min(spec_min_fish, config.min_fish_weight)
                        spec_max_fish = max(spec_max_fish, config.max_fish_weight)

                # Java 原代码声明 flag 但未使用；照搬逻辑，不使用。
                _flag = False
                if spec_min_fish > need_max_fish:
                    # Java 原代码这里是空逻辑块。
                    pass
                if need_min_fish < spec_min_fish and need_max_fish < spec_min_fish:
                    box_fishes = list(cache_boxes[spec_idx][j])
                    total_weight = sum_weights[spec_idx][j]
                    fail_box_list.append(Box(configs[spec_idx].spec, box_fishes, total_weight, len(box_fishes)))
                    cache_boxes[spec_idx][j] = []
                    sum_weights[spec_idx][j] = 0
        loops += 1
        if not (packed and loops < 20):
            break


def try_pack_from_cache(spec_idx: int, box_idx: int) -> bool:
    cache_box = cache_boxes[spec_idx][box_idx]
    cur_cnt = len(cache_box)
    cur_w = sum_weights[spec_idx][box_idx]
    cfg = configs[spec_idx]
    main_spec = cfg.spec
    spec_list = cfg.spec_list

    if cur_cnt < cfg.min_fish_count and cur_cnt < box_threshold[spec_idx]:
        return False
    if cur_w > MAX_WEIGHT:
        return False

    # 情况A：暂存箱自身满足条件
    if cur_cnt >= cfg.min_fish_count and MIN_WEIGHT <= cur_w <= MAX_WEIGHT and cur_cnt <= cfg.max_fish_count:
        complete_box(spec_idx, box_idx, [])
        return True

    # 情况B：需要缓冲池补鱼
    need_low = MIN_WEIGHT - cur_w
    need_high = MAX_WEIGHT - cur_w
    min_add = max(1, cfg.min_fish_count - cur_cnt)
    max_add = cfg.max_fish_count - cur_cnt

    if need_high < 0 or max_add <= 0:
        return False

    # B1：纯同规格
    main_buf = buffer_map.get(main_spec, [])
    if main_buf:
        main_table = FreqTable(main_buf)
        for k in range(min_add, max_add + 1):
            res = main_table.find_combination(k, max(0, need_low), need_high)
            if res is not None:
                complete_box(spec_idx, box_idx, res)
                return True

    # B2：1条相邻 + 其余同规格
    for adj_spec in spec_list:
        if adj_spec == main_spec:
            continue
        adj_buf = buffer_map.get(adj_spec, [])
        if not adj_buf:
            continue
        for adj in list(adj_buf):
            rem_low = need_low - adj.weight
            rem_high = need_high - adj.weight
            need_min2 = max(0, min_add - 1)
            need_max2 = max_add - 1
            if need_min2 <= need_max2 and main_buf:
                reduced = FreqTable(main_buf)
                # 照搬 Java：从同规格表 reduced 中 remove(adj)。adj 通常来自相邻规格，因此一般不会移除。
                reduced.remove(adj)
                for k in range(max(0, need_min2), need_max2 + 1):
                    res = reduced.find_combination(k, max(0, rem_low), rem_high)
                    if res is not None:
                        res.insert(0, adj)
                        complete_box(spec_idx, box_idx, res)
                        return True
    return False


def try_all_specs_packing() -> bool:
    for si in range(len(configs)):
        for bj in range(CACHE_BOX_PER_SPEC):
            if cache_boxes[si][bj] and len(cache_boxes[si][bj]) >= box_threshold[si]:
                if try_pack_from_cache(si, bj):
                    return True
    return try_empty_box_packing()


def try_match_for_spec(spec: str) -> None:
    affected_specs: "OrderedDict[int, None]" = OrderedDict()
    for i in range(len(configs)):
        if spec in configs[i].spec_list:
            affected_specs[i] = None
    if not affected_specs:
        return

    loops = 0
    max_loops = 10
    while True:
        matched = False
        for si in list(affected_specs.keys()):
            for bj in range(CACHE_BOX_PER_SPEC):
                cb = cache_boxes[si][bj]
                if not cb:
                    continue
                if len(cb) >= box_threshold[si] or len(cb) >= configs[si].min_fish_count:
                    now = _now_millis()
                    h = compute_buffer_hash()
                    cache_idx = si * CACHE_BOX_PER_SPEC + bj
                    if (
                        cache_idx < len(last_no_match_time)
                        and last_no_match_time[cache_idx] > 0
                        and now - last_no_match_time[cache_idx] < 50
                        and last_no_match_hash[cache_idx] == h
                    ):
                        continue
                    if try_pack_from_cache(si, bj):
                        if cache_idx < len(last_no_match_time):
                            last_no_match_time[cache_idx] = 0
                        matched = True
                        affected_specs.clear()
                        for i in range(len(configs)):
                            affected_specs[i] = None
                        break
                    else:
                        if cache_idx < len(last_no_match_time):
                            last_no_match_time[cache_idx] = now
                            last_no_match_hash[cache_idx] = h
            if matched:
                break
        loops += 1
        if not (matched and loops < max_loops):
            break


def try_empty_box_packing() -> bool:
    global BOXED_FISH_COUNT

    for si in range(len(configs)):
        main_spec = configs[si].spec
        min_k = configs[si].min_fish_count
        max_k = configs[si].max_fish_count

        main_buf = buffer_map.get(main_spec, [])
        if main_buf:
            main_table = FreqTable(main_buf)
            for k in range(min_k, max_k + 1):
                res = main_table.find_combination(k, MIN_WEIGHT, MAX_WEIGHT)
                if res is not None:
                    for f in list(res):
                        remove_from_buffer(f)
                    total_weight = sum(f.weight for f in res)
                    box_list.append(Box(main_spec, list(res), total_weight, len(res)))
                    BOXED_FISH_COUNT += len(res)
                    return True
    return False


def compute_buffer_hash() -> int:
    h = 0
    for key, value in buffer_map.items():
        h = _java_int(31 * h + _java_string_hashcode(key))
        h = _java_int(31 * h + len(value))
    return h


# ==================== 重量频率表（核心数据结构）====================


class FreqTable:
    """
    将鱼按重量分组，用于高效组合枚举。
    每个重量组内的鱼保持 FIFO 顺序。
    """

    def __init__(self, fish_list_or_table: Iterable[Fish] | "FreqTable") -> None:
        self.weights: List[int] = []
        self.counts: List[int] = []
        self.fish_by_weight: List[List[Fish]] = []

        if isinstance(fish_list_or_table, FreqTable):
            self.weights.extend(fish_list_or_table.weights)
            self.counts.extend(fish_list_or_table.counts)
            self.fish_by_weight.extend([list(lst) for lst in fish_list_or_table.fish_by_weight])
            return

        grouped: "OrderedDict[int, List[Fish]]" = OrderedDict()
        for f in fish_list_or_table:
            if f.weight not in grouped:
                grouped[f.weight] = []
            grouped[f.weight].append(f)

        for w in sorted(grouped.keys()):
            self.weights.append(w)
            fish_list = grouped[w]
            self.counts.append(len(fish_list))
            self.fish_by_weight.append(fish_list)

    def copy(self) -> "FreqTable":
        return FreqTable(self)

    def remove(self, f: Fish) -> None:
        for i, fish_list in enumerate(list(self.fish_by_weight)):
            removed = False
            for idx, item in enumerate(fish_list):
                if item is f:
                    del fish_list[idx]
                    removed = True
                    break
            if removed:
                self.counts[i] -= 1
                if self.counts[i] == 0:
                    del self.weights[i]
                    del self.counts[i]
                    del self.fish_by_weight[i]
                return

    def find_combination(self, k: int, low: int, high: int) -> Optional[List[Fish]]:
        """选恰好 k 条鱼，总重在 [low, high] 内。"""
        if k <= 0:
            return None
        if low < 0:
            low = 0
        if self.min_sum(k) > high or self.max_sum(k) < low:
            return None
        out: List[Fish] = []
        found = self._dfs(0, k, low, high, 0, out)
        return out if found else None

    def min_sum(self, k: int) -> int:
        s = 0
        need = k
        for i in range(len(self.weights)):
            if need <= 0:
                break
            take = min(need, self.counts[i])
            s += self.weights[i] * take
            need -= take
        return s

    def max_sum(self, k: int) -> int:
        s = 0
        need = k
        for i in range(len(self.weights) - 1, -1, -1):
            if need <= 0:
                break
            take = min(need, self.counts[i])
            s += self.weights[i] * take
            need -= take
        return s

    def _dfs(self, idx: int, remain: int, low: int, high: int, cur_sum: int, out: List[Fish]) -> bool:
        if remain == 0:
            return cur_sum >= low
        if idx >= len(self.weights):
            return False

        w = self.weights[idx]
        max_take = min(remain, self.counts[idx])

        # 剪枝：最优情况也不在范围内
        best_min = cur_sum + w * max_take
        if remain > max_take:
            need_more = remain - max_take
            for j in range(idx + 1, len(self.weights)):
                if need_more <= 0:
                    break
                t = min(need_more, self.counts[j])
                best_min += self.weights[j] * t
                need_more -= t
        if best_min > high:
            return False

        best_max = cur_sum + w * max_take
        if remain > max_take:
            need_more = remain - max_take
            for j in range(len(self.weights) - 1, idx, -1):
                if need_more <= 0:
                    break
                t = min(need_more, self.counts[j])
                best_max += self.weights[j] * t
                need_more -= t
        if best_max < low:
            return False

        # 尝试取 0~maxTake 条当前重量的鱼（优先取前面的，保证 FIFO）
        chosen = self.fish_by_weight[idx]
        for take in range(max_take + 1):
            new_sum = cur_sum + w * take
            if new_sum > high:
                break

            for t in range(take):
                out.append(chosen[t])

            if self._dfs(idx + 1, remain - take, low, high, new_sum, out):
                return True

            for _ in range(take):
                out.pop()
        return False


# ==================== 装箱操作 ====================


def complete_box(spec_idx: int, box_idx: int, used_fish: List[Fish]) -> None:
    global BOXED_FISH_COUNT

    box_fishes = list(cache_boxes[spec_idx][box_idx])
    box_fishes.extend(used_fish)
    total_weight = sum_weights[spec_idx][box_idx] + sum(f.weight for f in used_fish)
    box = Box(configs[spec_idx].spec, box_fishes, total_weight, len(box_fishes))
    box_list.append(box)
    BOXED_FISH_COUNT += len(box_fishes)

    # 从缓冲池移除已使用的鱼
    for f in list(used_fish):
        remove_from_buffer(f)

    # 清空暂存箱
    cache_boxes[spec_idx][box_idx] = []
    sum_weights[spec_idx][box_idx] = 0

    # 从缓冲池补充该暂存箱（优先同规格 FIFO）
    fill_box_from_buffer(spec_idx, box_idx)


def fill_box_from_buffer(spec_idx: int, box_idx: int) -> None:
    global total_buffered

    spec = configs[spec_idx].spec
    spec_buf = buffer_map.get(spec)
    if spec_buf is None or not spec_buf:
        return

    idx = 0
    while len(cache_boxes[spec_idx][box_idx]) < box_threshold[spec_idx] and idx < len(spec_buf):
        f = spec_buf[idx]
        if sum_weights[spec_idx][box_idx] + f.weight <= MAX_WEIGHT:
            del spec_buf[idx]
            total_buffered -= 1
            cache_boxes[spec_idx][box_idx].append(f)
            sum_weights[spec_idx][box_idx] += f.weight
            # Java Iterator.remove 后继续取下一个当前位置元素，所以 idx 不增加
        else:
            idx += 1

    if not spec_buf:
        buffer_map.pop(spec, None)


def add_to_buffer(fish: Fish) -> None:
    global total_buffered

    if fish.spec not in buffer_map:
        buffer_map[fish.spec] = []
    buffer_map[fish.spec].append(fish)
    total_buffered += 1


def remove_from_buffer(f: Fish) -> None:
    global total_buffered

    fish_list = buffer_map.get(f.spec)
    if fish_list is not None:
        for idx, item in enumerate(fish_list):
            if item is f:
                del fish_list[idx]
                total_buffered -= 1
                if not fish_list:
                    buffer_map.pop(f.spec, None)
                return


def evict_oldest() -> Optional[Fish]:
    global total_buffered

    oldest: Optional[Fish] = None
    oldest_spec: Optional[str] = None
    for spec, fishes in buffer_map.items():
        if fishes:
            first = fishes[0]
            if oldest is None or first.id < oldest.id:
                oldest = first
                oldest_spec = spec

    if oldest is not None and oldest_spec is not None:
        fishes = buffer_map[oldest_spec]
        for idx, item in enumerate(fishes):
            if item is oldest:
                del fishes[idx]
                break
        total_buffered -= 1
        if not fishes:
            buffer_map.pop(oldest_spec, None)
    return oldest


# ==================== 收尾处理 ====================


def finish_with_buffer() -> None:
    global stop_reason

    loops = 0
    max_loops = 500
    while total_buffered > 0 and loops < max_loops:
        loops += 1
        progress = False

        # 从缓冲池补充各暂存箱
        for si in range(len(configs)):
            for bj in range(CACHE_BOX_PER_SPEC):
                before = total_buffered
                fill_box_from_buffer(si, bj)
                if total_buffered < before:
                    progress = True

        # 尝试装箱
        for si in range(len(configs)):
            for bj in range(CACHE_BOX_PER_SPEC):
                if cache_boxes[si][bj] and len(cache_boxes[si][bj]) >= min(
                    box_threshold[si], configs[si].min_fish_count
                ):
                    if try_pack_from_cache(si, bj):
                        progress = True
                        break
            if progress:
                break

        if not progress and not try_relaxed_complete():
            break

    if total_buffered > 0:
        stop_reason = f"缓冲池剩余{total_buffered}条无法装箱"


def try_relaxed_complete() -> bool:
    # 1. 处理半成品暂存箱
    for si in range(len(configs)):
        for bj in range(CACHE_BOX_PER_SPEC):
            if not cache_boxes[si][bj]:
                continue
            cur_w = sum_weights[si][bj]
            cur_cnt = len(cache_boxes[si][bj])
            main_spec = configs[si].spec
            min_k = configs[si].min_fish_count
            max_k = configs[si].max_fish_count

            need_low = MIN_WEIGHT - cur_w
            need_high = MAX_WEIGHT - cur_w
            min_add = max(0, min_k - cur_cnt)
            max_add = max_k - cur_cnt

            main_buf = buffer_map.get(main_spec, [])

            # 纯同规格
            if main_buf and min_add <= max_add:
                mt = FreqTable(main_buf)
                n_min = max(0, min_add)
                for k in range(n_min, max_add + 1):
                    res = mt.find_combination(k, max(0, need_low), need_high)
                    if res is not None:
                        complete_box(si, bj, res)
                        return True

            # 1条相邻
            for adj_spec in configs[si].spec_list:
                if adj_spec == main_spec:
                    continue
                adj_buf = buffer_map.get(adj_spec, [])
                for adj in list(adj_buf):
                    rem_low = need_low - adj.weight
                    rem_high = need_high - adj.weight
                    n_min2 = max(0, min_add - 1)
                    n_max2 = max_add - 1
                    if n_min2 <= n_max2 and main_buf:
                        reduced = FreqTable(main_buf)
                        reduced.remove(adj)
                        for k in range(max(0, n_min2), n_max2 + 1):
                            res = reduced.find_combination(k, max(0, rem_low), rem_high)
                            if res is not None:
                                res.insert(0, adj)
                                complete_box(si, bj, res)
                                return True

    # 2. 从缓冲池直接装箱
    return try_empty_box_packing()


# ==================== 工具方法 ====================


def get_config_index(spec: str) -> int:
    for i, cfg in enumerate(configs):
        if cfg.spec == spec:
            return i
    return -1


def get_spec(weight: int) -> Optional[BoxConfig]:
    for c in configs:
        if c.min_fish_weight <= weight <= c.max_fish_weight:
            return c
    return None


def init_box_config() -> None:
    global stop_reason, FISH_SIZE, BOXED_FISH_COUNT, MAX_BUFFER_SIZE, total_buffered
    global cache_boxes, sum_weights, configs, box_threshold, last_no_match_time, last_no_match_hash
    global total_fish_weight, calculate_size, error_interval, min_possible_next_weight
    global FISH_MIN_WEIGHT, FISH_MAX_WEIGHT

    stop_reason = None
    FISH_SIZE = 0
    BOXED_FISH_COUNT = 0
    MAX_BUFFER_SIZE = 0
    total_buffered = 0
    buffer_map.clear()
    box_list.clear()
    out_time_fish.clear()
    reflow_fish.clear()
    total_fish_weight = 0
    calculate_size = 0

    # 注意：Java 原代码 initBoxConfig 没有清空 failBoxList，这里照搬，不 clear fail_box_list。
    error_interval = {}

    config_map = get_box_config_map()
    configs = [
        # config_map["15p"],
        config_map["20p"],
        config_map["25p"],
        config_map["30p"],
        config_map["35p"],
        config_map["40p"],
        config_map["45p"],
        # config_map["50p"],
        # config_map["60p"],
        # config_map["70p"],
        # config_map["80p"],
        # config_map["90p"],
        # config_map["100p"],
        # config_map["110p"],
        # config_map["120p"],
        # config_map["130p"],
        # config_map["140p"],
        # config_map["150p"],
    ]

    spec_count = len(configs)
    cache_boxes = [[[] for _ in range(CACHE_BOX_PER_SPEC)] for _ in range(spec_count)]
    sum_weights = [[0 for _ in range(CACHE_BOX_PER_SPEC)] for _ in range(spec_count)]
    box_threshold = [0 for _ in range(spec_count)]
    last_no_match_time = [0 for _ in range(spec_count * CACHE_BOX_PER_SPEC)]
    last_no_match_hash = [0 for _ in range(spec_count * CACHE_BOX_PER_SPEC)]
    min_possible_next_weight = [0 for _ in range(spec_count)]

    for i in range(spec_count):
        # 阈值 = 最小装箱条数，达到后暂存箱不再接收新鱼
        box_threshold[i] = int((configs[i].min_fish_count + 1) * 0.5)
        # box_threshold[i] = int(configs[i].min_fish_count - 1)

        min_w = configs[i].min_fish_weight
        for spec in configs[i].spec_list:
            if spec == configs[i].spec:
                continue
            idx = get_config_index(spec)
            if idx >= 0:
                min_w = min(min_w, configs[idx].min_fish_weight)
        min_possible_next_weight[i] = min_w
        error_interval[i] = get_error_interval(i)

    for i in range(spec_count * CACHE_BOX_PER_SPEC):
        last_no_match_time[i] = 0
        last_no_match_hash[i] = 0

    FISH_MIN_WEIGHT = configs[spec_count - 1].min_fish_weight
    FISH_MAX_WEIGHT = configs[0].max_fish_weight


def get_error_interval(spec_index: int) -> List[Tuple[int, int]]:
    config = configs[spec_index]
    min_fish_weight = config.min_fish_weight
    max_fish_weight = config.max_fish_weight
    spec_list = config.spec_list
    max_weight = MAX_WEIGHT
    min_weight = MIN_WEIGHT
    first = True
    neighbor_min = 0
    neighbor_max = 0

    for spec in spec_list:
        idx = get_config_index(spec)
        if idx >= 0:
            cfg = configs[idx]
            if neighbor_min == 0:
                neighbor_min = cfg.min_fish_weight
            else:
                neighbor_min = min(neighbor_min, cfg.min_fish_weight)
            if neighbor_max == 0:
                neighbor_max = cfg.max_fish_weight
            else:
                neighbor_max = max(neighbor_max, cfg.max_fish_weight)

    error_list: List[Tuple[int, int]] = []
    while True:
        current_min = min_weight
        current_max = max_weight
        if first:
            current_max = current_max - neighbor_min
            current_min = current_min - neighbor_max
            first = False
        else:
            current_max = current_max - min_fish_weight
            current_min = current_min - max_fish_weight
            if current_max < min_weight:
                error_list.append((current_max, min_weight))
            else:
                break
        max_weight = current_max
        min_weight = current_min
    return error_list


def get_box_info(box: Box) -> str:
    content = ",".join(f"{f.id}={f.weight}g/{f.spec}" for f in box.fish_list)
    return f"[{content}]"


def is_in_error_interval(spec_idx: int, weight: int) -> bool:
    intervals = error_interval.get(spec_idx)
    if intervals is None:
        return False
    for start, end in intervals:
        if start <= weight <= end:
            return True
    return False


def get_box_config_map() -> Dict[str, BoxConfig]:
    return {
        "15p": BoxConfig("15p", 7, 9, 566, 700, ["15p", "20p"]),
        "20p": BoxConfig("20p", 10, 11, 446, 565, ["15p", "20p", "25p"]),
        "25p": BoxConfig("25p", 12, 14, 366, 445, ["20p", "25p", "30p"]),
        "30p": BoxConfig("30p", 15, 16, 306, 365, ["25p", "30p", "35p"]),
        "35p": BoxConfig("35p", 17, 19, 266, 305, ["30p", "35p", "40p"]),
        "40p": BoxConfig("40p", 20, 21, 231, 265, ["35p", "40p", "45p"]),
        "45p": BoxConfig("45p", 22, 23, 211, 230, ["40p", "45p", "50p"]),
        "50p": BoxConfig("50p", 25, 26, 183, 210, ["45p", "50p", "60p"]),
        "60p": BoxConfig("60p", 30, 31, 153, 182, ["50p", "60p", "70p"]),
        "70p": BoxConfig("70p", 35, 36, 133, 152, ["60p", "70p", "80p"]),
        "80p": BoxConfig("80p", 40, 41, 116, 132, ["70p", "80p", "90p"]),
        "90p": BoxConfig("90p", 45, 46, 106, 115, ["80p", "90p", "100p"]),
        "100p": BoxConfig("100p", 50, 51, 96, 105, ["90p", "100p", "110p"]),
        "110p": BoxConfig("110p", 55, 56, 87, 95, ["100p", "110p", "120p"]),
        "120p": BoxConfig("120p", 60, 61, 80, 86, ["110p", "120p", "130p"]),
        "130p": BoxConfig("130p", 65, 66, 74, 79, ["120p", "130p", "140p"]),
        "140p": BoxConfig("140p", 70, 71, 69, 73, ["130p", "140p", "150p"]),
        "150p": BoxConfig("150p", 75, 76, 65, 68, ["140p", "150p"]),
    }


def fish_to_dict(fish: Fish) -> Dict[str, object]:
    return {"id": fish.id, "weight": fish.weight, "status": fish.status, "spec": fish.spec}


def box_to_dict(box: Box) -> Dict[str, object]:
    return {
        "spec": box.spec,
        "weight": box.weight,
        "fishCount": box.fish_count,
        "fishList": [fish_to_dict(f) for f in box.fish_list],
    }


if __name__ == "__main__":
    main()
