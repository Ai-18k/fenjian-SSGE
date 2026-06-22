# 纯算 `Scheduler_EngineV1.py` 脑图与运行流程说明

基于当前 [`Scheduler_EngineV1.py`](Scheduler_EngineV1.py) 及 `plan/` 子模块整理。  
核心特征：**三合一单料道**、**需求驱动入道/封箱**（`计算需求.py`）、**无 DFS**、**系统累计停留超时**。

---

## 一、总览脑图

```mermaid
mindmap
  root((Scheduler_EngineV1))
    全局常量与规格表
      SPECS / MODULE_SPECS
      TARGET_MIN/MAX 盒重
      DEFAULT_* 运行默认
      BUCKET_RANGES 小中大分区
    外部算法模块 plan/
      细分规则.py → classify_bucket
      随机种子生成.py → 批次 CSV
      计算需求.py → BoxDemandCalculator
    数据模型
      Fish 运行时鱼
      FishTrace 生命周期
      BoxPlan 成盒方案
      Stats 累计统计
    批次准备
      load_or_generate_batch
      record_to_fish
    物理状态 SortingLanes
      lane 三合一料道
      storage 暂存箱
      outside / reflow
    追踪 FishTracker
      register / mark_*
    主引擎 SchedulerEngine
      process_one 单步
      finish_batch 批末
      run 总入口
    输出产物 data/
      run_report / cartons
      remaining / timeout_tail
      run_log
```

---

## 二、全局变量与常量

```mermaid
mindmap
  root((全局配置))
    规格 SPECS
      range 克重区间
      counts 合法装箱尾数 如7-8
    模块 MODULE_SPECS
      A 15p-40p
      B 45p-90p
      C 100p-150p
    盒重目标
      TARGET_MIN 4980g
      TARGET_MAX 5030g
      TARGET_MID 5005g
    分区 BUCKETS
      small / medium / large
      BUCKET_RANGES 各规格克重切分
    运行默认
      DEFAULT_SEED 42
      DEFAULT_TOTAL 25000
      DEFAULT_MOVE_TIMEOUT 180
      DEFAULT_STORAGE_CAPACITY 500
      DEFAULT_CAP_FACTOR 2 兼容保留
    结束模式
      STOP_MODE_COUNT 按条数
      STOP_MODE_WEIGHT 按总重
    超时时钟
      TIMEOUT_CLOCK_INTAKE 每步+1
      TIMEOUT_CLOCK_REAL 墙钟秒
```

| 变量 | 职责 |
|------|------|
| `SPECS` | 18 规格字典：`range` 单尾克重区间，`counts` 合法成盒尾数元组 |
| `MODULE_SPECS` | 三大模块各自包含的 6 个规格 |
| `DEFAULT_ENABLED_SPECS` | 默认启用规格（当前 15p–40p） |
| `BUCKET_RANGES` | 启动时按 `细分规则.py` 预计算各规格小/中/大克重区间 |
| `BoxDemandCalculator` | 从 `计算需求.py` 动态加载，判定入道/封箱 |

---

## 三、数据模型脑图

```mermaid
mindmap
  root((数据模型))
    Fish 运行时实体
      id weight spec bucket
      enter_time 当前队列入队时刻
      rounds 系统轮次
    FishTrace 全生命周期
      first_in_time 首次入系统
      outbound_time 出站时刻
      status queued/packed/unmatched_*
      lane_wait_s 超时时段等待
      dwell_time 属性 总停留
    BoxPlan 成盒记录
      spec count weight parts
      fish 入选鱼列表
    Stats 引擎累计
      input_count/weight
      cartons packed_fish
      storage_* 暂存进出/峰值
      timeout_tail storage_timeout_tail
      tail_count unmatched_count
```

### 关键时间语义

| 字段 | 含义 | 何时写入/重置 |
|------|------|----------------|
| `Fish.enter_time` | 进入**当前队列**的时刻 | 入料道/暂存/出队变队首时重置 |
| `FishTrace.first_in_time` | **首次**进入系统 | `register()` 首次登记，不再变 |
| `_fish_system_dwell()` | 超时判定用 | `tick - first_in_time`（料道↔暂存不重置） |

---

## 四、工具函数分层

