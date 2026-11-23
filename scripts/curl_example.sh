#!/usr/bin/env bash
# 回执到达前后放行结果不同的 API 示例（curl 版）
# 前置：docker compose up -d  或  uvicorn app.main:app --port 8000
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}
PREFIX="10.20.0.0%2F16"   # 10.20.0.0/16，'/' 做百分号编码后走 query 参数

echo "== 1. 录入换路方案 =="
curl -s -X PUT "$BASE/plans" -H 'Content-Type: application/json' \
  -d "$(sed 's/10.11.0.0\/16/10.20.0.0\/16/' "$(dirname "$0")/plan_a.json")"
echo

echo "== 2. 申请 r3 放行（预期 approved，进入已放行未确认）=="
curl -s -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r3"]}'
echo

echo "== 3. 申请 r4 放行（预期 rejected：r3=old & r4=new 时 r3<->r4 环路，整批退回）=="
curl -s -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
echo

echo "== 4. 登记 r3 成功回执（此后 r3 按新路由 r3->exit 核算）=="
curl -s -X POST "$BASE/plans/receipts?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"device":"r3","note":"现场切换成功"}'
echo

echo "== 5. 重新申请 r4（预期 approved，与第 3 步结果不同）=="
curl -s -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
echo

echo "== 6. 查询网段全量状态（含全部申请历史）=="
curl -s "$BASE/plans/detail?prefix=$PREFIX"
echo

# ---------- 入口限速单：试算后又放行设备，限速单因超容被拒 ----------

PREFIX_C="10.21.0.0%2F16"   # 10.21.0.0/16，检修链路 r3->exit 170 -> 70

echo "== 7. 录入限速演示方案（10.21.0.0/16）=="
curl -s -X PUT "$BASE/plans" -H 'Content-Type: application/json' \
  -d "$(sed 's/10.13.0.0\/16/10.21.0.0\/16/' "$(dirname "$0")/plan_c.json")"
echo

echo "== 8. 开立限速单（保底 r1=30、r2=20，r4 不限速；预期试算通过）=="
ORDER1=$(curl -s -X POST "$BASE/plans/rate-limits?prefix=$PREFIX_C" \
  -H 'Content-Type: application/json' \
  -d '{"link":{"src":"r3","dst":"exit"},"new_capacity":70,
       "guarantees":[{"node":"r1","min_rate":30},{"node":"r2","min_rate":20}]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["order_id"])')
echo "order_id=$ORDER1"

echo "== 9. 试算未登记，申请 r4 仍按原容量核算（预期 approved）=="
curl -s -X POST "$BASE/plans/applications?prefix=$PREFIX_C" \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
echo

echo "== 10. 凭单号登记（预期 409：复核时 r4=new 组合 r3->exit 150 > 70 超容）=="
curl -s -X POST "$BASE/plans/rate-limits/register?prefix=$PREFIX_C" \
  -H 'Content-Type: application/json' -d "{\"order_id\":\"$ORDER1\"}"
echo

echo "== 11. 原记录与核算参数保留 =="
curl -s "$BASE/plans/rate-limits?prefix=$PREFIX_C"
echo

echo "== 12. 重新开立限速单（r4 也列入保底 10）并登记生效 =="
ORDER2=$(curl -s -X POST "$BASE/plans/rate-limits?prefix=$PREFIX_C" \
  -H 'Content-Type: application/json' \
  -d '{"link":{"src":"r3","dst":"exit"},"new_capacity":70,
       "guarantees":[{"node":"r1","min_rate":30},{"node":"r2","min_rate":20},{"node":"r4","min_rate":10}]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["order_id"])')
curl -s -X POST "$BASE/plans/rate-limits/register?prefix=$PREFIX_C" \
  -H 'Content-Type: application/json' -d "{\"order_id\":\"$ORDER2\"}"
echo

echo "== 13. 登记后申请 r1（按新容量和限速核算，响应带生效单号）=="
curl -s -X POST "$BASE/plans/applications?prefix=$PREFIX_C" \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
echo
