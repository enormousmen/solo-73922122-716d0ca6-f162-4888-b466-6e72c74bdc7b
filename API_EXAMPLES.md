# API 示例：回执到达前后放行结果不同

以下响应均为服务真实输出（完整 JSON 见 `demo_output.json`，可由 `python scripts/demo.py` 重新生成）。

## 拓扑（目的网段 10.11.0.0/16，出口 exit）

录入载荷见 `scripts/plan_a.json`。路由器与新旧下一跳：

| 设备 | 旧下一跳 | 新下一跳 |
| --- | --- | --- |
| r1 | r2 | r3 |
| r2 | r4 | r1 |
| r3 | r4 | **exit** |
| r4 | exit | **r3** |

入口流量：r1 注入 10；所有相关有向链路（含 `r3->exit`、`r4->r3`、`r3->r4`）容量均为 100。

### 1. 录入方案

```bash
curl -X PUT http://localhost:8000/plans \
  -H 'Content-Type: application/json' -d @scripts/plan_a.json
```

```json
{
  "prefix": "10.11.0.0/16",
  "exit_node": "exit",
  "routers": ["r1", "r2", "r3", "r4"],
  "confirmed_devices": [],
  "pending_devices": [],
  "application_count": 0
}
```

### 2. 申请 r3 —— 通过（2 种新旧组合均安全）

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r3"]}'
```

```json
{
  "application_id": "app-b1124b2ab343",
  "decision": "approved",
  "batch": ["r3"],
  "uncertain_devices": ["r3"],
  "scenarios_evaluated": 2,
  "failing_scenarios_count": 0,
  "fault_summary": {"断路": 0, "环路": 0, "超载": 0},
  "message": "全部 2 种新旧组合核算通过，本批 1 台设备放行；登记成功回执前按不确定设备处理"
}
```

此后 r3 进入“已放行未确认”（pending）。

### 3. 申请 r4 —— 整批退回（r3 未确认，存在 r3↔r4 环路组合）

不确定设备 = 本批 `{r4}` ∪ pending `{r3}`，共穷举 4 种组合。
当 `r3=old（->r4）` 且 `r4=new（->r3）` 时，r1 的流量沿
`r1 -> r2 -> r4 -> r3 -> r4` 形成环路：

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
```

```json
{
  "application_id": "app-d843973d934a",
  "decision": "rejected",
  "batch": ["r4"],
  "uncertain_devices": ["r3", "r4"],
  "scenarios_evaluated": 4,
  "failing_scenarios_count": 1,
  "fault_summary": {"断路": 0, "环路": 1, "超载": 0},
  "failing_scenarios": [
    {
      "index": 1,
      "device_states": {"r3": "old", "r4": "new"},
      "reachable": false,
      "faults": [
        {
          "type": "环路",
          "location": "r1 -> r2 -> r4 -> r3 -> r4",
          "detail": "入口 r1 的流量沿 r1 -> r2 -> r4 -> r3 -> r4 形成转发环路，无法到达出口 exit"
        }
      ]
    }
  ],
  "message": "在 1/4 种新旧组合下存在断路/环路/超载，整批退回，原有放行记录不变"
}
```

退回后查询状态，r3 的原放行记录保留：`pending_devices=["r3"]`，申请历史中同时留有
第 2、3 步两条记录。

### 4. 核算期间登记 r3 的成功回执

```bash
curl -X POST 'http://localhost:8000/plans/receipts?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"device":"r3","note":"现场切换成功"}'
```

```json
{
  "prefix": "10.11.0.0/16",
  "device": "r3",
  "receipt_id": "rcpt-4e441de29ac7",
  "status": "registered",
  "confirmed_devices": ["r3"],
  "pending_devices": []
}
```

此后 r3 强制按新路由 `r3 -> exit` 转发，不再参与新旧组合穷举。
重复登记同一设备回执返回 `"status": "already_registered"`（幂等）。

### 5. 回执到达后重新申请 r4 —— 通过（与第 3 步裁决不同）