```mermaid
mindmap
  root((模块级函数))
    规格与批次
      expand_spec_list
      normalize_enabled_specs
      classify_spec 按克重归规格
      classify_bucket 小中大
      batch_csv_path*
      load_or_generate_batch
      record_to_fish
    料道容量
      spec_min_count / spec_max_count
      spec_total_capacity = max counts
    需求计算桥接
      lane_inventory_weights
      lane_demand_weight_ranges
      fish_matches_lane_demand
      weight_in_ranges
    尾料与报表
      TAIL_STATUS_LABEL
      sum_unmatched_traces
      describe_tail_trace
```

| 函数 | 输入 → 输出 | 职责 |
|------|-------------|------|
| `lane_inventory_weights` | `SortingLanes, spec` → 克重列表 | 料道当前鱼重量（不含暂存） |
| `lane_demand_weight_ranges` | 料道状态 → `[(lo,hi),…]` | 下一条可进鱼的克重区间 |
| `fish_matches_lane_demand` | `Fish` → bool | `BoxDemandCalculator.check_incoming_fish` |
| `record_to_fish` | CSV 记录 → `Fish` | 赋 spec/bucket/enter_time |
| `load_or_generate_batch` | seed/total/规格 → 记录列表 | 读缓存或 `随机种子生成.py` 生成 |

---

## 五、`FishTracker` 脑图

```mermaid
mindmap
  root((FishTracker))
    存储
      traces dict id→FishTrace
      unmatched 尾料列表
    register
      首次入系统 first_in_time
      更新 status/rounds
    mark_packed
      status=packed outbound_time
    mark_stored
      status=stored 进暂存
    mark_timeout_tail
      unmatched_timeout / storage_timeout
      记录 lane_wait_s
    mark_unmatched
      批末/箱满等尾料
    mark_reflow
      超容回流 现流程少用
    导出
      save_report CSV
      remaining_records JSON行
```

| 方法 | 职责 |
|------|------|
| `register` | 鱼首次入系统或更新轮次/状态 |
| `mark_packed` | 封箱成功，写 `outbound_time` |
| `mark_stored` | 进入暂存箱 |
| `mark_timeout_tail` | 超时淘汰，记入 `unmatched` |
| `mark_unmatched` | 批末/箱满等尾料 |
| `save_report` | 导出 `run_report_seed_*.csv` |
| `remaining_records` | 尾料 JSON 行，供前端/API |

---

## 六、`SortingLanes` 物理状态脑图

```mermaid
mindmap
  root((SortingLanes))
    结构
      lane spec→FIFO Fish列表 三合一
      storage 暂存箱 FIFO
      outside 规格外
      reflow 回流 通常为空
      storage_capacity 500
    查询
      total_in_spec 料道总条数
      lane_room 剩余空位
      bucket_fish 按小中大筛
      storage_count
    入队
      enqueue 直接入料道或outside
      try_enqueue_reflow 回流再入道
      try_push_storage 进暂存
      _put_in_lane append FIFO
    出队/转移
      pop_lane_head 队首弹出
      take_lane_all 整道清空封箱用
      transfer_storage_to_lane 暂存→料道
      remove_from_storage_ids
    超时
      discard_head_timeout 队首淘汰
```

### 料道容量公式

```
spec_total_capacity(spec) = max(SPECS[spec]["counts"])
例：15p counts=(7,8) → 料道最多 8 条（三合一合计，不分小中大容量）
```

| 方法 | 职责 |
|------|------|
| `lane_room` | `cap - total_in_spec`，是否有空位 |
| `enqueue` | 规格内 append 料道 / 规格外进 outside |
| `try_push_storage` | 暂存未满则入箱，写 `enter_time` |
| `transfer_storage_to_lane` | 按候选顺序从暂存移入料道 |
| `take_lane_all` | 封箱时清空整道 |
| `discard_head_timeout` | 队首超时弹出并记尾料 |

---

## 七、`SchedulerEngine` 方法脑图

