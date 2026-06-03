# ImprovedFishBoxing501 Python 迁移说明

本文档说明 `improved_fish_boxing_501.py` 的结构、运行方式和迁移对应关系。该 Python 版本按原 Java 类 `ImprovedFishBoxing501` 的静态变量、流程函数、缓冲池策略、暂存箱策略和 DFS 组合搜索逻辑迁移。

## 1. 文件说明

- `improved_fish_boxing_501.py`：Python 主程序，包含 Java 中的主类逻辑以及 `Fish`、`Box`、`BoxConfig` 三个等价数据类。
- `improved_fish_boxing_501_说明文档.md`：当前说明文档。

## 2. 运行方式

直接运行：

```bash
python improved_fish_boxing_501.py
```

默认入口与 Java `main` 保持一致：

```python
main_function(100, 5011, 140)
```

也就是执行 100 个批次，测试编号 `5011`，缓冲池限制 `140`。

如果只想测试一小段逻辑，可以在交互环境中改小总重量：

```python
import improved_fish_boxing_501 as m
m.limit_weight = 100_000
m.main_function(1, 5011, 140)
```

## 3. 核心常量

| Python 变量 | Java 对应 | 含义 |
|---|---|---|
| `limit_weight` | `limitWeight` | 模拟总重量上限，默认 `10t - 100kg` |
| `MAX_WEIGHT` | `MAX_WEIGHT` | 单箱最大重量，5030g |
| `MIN_WEIGHT` | `MIN_WEIGHT` | 单箱最小重量，4980g |
| `BUFFER_SIZE_LIMIT` | `BUFFER_SIZE_LIMIT` | 缓冲池最大限制 |
| `CACHE_BOX_PER_SPEC` | `CACHE_BOX_PER_SPEC` | 每个规格的暂存箱数量，默认 4 |
| `FISH_MAX_ROUND` | `FISH_MAX_ROUND` | 超时鱼判断轮次差，默认 600 |

## 4. 数据结构迁移

### 4.1 Fish

Java 原逻辑依赖 `Fish#getId()`、`getWeight()`、`getSpec()` 和 `getPrintStr()`。Python 中使用：

```python
@dataclass(eq=False)
class Fish:
    id: int
    weight: int
    status: int
    spec: str
```

`eq=False` 是为了让 `list.remove`、对象匹配等行为尽量接近 Java 默认对象身份语义。

### 4.2 BoxConfig

对应规格配置，包含：规格名、最小条数、最大条数、单鱼重量区间、可参与匹配的相邻规格列表。

### 4.3 Box

对应完成箱或失败暂存箱记录，包含：规格、鱼列表、总重、条数。

### 4.4 buffer_map

Java 使用 `LinkedHashMap<String, List<Fish>>`。Python 使用 `OrderedDict[str, List[Fish]]`，保持按规格插入顺序，同时每个规格内部的鱼列表保持 FIFO。

## 5. 主流程说明

### 5.1 main_function

执行多轮模拟：

1. 调用 `init_box_config()` 初始化本批状态。
2. 设置 `BUFFER_SIZE_LIMIT`。
3. 调用 `simulate_fish_flow()` 生成鱼流并持续处理。
4. 打印箱子、暂存箱、缓冲池、回流、完成率、剩余率等统计信息。
5. 记录最终摘要到 `result`。

说明：原 Java 中 `saveLogs(...)` 调用是注释状态，Python 版也默认不主动保存数据库。

### 5.2 simulate_fish_flow

持续生成随机鱼，直到总重量达到 `limit_weight`：

1. `generate_random_fish(i)` 随机选择一个规格配置，再在该规格重量区间中随机生成一条鱼。
2. 遍历缓冲池，如果当前鱼 ID 与缓冲池鱼 ID 差值大于等于 `FISH_MAX_ROUND`，记录到 `out_time_fish`。
3. 累计总重量。
4. 调用 `process_new_fish(fish)` 进入核心装箱流程。
5. 定期把 `reflow_fish` 回流重新处理。
6. 最后调用 `finish_with_buffer()` 做收尾匹配。

## 6. 新鱼处理逻辑

`process_new_fish(fish)` 是核心入口，保持三阶段流程：

