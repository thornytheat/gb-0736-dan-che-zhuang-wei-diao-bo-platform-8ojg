# 数据字典

## 班次和输入

时间字段必须带时区偏移。班次、容量版本与潮汐窗口均为 `[开始, 结束)`。接口和归档统一输出 UTC，工作台显示北京时间。车辆、桩位与记录数使用整数。

| 文件 | 字段 | 含义 |
|---|---|---|
| `sample_data/shift.json` | `start`、`end`、`name` | 班次起止与显示名 |
| 同上 | `stations`、`opening`、`opening_road` | 站号到站名和期初站内车辆数。期初在骑车辆按全网单池记账 |
| 同上 | `trucks`、`load_limit`、`available_at`、`station` | 调拨车出班信息。载重以可装车辆数计 |
| 同上 | `min_travel_minutes` | 任意两站间估算的最短通行分钟数 |
| `sample_data/events.csv` | `event_id`、`station`、`kind` | 不透明事件号、站号与尝试类型 `rent` 或 `return` |
| 同上 | `occurred_at`、`received_at` | 实际发生与补传时间。收件延迟达 300 秒标为迟报 |
| 同上 | `note` | 可空，保留引号内换行 |
| `sample_data/capacity.csv` | `station`、`revision`、`start`、`end`、`slots`、`note` | 容量修订的区间、桩数与备注。`0` 表示关站 |
| `sample_data/plan.json` | `id`、`truck`、`from`、`to`、`depart`、`arrive`、`count` | 行程键、车辆、装卸地点与时点、装卸车辆数。行程时点支持毫秒 |
| `sample_data/shift.json` | `next_shift` | 下一班窗口 `name/start/end` 与车辆接续表，车辆项含驻站 `station`、最早可用 `available_at`、载重和接收班组 `crew`。跨班交接依赖该字段 |

CSV 上限为 256 KiB 和 2000 条逻辑记录。`line` 是记录起始的物理行号。`raw` 保存原始 CSV 字段，字段前后空白在归一列中清除。必填字段的空串、`-`、`N/A`、`NULL` 和 `未知` 单列原因码。同一事件号、站点、租还动作和发生时点相等时，保留收件最早的一行，同收件时点保留源行号最小的一行。一个事件号的有效候选出现业务矛盾时，全部隔离。

## 时间线、潮汐与库存

- 同一时点先切换容量，再卸车，再装车，再处理租还。各组同秒按站号、行程号或事件号的字符串顺序裁决。
- 同站同修订号的重叠记录全部隔离；不同修订号覆盖同一时段时取修订号最大的有效记录。缺失有效版本时报 `CAPACITY_GAP`。
- 缩容时多出的车辆转站内暂存。暂存车占用库存，遇到复开、借车或装车腾出的桩，先在站内补桩。车池只在成功借还时增减。
- 15 分钟潮汐槽位的占用和空桩取窗口最后状态，借还尝试和未成交是窗口内发生次数。关站标记反映窗口最后状态。点击任一槽位可定位该站和时段的时间线。失败需求留存原编号和原因，并保持库存原值。
- 调拨行程先校验载重、时间和车辆路径，再按对应该时点的桩位状态模拟装卸。每辆车在装车时扣出站库存，卸车后才进入目标站。站点容不下整趟或装车数量不足时整份试算失败。草稿最多 20 趟。
- 比较使用同一组已采信事件，差量为方案未成交数减基准未成交数。负数表示净改善，原因改变但仍未成交的尝试也进入逐笔差异。

## API 写入与证据

| 入口 | 请求字段 | 结果 |
|---|---|---|
| `POST /api/intake` | `multipart/form-data` 的 `expected` 和 `events` | 以替换事件和当前方案生成新班次 |
| `POST /api/dispatch/preview` | JSON `run`、`legs` | 返回试算差量、调拨动作、勾稽与槽位 |
| `POST /api/dispatch/publish` | JSON `expected`、`legs` | 校验并发布方案 |
| `POST /api/runs/rerun` | JSON `expected` | 按封存原文件与已发布方案复算 |
| `POST /api/runs/restore` | JSON `expected`、`target` | 原子回切到归档版本 |

`expected` 为写入时所见当前班次完整编号。查询七个阶段时可通过 `?run=<编号>` 固定证据版本。写入响应的 `created` 表示新增归档，`changed` 表示响应编号与所提交的 `expected` 不同。无效请求返回 `code`、`message`、`details`。

## 跨班交接

| 入口 | 请求字段 | 结果 |
|---|---|---|
| `POST /api/handover/preview` | JSON `run`、`cutoff`、`signoffs` | 不落库，返回交接执行单试算 |
| `POST /api/handover/publish` | JSON `expected`、`cutoff`、`signoffs` | 事务内发布交接单并写入签收留档 |
| `GET /api/handover` | 可选 `?handover=<编号>` | 查看生效或指定交接单与历史 |
| `GET /api/handover/<id>/evidence.json` | 路径编号 | 下载含签收原文的证据包及 SHA-256 |

`cutoff` 为交接时点，必须落在本班开始（不含）至下一班开始（含）之间。`signoffs` 每项字段为 `leg`（行程键）、`kind`（`accepted` 确认卸车 / `rejected` 拒收）、`count`（本张签收的辆数）、`at`（签收时点）、`text`（签收原文，必填）。同一行程、时点、原文、辆数重复上报标记 `duplicate=true`，已卸辆数只计一次；原文仍逐张写入 `handover_papers`，拒收与冲突结论均留痕。同行程出现互相矛盾的有效结论返回 `SIGNOFF_CONFLICT`，累计确认辆数超过行程装车数返回 `SIGNOFF_OVER_COUNT`。

执行单关键字段：`legs[].status` 取 `done/partial/arrived_pending/in_transit/pending`，`unloaded` 为去重后已卸、`remaining_on_truck` 为随车剩余；`receivers` 按下一班车辆给出接收班组与辆数；`opening` 为下一班期初各站库存，由“无调拨基准 + 实际装卸”迷你重放推导；`demand.carried` 保留未成交原事件号、原因码与站点缓解量；`options.original` 与 `options.rerouted` 分别是原路线续跑和按下一班缺车（未成交借车笔数）重排的下一班行程。`expires_at` 即旧行程失效时点，等于下一班开始时间。交接单编号由班次号、交接时点、执行结果与规范化签收单计算，同单重试幂等；发布失败或 `STALE_RUN` 不改动旧行程与旧库存。

运行编号由原始文件的 SHA-256 与字节数、规范化方案和规则版本计算。相同输入重跑会比对已存结果，原生成时间、原耗时与证据文件保持一致。证据 SHA-256 针对 `evidence` 对象，编码为 UTF-8，键名排序、紧凑 JSON，中文直接写入。只有下载包外层另附证据摘要。

## 模拟边界

期初在骑车辆为匿名池，结果按尝试分配成功与失败。单次尝试只计一辆，缺车或满桩后的自行绕行以独立尝试出现。调拨车按已填装卸站顺序行驶，行程之间暂不模拟空车换站、道路拥堵或司机班表。工作台用随项目发布的班次与容量数据，事件 CSV 可替换。
