# 单车桩位调拨台规则与测试索引

- 规模为中型。供站点调度员复查早高峰尝试、核算未成交并试算补车方向。
- 同一批有效尝试以发生时间进入无调拨基准和候选方案。动态容量、关站暂存、车载在途及在骑车辆进入同一守恒账。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 桩事件收件 | 上传事件或核对隔离、迟报和同号矛盾 | `GET /api/intake` `POST /api/intake` | `app/modules/intake/service.py` `app/modules/intake/reader.py` | `tests/test_intake.py` |
| M2 容量版本 | 核对每站有效容量和冲突维修单 | `GET /api/capacity` | `app/modules/capacity/versions.py` `app/modules/capacity/service.py` | `tests/test_capacity.py` |
| M3 借还回放 | 按时点查看可借、可还、暂存状态 | `GET /api/replay` | `app/modules/replay/service.py` `app/modules/replay/schedule.py` | `tests/test_replay.py` |
| M4 溢出需求 | 按站、时槽和原因追踪未成交 | `GET /api/demand` | `app/modules/demand/service.py` `app/modules/demand/buckets.py` | `tests/test_demand.py` |
| M5 调拨试算 | 验证行程并比较逐站、逐笔差量 | `GET /api/dispatch` `POST /api/dispatch/preview` `POST /api/dispatch/publish` | `app/modules/dispatch/constraints.py` `app/modules/dispatch/compare.py` | `tests/test_dispatch.py` |
| M6 库存勾稽 | 核对各站期末及全网车辆守恒 | `GET /api/inventory` | `app/modules/inventory/service.py` `app/modules/inventory/stations.py` | `tests/test_inventory.py` |
| M7 班次方案留档 | 幂等重跑、回切和下载归档证据 | `GET /api/runs` `POST /api/runs/rerun` `POST /api/runs/restore` `GET /api/runs/<run_id>/evidence.json` | `app/modules/archive/service.py` `app/modules/archive/evidence.py` `app/store.py` | `tests/test_archive.py` |
| M8 跨班交接 | 签收去重、执行单、下一班库存与续跑方案 | `GET /api/handover` `POST /api/handover/preview` `POST /api/handover/publish` `GET /api/handover/<id>/evidence.json` | `app/modules/handover/domain.py` `app/modules/handover/service.py` `app/store.py` | `tests/test_handover.py` `tests/test_handover_api.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030 导出含中文 | UTF-8 解码失败 | UTF-8 严格解码，失败回退 GB18030，测试提供带中文的字节样例 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_gb18030_reordered_columns_and_mixed_line_endings` |
| TRAP-02 | `events.csv` 首列带 BOM | 首列无法匹配模板 | 解码时移除 BOM 并记录编码 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_bom_and_leading_zero` |
| TRAP-03 | `events.csv` 的 0007 含跨行备注 | 拆坏记录和后续源行 | CSV 引号规则和物理起始行 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_quoted_multiline_keeps_physical_source_rows` |
| TRAP-04 | `events.csv` 设备恢复后带分段表头 | 表头被当事件 | 用 `REPEATED_HEADER` 隔离 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_segment_header_and_extra_short_rows_are_isolated` |
| TRAP-05 | `events.csv` 的 0095 缺列 | 数据错列 | 用 `COLUMN_COUNT` 隔离 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_segment_header_and_extra_short_rows_are_isolated` |
| TRAP-07 | `events.csv` 的 0093 时间为 `-` | 错当缺省时间 | 缺失判定先于时间解析 | `app/modules/intake/normalize.py` | `tests/test_intake.py::test_sentinel_is_never_zero` |
| TRAP-08 | 站号 `001` 与事件号 `0001` | 转数字丢前导零 | 按字符串留存和归并 | `app/modules/intake/normalize.py` | `tests/test_intake.py::test_bom_and_leading_zero` |
| TRAP-11 | `events.csv` 的 0002 以两种偏移上报 | 被重复扣车 | UTC 归一后按业务键合并 | `app/modules/intake/service.py` | `tests/test_intake.py::test_offset_duplicate_keeps_earliest_received` |
| TRAP-13 | `events.csv` 的 0011 迟到 13 分 15 秒 | 0012 错判缺车 | 按发生时间回放并保留迟报标记 | `app/modules/replay/schedule.py` | `tests/test_replay.py::test_physical_order_beats_file_or_received_order` |
| TRAP-14 | `events.csv` 的 0088 指向两站 | 后写覆盖或重复扣车 | 同号异义的采信候选全部隔离 | `app/modules/intake/service.py` | `tests/test_intake.py::test_contradictory_duplicates_are_never_counted` |
| TRAP-18 | 空字节、空白行、仅表头上传 | 三种状态均报成功 0 行 | 返回各自的原因码 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_three_empty_inputs_have_distinct_errors` |
| TRAP-19 | `plan.json` 卸车与 0008 借车同秒 | 输入顺序影响成交 | 明确容量、卸车、装车、尝试四级顺序 | `app/modules/replay/schedule.py` | `tests/test_replay.py::test_same_clock_arrival_before_attempt` |
| TRAP-23 | `events.csv` 的 0009 正好处于 08:15 | 同一笔计进两个槽位 | 时槽按左闭右开分配 | `app/modules/demand/buckets.py` | `tests/test_demand.py::test_boundary_bucketing_does_not_double_count` |
| TRAP-25 | `capacity.csv` 002 的同修订容量相互矛盾 | 落点随文件顺序变化 | 隔离同修订冲突，从剩余候选选最高版本 | `app/modules/capacity/versions.py` | `tests/test_capacity.py::test_conflicting_revisions_are_quarantined_together` |
| TRAP-26 | 样例班次更新后旧页面再发布 | 覆盖当前计划 | 事务内校验当前编号并返回 409 | `app/store.py` | `tests/test_archive.py::test_stale_publish_rejected_and_archive_can_be_restored` |
| TRAP-27 | 同张纸质签收重试上传 | 已卸车辆被重复计数 | 按行程+时点+原文+辆数判重，只计首次但全部留档 | `app/modules/handover/domain.py` | `tests/test_handover.py::test_repeated_signoff_is_counted_once_but_archived` |
| TRAP-28 | 交接发布中途存储失败 | 出现半套交接单或库存被改 | 交接单、签收留档、生效切换同一事务，失败回滚保留旧版 | `app/store.py` | `tests/test_handover_api.py::test_failed_publish_keeps_old_sheet_old_plan_and_inventory` |
| TRAP-29 | 直接照抄计划期末库存 | 把未实际卸车的在途车算进站点 | 以无调拨基准叠加实际签收做迷你重放推导下一班期初 | `app/modules/handover/domain.py` | `tests/test_handover.py::test_next_shift_opening_inventory_conserves_fleet` |
| TRAP-30 | 旧页面在新班次发布后补交 | 覆盖新行程与新库存 | 发布前校验 `active` 班次，不符返回 `STALE_RUN` | `app/modules/handover/service.py` | `tests/test_handover_api.py::test_stale_handover_rejected_after_run_changes` |

## 对账规则

- 收件数 = 采信数 + 隔离数，空行和重现表头计入隔离。容量表按输入版本记录数勾稽，生效区间可由一条记录产生多段。
- 成交数 + 未成交数 = 有效尝试数，且事件编号逐一对齐。
- 站点期末 = 期初 + 成功还车 + 调入 − 成功借车 − 调出。每个动作后的全网车辆总量等于各站桩上、暂存、在骑、调拨在途之和。
- 交接单守恒：下一班各站桩上与暂存 + 在骑 + 随车在途 = 本班期初全网总量。已卸 = 去重后 `accepted` 签收辆数，随车剩余 = 行程装车数 − 已卸；`rejected` 车辆不卸站、继续在途。