```mermaid
mindmap
  root((SchedulerEngine))
    初始化 __init__
      batch _cursor tick
      lanes tracker stats cartons
      move_timeout cap_factor
      stop_mode stop_count/weight
    时钟
      _advance_tick
      _sync_real_tick
      _fish_system_dwell
    日志
      _emit _log _log_flow
      _open/_close_run_log
      _storage/_timeout/_lane_status
      _note_storage_peak
      _warn_timeout_pressure
    需求诊断
      _best_diagnostic_for_spec
      _demand_for_spec
      _active_storage_demands
    封箱
      _seal_lane_box 整道封箱
      _try_pack_spec
      _try_pack_all
    入料路由
      _route_spec_intake
      _push_intake_storage
      _intake_matches_demand
    暂存/回流
      _process_reflow_intake
      _release_storage_by_demands
    超时
      _enforce_timeouts
      _monitor_storage
      _anti_block
    主循环
      process_one
      finish_batch
      run
    报表
      build_final_metrics
      print_config/report/quick_summary
      _save_*_csv
```

### `SchedulerEngine` 实例变量

| 变量 | 职责 |
|------|------|
| `batch` / `_cursor` / `total_fish` | 预加载批次与读取游标 |
| `tick` | 仿真时钟（步或秒） |
| `lanes` | 料道 + 暂存 + 规格外物理状态 |
| `tracker` | 每条鱼生命周期 |
| `stats` | 全局计数器 |
| `cartons` | 已成盒 `BoxPlan` 列表 |
| `move_timeout` | 系统累计停留超时阈值 |
| `timeout_tail_log` | 超时鱼明细，导出 CSV |
| `finished` | 批末是否完成 |

---

## 八、核心运行流程（总入口）

```mermaid
flowchart TD
    A[main / run_demo] --> B[load_or_generate_batch]
    B --> C[SchedulerEngine.__init__]
    C --> D[run]
    D --> E[print_config + _open_run_log]
    E --> F{process_one 返回 True?}
    F -->|是| G[可选 sleep interval]
    G --> F
    F -->|否| H[_enforce_timeouts 最后一轮]
    H --> I[finish_batch]
    I --> J[print_quick_summary / print_report]
    J --> K[关闭日志文件]
```

---

## 九、`process_one()` 单步详细流程

**每步处理批次中恰好 1 条鱼**，是仿真的心脏。

```mermaid
flowchart TD
    START([process_one 开始]) --> C1{_cursor >= total_fish<br/>或 _intake_complete?}
    C1 -->|是| END0([返回 False 结束循环])
    C1 -->|否| T1[_advance_tick +1]
    T1 --> T2[_enforce_timeouts 入料前清超时]
    T2 --> R1[取 batch_cursor → record]
    R1 --> R2[_cursor += 1]
    R2 --> S1[累计 stats.input_count/weight]
    S1 --> F1[record_to_fish → Fish]

    subgraph PRE [入料前]
        P1[_process_reflow_intake]
        P2[_release_storage_by_demands]
        P3[_try_pack_all]
        P4[_note_storage_peak]
    end

    F1 --> PRE

    subgraph ROUTE [入料路由]
        O1{规格外?}
        O1 -->|是| O2[lanes.enqueue → outside]
        O1 -->|否| O3[_route_spec_intake]
        O3 --> O4{匹配需求且料道未满?}
        O4 -->|是| O5[入料道 + _try_pack_spec]
        O4 -->|否| O6[_push_intake_storage]
    end

    PRE --> ROUTE

    subgraph POST [入料后]
        Q1[_release_storage_by_demands]
        Q2[_try_pack_all]
        Q3[_enforce_timeouts]
        Q4[_note_storage_peak]
        Q5[_warn_timeout_pressure]
    end

    ROUTE --> POST
    POST --> LOG{input_count % log_every == 0?}
    LOG -->|是| LOGP[打印进度]
    LOG -->|否| RET
    LOGP --> RET([返回 not _intake_complete])
```

### 9.1 `_route_spec_intake` 决策树

```mermaid
flowchart TD
    A[规格内鱼] --> B{fish_matches_lane_demand?}
    B -->|否| S1[暂存箱 原因:需求不匹配]
    B -->|是| C{total_in_spec < cap?}
    C -->|否| S2[暂存箱 原因:料道已满]
    C -->|是| D[lanes.enqueue 入三合一FIFO]
    D --> E[_try_pack_spec 立即尝试封箱]
```

### 9.2 `_seal_lane_box` 封箱逻辑

```mermaid
flowchart TD
    A[取 lane 全部鱼重量] --> B{BoxDemandCalculator.calc<br/>meets_requirement?}
    B -->|否| X([不封箱])
    B -->|是| C[take_lane_all 整道取出]
    C --> D[统计小中大配比 → BoxPlan]
    D --> E[tracker.mark_packed 每条鱼]
    E --> F[stats.cartons++ stats.packed_fish+=n]
    F --> G[cartons.append + 日志]
```