1. **直接装箱优先**：调用 `try_direct_packing(spec_idx, fish)`，尝试用“当前新鱼 + 同规格暂存箱 + 缓冲池鱼”完成一箱。
2. **放入暂存箱**：如果直接装箱失败，调用 `try_place_in_cache_box(spec_idx, fish)`，优先放入已有同规格暂存箱，再尝试空暂存箱。
3. **进入缓冲池**：如果暂存箱不能接收，则进入缓冲池。若缓冲池已满，先尝试全局匹配，失败后挤出最老鱼到 `reflow_fish`。

## 7. 装箱匹配策略

### 7.1 try_direct_packing

遍历该规格的 4 个暂存箱。对每个非空暂存箱：

- 若“暂存箱 + 新鱼”已经满足重量和条数，直接完成装箱。
- 否则计算还需要补充的重量范围和条数范围。
- 先从同规格缓冲池中找组合。
- 再尝试“1 条相邻规格鱼 + 若干同规格鱼”的组合。

### 7.2 try_pack_from_cache

不包含新鱼，尝试让已有暂存箱加缓冲池鱼完成装箱。逻辑和 `try_direct_packing` 的补鱼部分基本一致。

### 7.3 try_empty_box_packing

当所有暂存箱无法匹配时，尝试直接从缓冲池中选鱼组成一箱。

### 7.4 try_relaxed_complete

收尾阶段的宽松匹配：

1. 先处理半成品暂存箱。
2. 再尝试缓冲池直接装箱。

## 8. FreqTable 组合搜索

`FreqTable` 是原 Java 代码中的核心数据结构，Python 版保持同名逻辑：

1. 将鱼按重量分组。
2. 每个重量组内部保持 FIFO。
3. `find_combination(k, low, high)` 寻找恰好 `k` 条鱼，且总重落在 `[low, high]`。
4. 先用 `min_sum(k)` 和 `max_sum(k)` 剪枝。
5. 再用 DFS 枚举每个重量取 `0~maxTake` 条。

## 9. 规格配置

Python 版 `get_box_config_map()` 保留完整配置：`15p` 到 `150p`。但 `init_box_config()` 中实际启用的规格与 Java 当前代码一致：

```python
configs = [
    config_map["20p"],
    config_map["25p"],
    config_map["30p"],
    config_map["35p"],
    config_map["40p"],
    config_map["45p"],
]
```

其他规格保留为注释或备用配置。

## 10. 与 Java 保持一致的细节

以下细节容易被误改，Python 版已按原逻辑保留：

1. `simulate_fish_flow(fish_count)` 传入了 `fish_count`，但原 Java 实际按 `limitWeight` 停止，未使用 `fishCount`；Python 版也不使用该参数控制停止。
2. `init_box_config()` 没有清空 `failBoxList`；Python 版同样没有清空 `fail_box_list`，因此多轮运行时失败箱会累计。
3. `check_and_pack_spec()` 中 `configIndex > 0` 的判断照搬，意味着索引为 0 的配置不会参与该分支计算。
4. 相邻规格补鱼时，原 Java 会对同规格频率表执行 `remove(adj)`，但 `adj` 来自相邻规格，通常不会删除任何鱼；Python 版保留该行为。
5. `complete_box()` 完成装箱后会立即从缓冲池补充同规格鱼进入刚清空的暂存箱。
6. 统计中的重量成功率保留原 Java 的计算方式：`100 - remain_weight / total_fish_weight`，不是 `100 - 剩余百分比 * 100`。

## 11. 数据库日志说明

Java 原代码有 `saveLogs`，但调用处是注释。Python 版保留 `save_logs`，但没有硬编码数据库密码，改用环境变量：

```bash
set FISH_DB_HOST=数据库地址
set FISH_DB_PORT=3306
set FISH_DB_NAME=fish_test
set FISH_DB_USER=用户名
set FISH_DB_PASSWORD=密码
```

Linux/macOS：

```bash
export FISH_DB_HOST=数据库地址
export FISH_DB_PORT=3306
export FISH_DB_NAME=fish_test
export FISH_DB_USER=用户名
export FISH_DB_PASSWORD=密码
```

需要安装依赖：

```bash
pip install pymysql
```

## 12. 注意事项

- Python 和 Java 的随机数实现不同，因此即使逻辑一致，随机生成的样本序列不会完全一样。
- 若要对比 Java 与 Python 的逐条结果，需要改造成固定输入鱼流，而不是两边各自随机生成。
- 默认 `main_function(100, 5011, 140)` 运行量较大，调试时建议先降低 `limit_weight` 或只跑 1 轮。