不确定集合只剩 `{r4}`：r4=old 走 `r4 -> exit`；r4=new 走 `r4 -> r3 -> exit`，两种均安全。

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
```

```json
{
  "application_id": "app-8722195038e9",
  "decision": "approved",
  "batch": ["r4"],
  "uncertain_devices": ["r4"],
  "scenarios_evaluated": 2,
  "failing_scenarios_count": 0,
  "fault_summary": {"断路": 0, "环路": 0, "超载": 0},
  "message": "全部 2 种新旧组合核算通过，本批 1 台设备放行；登记成功回执前按不确定设备处理"
}
```

**同一批设备（r4），回执前退回、回执后放行**，这就是回执改变裁决结果的完整过程。

## 附：断路 + 超载的故障定位（10.12.0.0/16）

录入载荷见 `scripts/plan_b.json`：r1 新下一跳 r3 但未录入 `r1 -> r3` 链路（断路）；
r1、r2 各注入 60，r3 -> exit 容量仅 100（超载）。

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.12.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
```

```json
{
  "decision": "rejected",
  "scenarios_evaluated": 2,
  "failing_scenarios_count": 2,
  "fault_summary": {"断路": 1, "环路": 0, "超载": 1},
  "failing_scenarios": [
    {
      "index": 0,
      "device_states": {"r1": "old"},
      "faults": [
        {"type": "超载", "location": "r3 -> exit",
         "detail": "链路 r3 -> exit 合计流量 120 超过容量 100",
         "load": 120.0, "capacity": 100.0}
      ]
    },
    {
      "index": 1,
      "device_states": {"r1": "new"},
      "faults": [
        {"type": "断路", "location": "r1 -> r3",
         "detail": "入口 r1 的流量在 r1 -> r3 处无法继续转发到出口 exit"}
      ]
    }
  ]
}
```

## 并发申请语义

所有申请/回执写操作在同一把 asyncio 锁内串行裁决。两个各自安全的批次并发提交时，
先裁决者放行并进入 pending，后裁决者必须把先裁决的批次计入不确定集合——若联合穷举
出现风险则退回（自测 `test_api_receipt_changes_decision_concurrency` 验证：并发结果
恰为一个 approved、一个 rejected，且只有先裁决的设备出现在 pending 中）。

## 入口限速单：试算后又放行设备，限速单因超容被拒

以下响应均为服务真实输出（`demo_output.json` 的 `plan_c` 部分，可由
`python scripts/demo.py` 重新生成；curl 版本见 `scripts/curl_example.sh` 第 7~13 步）。

### 拓扑（目的网段 10.13.0.0/16，出口 exit）

录入载荷见 `scripts/plan_c.json`。检修链路为 `r3 -> exit`（容量 170，拟调低到 70）：

| 设备 | 旧下一跳 | 新下一跳 |
| --- | --- | --- |
| r1 | r2 | r3 |
| r2 | r3 | r3 |
| r3 | exit | exit |
| r4 | exit | **r3** |

入口流量：r1 注入 50、r2 注入 40、r4 注入 80。r4 的新路由 `r4 -> r3 -> exit`
会把流量引到检修链路上。

### 1. 开立限速单并试算 —— 通过

值班员按优先级给出保底：r1 保 30、r2 保 20；r4 未列出，不被本单限速。
限速值分配：先留足保底 30+20=50，剩余 20 全补给优先级最高的 r1（30+20=50，保住原速率），
r2 只能按保底 20 限速。

```bash
curl -X POST 'http://localhost:8000/plans/rate-limits?prefix=10.13.0.0%2F16' \
  -H 'Content-Type: application/json' \
  -d '{"link":{"src":"r3","dst":"exit"},"new_capacity":70,
       "guarantees":[{"node":"r1","min_rate":30},{"node":"r2","min_rate":20}]}'
```

```json
{
  "order_id": "rl-7f454da34c56",
  "decision": "trial_passed",
  "status": "trial_passed",
  "new_capacity": 70,
  "rate_limits": {"r1": 50.0, "r2": 20.0},
  "verification_traffic": {"r1": 50.0, "r2": 20.0, "r4": 80.0},
  "scenarios_evaluated": 1,
  "message": "试算通过：1 种新旧组合在新容量 70 与限速后流量下均可到达出口且不超容；装好限速后请凭单号登记，登记前换路放行仍按原容量核算"
}
```