> **无 DFS**：料道鱼一旦 `meets_requirement`，整道 FIFO 全部成盒。

### 9.3 `_release_storage_by_demands` 暂存出库

```mermaid
flowchart TD
    A[遍历每个启用 spec] --> B{lane_room > 0?}
    B -->|否| NEXT[下一规格]
    B -->|是| C[筛暂存中 spec 相同且需求匹配的鱼]
    C --> D{有候选?}
    D -->|否| NEXT
    D -->|是| E[按 _fish_system_dwell 降序 快超时优先]
    E --> F[transfer_storage_to_lane 移 1 条]
    F --> G[_try_pack_spec]
    G --> B
```

### 9.4 超时处理 `_enforce_timeouts`

```mermaid
flowchart TD
    A[_enforce_timeouts] --> B[_monitor_storage]
    B --> C{暂存中最久鱼<br/>system_dwell >= 阈值?}
    C -->|是循环| D[移除 → unmatched_storage_timeout]
    C -->|否| E[_anti_block]
    E --> F{各规格料道队首<br/>system_dwell >= 阈值?}
    F -->|是循环| G[discard_head_timeout → unmatched_timeout]
    F -->|否| END([结束])
```

**超时语义**：以 `first_in_time` 起的**系统累计停留**判定；日志同时记录 `lane_wait_s`（本次队列等待）。  
同一步内用 `while` 循环清完所有已超时鱼，避免积压。

---

## 十、`finish_batch()` 批末流程

```mermaid
flowchart TD
    A[finish_batch] --> B[循环最多5000步]
    B --> C[回流入道 + 暂存出库 + 封箱]
    C --> D{timeout_in_finish?}
    D -->|是| E[_enforce_timeouts]
    D -->|否| F{成盒数不变 且 reflow/storage 空?}
    E --> F
    F -->|否| B
    F -->|是| G[料道剩余 → unmatched_tail]
    G --> H[暂存剩余 → unmatched_storage]
    H --> I[回流剩余 → unmatched_reflow]
    I --> J[导出 4 份 CSV]
    J --> K[finished = True]
```

| 批末产物 | 路径 | 内容 |
|----------|------|------|
| `run_report_seed_*.csv` | `tracker.save_report` | 全批次鱼生命周期 |
| `cartons_seed_*.csv` | `_save_cartons_csv` | 成盒明细 |
| `remaining_seed_*.csv` | `_save_remaining_csv` | 尾料明细 |
| `timeout_tail_seed_*.csv` | `_save_timeout_tail_csv` | 超时鱼明细 |

默认 `timeout_in_finish=False`：批末扫尾阶段**不继续超时淘汰**，剩余鱼直接标为批末尾料。

---

## 十一、鱼在系统中的状态流转

```mermaid
stateDiagram-v2
    [*] --> 批次记录: load_or_generate_batch
    批次记录 --> 入料: process_one
    入料 --> 料道: 需求匹配且未满
    入料 --> 暂存箱: 不匹配或料道满
    入料 --> 规格外: outside
    暂存箱 --> 料道: _release_storage_by_demands
    料道 --> 成盒: _seal_lane_box meets_requirement
    料道 --> 超时尾料: _anti_block
    暂存箱 --> 超时尾料: _monitor_storage
    暂存箱 --> 箱满尾料: try_push_storage 失败
    料道 --> 批末尾料: finish_batch 扫尾
    暂存箱 --> 批末尾料: finish_batch 扫尾
    成盒 --> [*]
    超时尾料 --> [*]
    批末尾料 --> [*]
    规格外 --> [*]
```

---

## 十二、方法职责速查表

### 批次层

| 方法 | 职责 |
|------|------|
| `batch_total_for_run` | 按总重模式估算需预生成条数上限 |
| `load_or_generate_batch` | 读/写 `fish_seed_*.csv` 或按重生成 |
| `record_to_fish` | CSV 行 → 运行时 `Fish` |

### `SchedulerEngine` 核心业务

