#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project : kaomam_project
@File : 深度搜索.py
@Description: 缓存区 + 深度优先自由组合配盒（4980–5030g）

与 Scheduler_Engine.BoxPlanner 的区别：
  · BoxPlanner 只能从小/中/大 FIFO 队头连续取前缀组合；
  · 本模块在有限容量的缓存区内对任意子集做 DFS，避免顺序约束导致的超容/无法成盒。

流程（以 15p、7–8 尾为例）：
  1. 设置缓存区容量上限；
  2. 大容量池逐条进鱼，缓存区达到 min(counts) 后启动 DFS 匹配；
  3. 匹配成功则移除对应鱼并记录成盒信息，继续进鱼。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator

TARGET_MIN = 4980   # 盒重下限（克）
TARGET_MAX = 5030   # 盒重上限（克）
TARGET_MID = 5005   # 盒重中心值，DFS 评分用

# SPECS：各规格的重量区间与合法装箱尾数
SPECS: dict[str, dict] = {
    "15p": {"range": (566, 700), "counts": (7, 8)},
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

# BUCKET_LABEL：小/中/大分区中文标签
BUCKET_LABEL = {"small": "小", "medium": "中", "large": "大"}

# DEFAULT_DFS_MAX_BUFFER：DFS 搜索窗口最大条数，超过则放弃全量搜索
DEFAULT_DFS_MAX_BUFFER = 42
# DEFAULT_DFS_MAX_NODES：DFS 最大访问节点数，防组合爆炸
DEFAULT_DFS_MAX_NODES = 300_000


def _load_bucket_of():
    """延迟加载 plan/细分规则.py 的 bucket_of 函数。"""
    try:
        from plan.细分规则 import bucket_of as _bucket_of
    except ImportError:
        from 细分规则 import bucket_of as _bucket_of
    return _bucket_of


_bucket_of_fn = None  # bucket_of 函数缓存（延迟加载）


def classify_bucket(spec: str, weight: int) -> str:
    """将克重归入规格下的小/中/大分区。"""
    global _bucket_of_fn
    if _bucket_of_fn is None:
        _bucket_of_fn = _load_bucket_of()
    lo, hi = SPECS[spec]["range"]
    if not (lo <= weight <= hi):
        raise ValueError(f"{weight}g 不在 {spec} 规格区间 {lo}-{hi}g")
    return _bucket_of_fn(weight)


@dataclass
class BufferFish:
    """DFS 缓存区内的鱼实体。"""

    fish_id: int    # 鱼 ID
    weight: int     # 克重
    bucket: str     # 小/中/大分区名


@dataclass
class BoxRecord:
    """一次成功封箱的记录。"""

    spec: str                           # 规格名
    count: int                          # 尾数
    weight: int                         # 总重（克）
    fish_ids: list[int]                 # 入选鱼 ID 列表
    fish_weights: list[int]             # 入选鱼克重列表
    parts: dict[str, int] = field(default_factory=dict)  # 小/中/大配比

    def to_dict(self) -> dict:
        """转为可序列化字典。"""
        return {
            "spec": self.spec,
            "count": self.count,
            "weight": self.weight,
            "fish_ids": self.fish_ids,
            "fish_weights": self.fish_weights,
            "parts": dict(self.parts),
        }


@dataclass
class PackStep:
    """配盒过程的单步日志。"""

    action: str                    # 动作类型（进鱼/成盒/回流）
    detail: str                    # 详情描述
    buffer_size: int = 0           # 当前缓存区条数
    box: BoxRecord | None = None   # 若本步成盒，记录 BoxRecord


def dfs_find_best_plan(
    buffer: list[BufferFish],
    spec: str,
    *,
    prefer_multi_bucket: bool = True,
    max_buffer: int = DEFAULT_DFS_MAX_BUFFER,
    max_nodes: int = DEFAULT_DFS_MAX_NODES,
) -> tuple[list[int], int, int] | None:
    """
    在 buffer 中深度优先搜索满足尾数与总重的最佳子集。

    尾数枚举 SPECS[spec]["counts"]（非固定 7–8），例如 15p→7/8 尾，20p→10/11 尾。
    总重须在 [4980, 5030] g；评分 |总重−5005|，缺区方案 +1.2。
    buffer 超过 max_buffer 或访问节点超过 max_nodes 时返回 None（由调用方回退 FIFO）。

    返回 (选中下标列表, 尾数, 总重)；无解返回 None。
    """
    if spec not in SPECS:
        raise KeyError(f"未知规格: {spec}")
    weights = [f.weight for f in buffer]
    n = len(buffer)
    counts = SPECS[spec]["counts"]
    min_count = min(counts)

    if n < min_count or n > max_buffer:
        return None

    # 预计算后缀升序列表，避免递归内反复 sort
    suffix_sorted = [sorted(weights[i:]) for i in range(n + 1)]

    def min_rest(start: int, need: int) -> int:
        tail = suffix_sorted[start]
        if len(tail) < need:
            return TARGET_MAX + 1
        return sum(tail[:need])

    def max_rest(start: int, need: int) -> int:
        tail = suffix_sorted[start]
        if len(tail) < need:
            return 0
        return sum(tail[-need:])

    best_indices: list[int] | None = None
    best_count = 0
    best_weight = 0
    best_score = float("inf")
    nodes = 0

    for target_count in counts:
        if n < target_count:
            continue

        picked: list[int] = []

        def dfs(start: int, total: int) -> None:
            nonlocal best_indices, best_count, best_weight, best_score, nodes
            nodes += 1
            if nodes > max_nodes:
                return
            need = target_count - len(picked)
            if need == 0:
                if TARGET_MIN <= total <= TARGET_MAX:
                    score = abs(total - TARGET_MID)
                    if prefer_multi_bucket:
                        buckets = {buffer[i].bucket for i in picked}
                        if len(buckets) < 3:
                            score += 1.2
                    if score < best_score:
                        best_indices = picked.copy()
                        best_count = target_count
                        best_weight = total
                        best_score = score
                return
            if n - start < need:
                return
            if total + min_rest(start, need) > TARGET_MAX:
                return
            if total + max_rest(start, need) < TARGET_MIN:
                return

            for i in range(start, n - need + 1):
                w = weights[i]
                new_total = total + w
                if new_total > TARGET_MAX:
                    continue
                picked.append(i)
                dfs(i + 1, new_total)
                picked.pop()

        dfs(0, 0)

    if best_indices is None:
        return None
    return best_indices, best_count, best_weight


def dfs_find_best_from_items(
    items: list,
    spec: str,
    *,
    prefer_multi_bucket: bool = True,
    max_buffer: int = DEFAULT_DFS_MAX_BUFFER,
    max_nodes: int = DEFAULT_DFS_MAX_NODES,
) -> tuple[list[int], int, int] | None:
    """引擎接入：items 需有 weight、bucket 属性（Fish 或 BufferFish）。"""
    buffer = [
        BufferFish(getattr(it, "id", idx + 1), it.weight, it.bucket)
        for idx, it in enumerate(items)
    ]
    return dfs_find_best_plan(
        buffer,
        spec,
        prefer_multi_bucket=prefer_multi_bucket,
        max_buffer=max_buffer,
        max_nodes=max_nodes,
    )


def fifo_head_find_from_items(
    items: list,
    spec: str,
) -> tuple[list[int], int, int] | None:
    """引擎接入：小/中/大 FIFO 队头前缀组合（buffer 过大或 DFS 超限时回退）。"""
    buffer = [
        BufferFish(getattr(it, "id", idx + 1), it.weight, it.bucket)
        for idx, it in enumerate(items)
    ]
    return fifo_head_find_plan(buffer, spec)


def fifo_head_find_plan(
    buffer: list[BufferFish],
    spec: str,
) -> tuple[list[int], int, int] | None:
    """
    模拟现有 BoxPlanner：按小/中/大 FIFO 队头前缀组合（非自由组合）。
    返回格式同 dfs_find_best_plan。
    """
    by_bucket: dict[str, list[int]] = {"small": [], "medium": [], "large": []}
    for idx, fish in enumerate(buffer):
        by_bucket[fish.bucket].append(idx)

    def prefix_sum(indices: list[int], take: int) -> int:
        return sum(buffer[i].weight for i in indices[:take])

    best: tuple[list[int], int, int] | None = None
    best_score = float("inf")

    for count in SPECS[spec]["counts"]:
        small_idx = by_bucket["small"]
        medium_idx = by_bucket["medium"]
        large_idx = by_bucket["large"]
        for a in range(min(len(small_idx), count) + 1):
            for b in range(min(len(medium_idx), count - a) + 1):
                c = count - a - b
                if c > len(large_idx):
                    continue
                weight = (
                    prefix_sum(small_idx, a)
                    + prefix_sum(medium_idx, b)
                    + prefix_sum(large_idx, c)
                )
                if not (TARGET_MIN <= weight <= TARGET_MAX):
                    continue
                score = abs(weight - TARGET_MID)
                if a == 0 or b == 0 or c == 0:
                    score += 1.2
                if score < best_score:
                    indices = small_idx[:a] + medium_idx[:b] + large_idx[:c]
                    best = (indices, count, weight)
                    best_score = score
    return best


class DepthFirstBufferPacker:
    """大容量池 → 有限缓存区 → DFS 成盒的独立配盒器。"""

    def __init__(self, spec: str, buffer_capacity: int, *, verbose: bool = False):
        """
        参数:
            spec: 规格名（如 15p）
            buffer_capacity: 缓存区容量上限
            verbose: 是否打印逐步日志
        """
        if spec not in SPECS:
            raise KeyError(f"未知规格: {spec}")
        if buffer_capacity < min(SPECS[spec]["counts"]):
            raise ValueError(
                f"缓存区容量 {buffer_capacity} 小于 {spec} 最小尾数 "
                f"{min(SPECS[spec]['counts'])}"
            )
        self.spec = spec                      # 规格名
        self.buffer_capacity = buffer_capacity  # 缓存区容量
        self.verbose = verbose                  # 详细日志开关
        self.buffer: list[BufferFish] = []      # 当前缓存区鱼列表
        self.cartons: list[BoxRecord] = []      # 已成盒记录
        self.overflow: list[BufferFish] = []    # 溢出（回流）鱼列表
        self.steps: list[PackStep] = []         # 过程步骤日志
        self._next_id = 1                       # 下一条鱼的自增 ID

    @property
    def min_count(self) -> int:
        """该规格最小合法装箱尾数。"""
        return min(SPECS[self.spec]["counts"])

    def _log(self, action: str, detail: str, box: BoxRecord | None = None) -> None:
        """记录一步操作到 steps 列表，verbose 时打印。"""
        self.steps.append(
            PackStep(action=action, detail=detail, buffer_size=len(self.buffer), box=box)
        )
        if self.verbose:
            print(f"[{action}] {detail} · 缓存 {len(self.buffer)}/{self.buffer_capacity}")

    def _make_box(self, indices: list[int], count: int, weight: int) -> BoxRecord:
        """按选中下标从缓存区取鱼并记录成盒。"""
        picked = [self.buffer[i] for i in sorted(indices)]
        parts: dict[str, int] = {}
        for fish in picked:
            parts[fish.bucket] = parts.get(fish.bucket, 0) + 1
        for i in sorted(indices, reverse=True):
            del self.buffer[i]
        box = BoxRecord(
            spec=self.spec,
            count=count,
            weight=weight,
            fish_ids=[f.fish_id for f in picked],
            fish_weights=[f.weight for f in picked],
            parts=parts,
        )
        self.cartons.append(box)
        parts_txt = "+".join(f"{BUCKET_LABEL[k]}{v}" for k, v in parts.items() if v)
        self._log(
            "成盒",
            f"#{len(self.cartons):04d} {self.spec.upper()} {count}尾 {weight}g ({parts_txt}) "
            f"鱼重 {[f.weight for f in picked]}",
            box=box,
        )
        return box

    def try_pack(self) -> BoxRecord | None:
        """尝试对当前缓存区执行 DFS 配盒，成功则移除鱼并返回 BoxRecord。"""
        if len(self.buffer) < self.min_count:
            return None
        plan = dfs_find_best_plan(self.buffer, self.spec)
        if not plan:
            return None
        indices, count, weight = plan
        return self._make_box(indices, count, weight)

    def feed(self, weight: int) -> BoxRecord | None:
        """大容量池进一条鱼；满容先尝试成盒，仍满则弹出队头至溢出区（模拟超容回流）。"""
        fish = BufferFish(
            fish_id=self._next_id,
            weight=weight,
            bucket=classify_bucket(self.spec, weight),
        )
        self._next_id += 1

        last_box: BoxRecord | None = None
        while len(self.buffer) >= self.buffer_capacity:
            packed = self.try_pack()
            if packed:
                last_box = packed
                continue
            reflowed = self.buffer.pop(0)
            self.overflow.append(reflowed)
            self._log(
                "回流",
                f"#{reflowed.fish_id} {reflowed.weight}g 缓存已满且未成盒 → 弹出队头",
            )
            if len(self.buffer) < self.buffer_capacity:
                break

        self.buffer.append(fish)
        self._log("进鱼", f"#{fish.fish_id} {weight}g → {BUCKET_LABEL[fish.bucket]}区")

        if len(self.buffer) >= self.min_count:
            packed = self.try_pack()
            if packed:
                return packed
        return last_box

    def run(self, pool: Iterable[int]) -> list[BoxRecord]:
        """批量喂入大容量池，批末扫尾封箱，返回全部成盒记录。"""
        for weight in pool:
            self.feed(weight)
        while len(self.buffer) >= self.min_count:
            if not self.try_pack():
                break
        return self.cartons

    def summary(self) -> dict:
        """返回配盒统计摘要字典。"""
        return {
            "spec": self.spec,
            "buffer_capacity": self.buffer_capacity,
            "cartons": len(self.cartons),
            "packed_fish": sum(c.count for c in self.cartons),
            "buffer_remaining": len(self.buffer),
            "overflow": len(self.overflow),
            "records": [c.to_dict() for c in self.cartons],
        }


def demo_doc_example(*, verbose: bool = True) -> None:
    """文档示例：大容量池 + 缓存区容量 11。"""
    pool_unit = [570, 689, 570, 571, 670, 671, 677, 680, 681]
    pool = pool_unit * 5
    buffer_capacity = 11

    print("=" * 64)
    print("深度优先缓存区配盒 · 15p 文档示例")
    print("=" * 64)
    print(f"大容量池: {len(pool)} 条 (单元 {pool_unit} × 5)")
    print(f"缓存区容量: {buffer_capacity}")
    print(f"配盒目标: {TARGET_MIN}-{TARGET_MAX}g · 尾数 {SPECS['15p']['counts']}")
    print()

    packer = DepthFirstBufferPacker("15p", buffer_capacity, verbose=verbose)
    packer.run(pool)
    info = packer.summary()

    print("\n--- 成盒结果 ---")
    for rec in packer.cartons:
        parts = "+".join(f"{BUCKET_LABEL[k]}{v}" for k, v in rec.parts.items() if v)
        print(
            f"  箱 #{rec.fish_ids[0] if rec.fish_ids else '?'}: "
            f"{rec.count}尾 {rec.weight}g ({parts}) "
            f"鱼重={rec.fish_weights}"
        )
    print(f"\n共成盒 {info['cartons']} 箱 · 装箱鱼 {info['packed_fish']} 条")
    print(f"缓存剩余 {info['buffer_remaining']} 条 · 溢出 {info['overflow']} 条")

    if packer.buffer:
        print(f"缓存剩余鱼重: {[f.weight for f in packer.buffer]}")

    print("\n--- 与 FIFO 队头法对比（成盒前 10 条缓存快照）---")
    snapshot = [570, 689, 570, 571, 670, 671, 677, 680, 681, 570]
    buf = [
        BufferFish(i + 1, w, classify_bucket("15p", w))
        for i, w in enumerate(snapshot)
    ]
    dfs_plan = dfs_find_best_plan(buf, "15p")
    fifo_plan = fifo_head_find_plan(buf, "15p")
    print(f"缓存快照 ({len(snapshot)} 条): {snapshot}")
    if dfs_plan:
        idx, cnt, wt = dfs_plan
        picked = [snapshot[i] for i in idx]
        print(f"  DFS 自由组合: {cnt}尾 {wt}g · 鱼重 {picked}")
    else:
        print("  DFS 自由组合: 无解")
    if fifo_plan:
        idx, cnt, wt = fifo_plan
        picked = [snapshot[i] for i in idx]
        print(f"  FIFO 队头组合: {cnt}尾 {wt}g · 鱼重 {picked}")
    else:
        print("  FIFO 队头组合: 无解（顺序约束导致无法成盒）")


def demo_interactive_steps(*, verbose: bool = True) -> None:
    """逐步演示：达到 7 尾后开始匹配。"""
    print("\n" + "=" * 64)
    print("逐步进鱼演示（前 12 条）")
    print("=" * 64)
    pool = [570, 689, 570, 571, 670, 671, 677, 680, 681, 570, 689, 570]
    packer = DepthFirstBufferPacker("15p", 11, verbose=verbose)
    for i, w in enumerate(pool, 1):
        box = packer.feed(w)
        if box:
            print(
                f"  >> 第 {i} 条进鱼后成盒: {box.count}尾 {box.weight}g "
                f"{box.fish_weights}"
            )
    print(f"累计成盒 {len(packer.cartons)} 箱")


if __name__ == "__main__":
    demo_doc_example(verbose=True)
    demo_interactive_steps(verbose=False)