当前无待确认设备，唯一场景下 `r3 -> exit` 合计 50+20=70，刚好不超容，试算通过。
若保底合计超过新容量（如 r1=30、r2=30 对容量 50），试算直接判不通过并返回
`"type": "超载"`、`"location": "r3 -> exit"` 的“保底已超容”故障。

### 2. 试算期间，其他网段照常申请换路并拿到裁决

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
```

```json
{"decision": "approved", "scenarios_evaluated": 4, "rate_limit_order_id": null}
```

试算只留痕，不改变任何网段的裁决基线。

### 3. 试算未登记，本网段申请 r4 仍按原容量核算 —— 通过

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.13.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
```

```json
{"decision": "approved", "batch": ["r4"], "scenarios_evaluated": 2, "rate_limit_order_id": null}
```

原容量 170 下 `r4=new` 时 `r3 -> exit` 合计 50+40+80=170，刚好不超容，r4 放行进入 pending。

### 4. 凭单号登记 —— 409：复核发现超容，原记录保留

值班员装好限速后、实际降容前凭单号登记。登记时按**最新**放行状态复核：
r4 已在途，`r4=new` 组合下未限速的 80 Mbps 被引向检修链路，合计 50+20+80=150 > 70。

```bash
curl -X POST 'http://localhost:8000/plans/rate-limits/register?prefix=10.13.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"order_id":"rl-7f454da34c56"}'
```

```json
{
  "http_status": 409,
  "detail": {
    "reason": "recheck_failed",
    "message": "按最新放行/回执状态复核未通过：1/2 种新旧组合存在断路/环路/超载；限速单维持未登记状态，原记录与核算参数保留",
    "scenarios_evaluated": 2,
    "failing_scenarios": [
      {
        "device_states": {"r4": "new"},
        "faults": [
          {"type": "超载", "location": "r3 -> exit",
           "detail": "链路 r3 -> exit 合计流量 150 超过容量 70",
           "load": 150.0, "capacity": 70.0}
        ]
      }
    ]
  }
}
```

拒绝后原记录与核算参数保留，`GET /plans/rate-limits?prefix=10.13.0.0%2F16` 可见该单
仍为 `trial_passed`，`rate_limits` 不变，且 `registration_attempts` 留下本次被拒记录
（`"result": "rejected", "reason": "recheck_failed"`）。方案被重新 PUT 覆盖后再登记旧单，
会以 `plan_overwritten` 拒绝；重复登记同一单以 `already_registered` 拒绝。

### 5. 修正保底后重新试算、登记生效

把 r4 也列入保底（10 Mbps）：保底合计 60，剩余 10 补给 r1，
限速值变为 r1=40、r2=20、r4=10，两种新旧组合下 `r3 -> exit` 最高 70，试算通过。

```bash
curl -X POST 'http://localhost:8000/plans/rate-limits?prefix=10.13.0.0%2F16' \
  -H 'Content-Type: application/json' \
  -d '{"link":{"src":"r3","dst":"exit"},"new_capacity":70,
       "guarantees":[{"node":"r1","min_rate":30},{"node":"r2","min_rate":20},{"node":"r4","min_rate":10}]}'
# => {"order_id": "rl-a7776be35c6c", "decision": "trial_passed",
#     "rate_limits": {"r1": 40.0, "r2": 20.0, "r4": 10.0}, "scenarios_evaluated": 2, ...}

curl -X POST 'http://localhost:8000/plans/rate-limits/register?prefix=10.13.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"order_id":"rl-a7776be35c6c"}'
```

```json
{
  "order_id": "rl-a7776be35c6c",
  "status": "registered",
  "recheck_scenarios_evaluated": 2,
  "message": "限速单 rl-a7776be35c6c 已登记生效：链路 r3 -> exit 按新容量 70 Mbps 与限速后流量核算后续换路放行"
}
```

### 6. 登记后，换路放行按新容量和限速核算

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.13.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
```

```json
{
  "decision": "approved",
  "scenarios_evaluated": 4,
  "rate_limit_order_id": "rl-a7776be35c6c",
  "message": "全部 4 种新旧组合核算通过，本批 1 台设备放行；登记成功回执前按不确定设备处理；本次按已登记限速单 rl-a7776be35c6c 的新容量与限速核算"
}
```

申请响应带 `rate_limit_order_id` 即表示本次裁决按生效限速单的新容量与限速后流量核算。