| 方法 | 调用时机 | 职责 |
|------|----------|------|
| `process_one` | 主循环每步 | 驱动整条流水线 |
| `_route_spec_intake` | 入料 | 需求匹配路由 |
| `_try_pack_all` | 入料前/后 | 全规格尝试封箱 |
| `_release_storage_by_demands` | 入料前/后 | 暂存按需求+快超时出库 |
| `_enforce_timeouts` | 入料前/后/批末 | 清暂存+料道超时 |
| `finish_batch` | 入料结束 | 扫尾封箱+标尾料+写 CSV |
| `build_final_metrics` | 报表 | 汇总装箱率/超时/峰值 |

### 入口

| 方法 | 职责 |
|------|------|
| `run` | `while process_one` → `finish_batch` → 打印结果 |
| `run_demo` | 封装加载批次+创建引擎+运行 |
| `main` | CLI 参数解析 → 创建 `SchedulerEngine` |

---

## 十三、单步时序（文字版）

以一条**规格内、需求匹配**的鱼为例：

```
tick+1
  → 入料前清超时（暂存最久 + 各料道队首）
  → 读 record #N，转 Fish，累计入料统计
  → 回流队列尝试再入道（通常空）
  → 暂存箱：有空位则按需求+快超时逐条出到料道，每条出完尝试封箱
  → 全规格扫描封箱（料道达标则整道成盒）
  → 记录暂存峰值
  → 本鱼：需求匹配且料道未满 → append 料道 FIFO → 本规格再封箱
  → 暂存再出库一轮 + 再封箱
  → 再清超时 + 峰值 + 超时预警（verbose）
  → 每 500 条打进度日志
```

以一条**需求不匹配**的鱼为例：

```
…（入料前同上）…
  → 本鱼：不匹配 → try_push_storage
       成功：stats.storage_in++，status=stored
       失败：mark_unmatched storage_full
…（入料后同上）…
```

---

## 十四、与 `plan/计算需求.py` 的衔接

```mermaid
flowchart LR
    subgraph 入料判定
        A1[lane_inventory_weights] --> A2[BoxDemandCalculator.check_incoming_fish]
        A2 --> A3{acceptable?}
    end
    subgraph 封箱判定
        B1[lane_inventory_weights] --> B2[BoxDemandCalculator.calc]
        B2 --> B3{meets_requirement?}
        B3 -->|是| B4[整道 take_lane_all]
    end
    subgraph 暂存广播
        C1[lane_demand_weight_ranges] --> C2[next_fish_ranges 克重区间]
        C2 --> C3[暂存鱼 weight 是否落在区间]
    end
```

---

## 十五、进鱼随机生成（`plan/随机种子生成.py`）

| 变量/参数 | 默认值 | 含义 |
|-----------|--------|------|
| `DEFAULT_OUTSIDE_RATE` | `0.01` | 约 **1%** 超规鱼 |
| `DEFAULT_SEED` | `42` | 随机种子，可复现 |
| `enabled_specs` | 如 module-a 6 规格 | 仅在这些规格内生成「规格内鱼」 |

**规格内鱼**：在启用规格中等概率选一档，克重在 `range` 内均匀随机。  
**超规鱼**：低于/高于启用区间，或落在未启用规格区间。  
生成后 `shuffle` 打乱顺序，赋 `id=1..N`。

---

## 十六、CLI 常用参数

```bash
# 快速跑 10 吨
python Scheduler_EngineV1.py --fast --seed 42 -w 10 --move-timeout 240 --specs module-a

# 按条数 + 完整报告
python Scheduler_EngineV1.py --fast -n 3000 --report -v

# 批末继续超时淘汰（旧行为）
python Scheduler_EngineV1.py ... --timeout-in-finish

# 指定运行日志
python Scheduler_EngineV1.py --log-file data/my_run.log ...
```

| 参数 | 含义 |
|------|------|
| `--seed` | 随机种子 |
| `--specs` | 启用规格，逗号分隔或 `module-a/b/c` |
| `--move-timeout` | 超时阈值（步或秒，见 `--timeout-clock`） |
| `-n` / `-w` | 按条数 / 按总重（吨）结束 |
| `--fast` | 不 sleep、静默、单行结果 |
| `-v` | 详细流向日志 |
| `--report` | 批末完整汇总 |
| `--timeout-in-finish` | 批末扫尾是否继续超时淘汰 |

---

*文档与 `Scheduler_EngineV1.py` 同步维护；料道结构为三合一单 FIFO，封箱无 DFS。*
