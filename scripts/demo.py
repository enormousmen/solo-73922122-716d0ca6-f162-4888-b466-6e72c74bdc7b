"""端到端演示：启动 uvicorn 后依次演示

场景 A（10.11.0.0/16，环路 + 回执改变裁决）：
  1) 录入 r1~r4 的新旧下一跳、入口流量、有向链路容量
  2) 申请 r3 放行 -> approved（进入“已放行未确认”）
  3) 申请 r4 放行 -> rejected（r3=old & r4=new 时 r3<->r4 环路，整批退回，原记录保留）
  4) 登记 r3 成功回执
  5) 再次申请 r4 -> approved（r3 已按新路由 r3->exit 转发）

场景 B（10.12.0.0/16，断路 + 超载故障定位）：
  申请 r1 -> rejected，返回断路位置 r1 -> r3（无此链路）与超载位置 r3 -> exit

场景 C（10.13.0.0/16，入口限速单：试算 -> 放行设备 -> 登记复核超容被拒 -> 修正后生效）：
  1) 录入方案；检修链路 r3->exit 容量 170 -> 70
  2) 开立限速单（保底 r1=30、r2=20，r4 不限速）-> 试算通过
  3) 试算期间其他网段（10.11.0.0/16）照常申请换路并拿到裁决
  4) 本网段申请 r4 -> approved（试算未登记，仍按原容量核算）
  5) 登记限速单 -> 409：复核时 r4=new 组合下 r3->exit 150 > 70 超容，原记录保留
  6) 重新开立限速单（把 r4 也列入保底）-> 试算通过 -> 登记生效
  7) 登记后申请 r1 -> approved，按新容量和限速核算（响应带生效单号）

只依赖标准库；完整 JSON 响应写入 demo_output.json。
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

OUTPUT_FILE = Path(__file__).resolve().parent.parent / "demo_output.json"


async def call(client: httpx.AsyncClient, method: str, path: str, body: dict | None = None) -> dict:
    resp = await client.request(method, path, json=body, timeout=10)
    data = resp.json()
    if resp.status_code >= 400:
        return {"http_status": resp.status_code, **data}
    return data


def plan_url(prefix: str, suffix: str) -> str:
    return f"/plans/{suffix}?prefix={urllib.parse.quote(prefix, safe='')}"


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def summarize_decision(resp: dict) -> None:
    print(f"  裁决: {resp['decision']} | 评估场景数: {resp['scenarios_evaluated']} "
          f"| 故障场景数: {resp['failing_scenarios_count']} | 故障统计: {resp['fault_summary']}")
    print(f"  说明: {resp['message']}")
    for scenario in resp["failing_scenarios"][:3]:
        states = ", ".join(f"{d}={s}" for d, s in sorted(scenario["device_states"].items()))
        print(f"  - 故障场景#{scenario['index']} [{states}]")
        for fault in scenario["faults"]:
            extra = ""
            if fault["type"] == "超载":
                extra = f"（流量 {fault['load']} > 容量 {fault['capacity']}）"
            print(f"      {fault['type']} @ {fault['location']}{extra}: {fault['detail']}")
    if resp["failing_scenarios_truncated"]:
        print("  - 其余故障场景已截断（详见接口返回）")


PLAN_A = {
    "prefix": "10.11.0.0/16",
    "exit_node": "exit",
    "routers": ["r1", "r2", "r3", "r4"],
    "links": [
        {"src": "r1", "dst": "r2", "capacity": 100},
        {"src": "r1", "dst": "r3", "capacity": 100},
        {"src": "r2", "dst": "r4", "capacity": 100},
        {"src": "r2", "dst": "r1", "capacity": 100},
        {"src": "r3", "dst": "r4", "capacity": 100},
        {"src": "r3", "dst": "exit", "capacity": 100},
        {"src": "r4", "dst": "exit", "capacity": 100},
        {"src": "r4", "dst": "r3", "capacity": 100},
    ],
    "ingresses": [{"node": "r1", "traffic": 10}],
    "routes": [
        {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r3"},
        {"device": "r2", "old_next_hop": "r4", "new_next_hop": "r1"},
        {"device": "r3", "old_next_hop": "r4", "new_next_hop": "exit"},
        {"device": "r4", "old_next_hop": "exit", "new_next_hop": "r3"},
    ],
}

PLAN_B = {
    "prefix": "10.12.0.0/16",
    "exit_node": "exit",
    "routers": ["r1", "r2", "r3"],
    "links": [
        {"src": "r1", "dst": "r2", "capacity": 100},
        {"src": "r2", "dst": "r3", "capacity": 150},
        {"src": "r3", "dst": "exit", "capacity": 100},
    ],
    "ingresses": [
        {"node": "r1", "traffic": 60},
        {"node": "r2", "traffic": 60},
    ],
    "routes": [
        # r1 新下一跳 r3，但未录入 r1->r3 有向链路 => 断路
        {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r3"},
        {"device": "r2", "old_next_hop": "r3", "new_next_hop": "r3"},
        {"device": "r3", "old_next_hop": "exit", "new_next_hop": "exit"},
    ],
}

# 场景 C：检修链路 r3->exit（170 -> 70）。r4 未列入限速单时保持原速率 80，
# 其新路由 r4->r3->exit 会把这 80 Mbps 引到检修链路。
PLAN_C = {
    "prefix": "10.13.0.0/16",
    "exit_node": "exit",
    "routers": ["r1", "r2", "r3", "r4"],
    "links": [
        {"src": "r1", "dst": "r2", "capacity": 100},
        {"src": "r1", "dst": "r3", "capacity": 100},
        {"src": "r2", "dst": "r3", "capacity": 100},
        {"src": "r3", "dst": "exit", "capacity": 170},
        {"src": "r4", "dst": "exit", "capacity": 150},
        {"src": "r4", "dst": "r3", "capacity": 100},
    ],
    "ingresses": [
        {"node": "r1", "traffic": 50},
        {"node": "r2", "traffic": 40},
        {"node": "r4", "traffic": 80},
    ],
    "routes": [
        {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r3"},
        {"device": "r2", "old_next_hop": "r3", "new_next_hop": "r3"},
        {"device": "r3", "old_next_hop": "exit", "new_next_hop": "exit"},
        {"device": "r4", "old_next_hop": "exit", "new_next_hop": "r3"},
    ],
}


def summarize_order(resp: dict) -> None:
    print(f"  试算: {resp['decision']} | 单号: {resp['order_id']} | 新容量: {resp['new_capacity']} Mbps")
    print(f"  限速值: {resp['rate_limits']} | 核算流量: {resp['verification_traffic']}")
    print(f"  说明: {resp['message']}")
    for fault in resp.get("faults", []):
        print(f"  - {fault['type']} @ {fault['location']}: {fault['detail']}")
    for scenario in resp.get("failing_scenarios", [])[:3]:
        states = ", ".join(f"{d}={s}" for d, s in sorted(scenario["device_states"].items()))
        print(f"  - 故障场景#{scenario['index']} [{states}]")
        for fault in scenario["faults"]:
            extra = ""
            if fault["type"] == "超载":
                extra = f"（流量 {fault['load']} > 容量 {fault['capacity']}）"
            print(f"      {fault['type']} @ {fault['location']}{extra}: {fault['detail']}")


def summarize_register_reject(resp: dict) -> None:
    detail = resp.get("detail", {})
    print(f"  登记被拒（HTTP {resp['http_status']}）: {detail.get('message')}")
    for scenario in detail.get("failing_scenarios", [])[:3]:
        states = ", ".join(f"{d}={s}" for d, s in sorted(scenario["device_states"].items()))
        print(f"  - 故障场景#{scenario['index']} [{states}]")
        for fault in scenario["faults"]:
            extra = ""
            if fault["type"] == "超载":
                extra = f"（流量 {fault['load']} > 容量 {fault['capacity']}）"
            print(f"      {fault['type']} @ {fault['location']}{extra}: {fault['detail']}")


async def main() -> None:
    archive: dict[str, list[dict]] = {"plan_a": [], "plan_b": [], "plan_c": []}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://route-approval") as client:
        banner("场景 A：录入换路方案（10.11.0.0/16）")
        resp = await call(client, "PUT", "/plans", PLAN_A)
        print(f"  已录入，当前 pending={resp['pending_devices']} confirmed={resp['confirmed_devices']}")
        archive["plan_a"].append({"step": "put_plan", "response": resp})

        banner("场景 A：第一批申请 [r3] —— 预期 approved")
        resp = await call(client, "POST", plan_url(PLAN_A["prefix"], "applications"), {"devices": ["r3"]})
        summarize_decision(resp)
        archive["plan_a"].append({"step": "apply_r3", "response": resp})
        assert resp["decision"] == "approved"

        banner("场景 A：第二批申请 [r4]（r3 已放行未确认）—— 预期 rejected：r3<->r4 环路")
        resp = await call(client, "POST", plan_url(PLAN_A["prefix"], "applications"), {"devices": ["r4"]})
        summarize_decision(resp)
        archive["plan_a"].append({"step": "apply_r4_before_receipt", "response": resp})
        assert resp["decision"] == "rejected"

        state = await call(client, "GET", plan_url(PLAN_A["prefix"], "detail"))
        print(f"  退回后状态（原放行记录保留）: pending={state['pending_devices']} "
              f"confirmed={state['confirmed_devices']} 历史申请数={state['application_count']}")
        archive["plan_a"].append({"step": "state_after_reject", "response": state})
        assert state["pending_devices"] == ["r3"]

        banner("场景 A：登记 r3 成功回执 —— 此后 r3 按新路由 r3->exit 核算")
        resp = await call(client, "POST", plan_url(PLAN_A["prefix"], "receipts"),
                          {"device": "r3", "note": "现场切换成功"})
        print(f"  回执: {resp['receipt_id']} | pending={resp['pending_devices']} "
              f"confirmed={resp['confirmed_devices']}")
        archive["plan_a"].append({"step": "receipt_r3", "response": resp})

        banner("场景 A：回执到达后重新申请 [r4] —— 预期 approved")
        resp = await call(client, "POST", plan_url(PLAN_A["prefix"], "applications"), {"devices": ["r4"]})
        summarize_decision(resp)
        archive["plan_a"].append({"step": "apply_r4_after_receipt", "response": resp})
        assert resp["decision"] == "approved"

        banner("场景 B：录入方案并申请 [r1] —— 预期 rejected：断路 + 超载，带故障位置")
        await call(client, "PUT", "/plans", PLAN_B)
        resp = await call(client, "POST", plan_url(PLAN_B["prefix"], "applications"), {"devices": ["r1"]})
        summarize_decision(resp)
        archive["plan_b"].append({"step": "apply_r1", "response": resp})
        assert resp["decision"] == "rejected"
        locations = {f["location"] for s in resp["failing_scenarios"] for f in s["faults"]}
        assert "r1 -> r3" in locations and "r3 -> exit" in locations

        banner("场景 C：录入方案（10.13.0.0/16），检修链路 r3->exit 170 -> 70")
        resp = await call(client, "PUT", "/plans", PLAN_C)
        print(f"  已录入，当前 pending={resp['pending_devices']} confirmed={resp['confirmed_devices']}")
        archive["plan_c"].append({"step": "put_plan", "response": resp})

        banner("场景 C：开立入口限速单（保底 r1=30、r2=20，r4 不限速）—— 预期试算通过")
        resp = await call(client, "POST", plan_url(PLAN_C["prefix"], "rate-limits"), {
            "link": {"src": "r3", "dst": "exit"},
            "new_capacity": 70,
            "guarantees": [
                {"node": "r1", "min_rate": 30},
                {"node": "r2", "min_rate": 20},
            ],
            "note": "机房检修，临时调低 r3->exit",
        })
        summarize_order(resp)
        archive["plan_c"].append({"step": "create_rate_limit", "response": resp})
        assert resp["decision"] == "trial_passed"
        order1 = resp["order_id"]

        banner("场景 C：试算期间其他网段（10.11.0.0/16）照常申请换路 —— 预期 approved")
        resp = await call(client, "POST", plan_url(PLAN_A["prefix"], "applications"), {"devices": ["r1"]})
        summarize_decision(resp)
        archive["plan_c"].append({"step": "other_prefix_application_during_trial", "response": resp})
        assert resp["decision"] == "approved"

        banner("场景 C：试算未登记，本网段申请 [r4] 仍按原容量核算 —— 预期 approved")
        resp = await call(client, "POST", plan_url(PLAN_C["prefix"], "applications"), {"devices": ["r4"]})
        summarize_decision(resp)
        print(f"  生效限速单: {resp['rate_limit_order_id']}")
        archive["plan_c"].append({"step": "apply_r4_before_register", "response": resp})
        assert resp["decision"] == "approved" and resp["rate_limit_order_id"] is None

        banner("场景 C：凭单号登记限速单 —— 预期 409：复核时 r4=new 组合 r3->exit 150 > 70 超容")
        resp = await call(client, "POST", plan_url(PLAN_C["prefix"], "rate-limits/register"),
                          {"order_id": order1})
        summarize_register_reject(resp)
        archive["plan_c"].append({"step": "register_rejected_recheck_failed", "response": resp})
        assert resp["http_status"] == 409 and resp["detail"]["reason"] == "recheck_failed"

        banner("场景 C：原记录与核算参数保留（含被拒绝的登记尝试）")
        resp = await call(client, "GET", plan_url(PLAN_C["prefix"], "rate-limits"))
        kept = resp[0]
        print(f"  单号 {kept['order_id']} 状态 {kept['status']} | 限速值 {kept['rate_limits']} "
              f"| 登记尝试: {kept['registration_attempts'][-1]['result']}({kept['registration_attempts'][-1]['reason']})")
        archive["plan_c"].append({"step": "orders_after_reject", "response": resp})

        banner("场景 C：重新开立限速单（r4 也列入保底 10）—— 预期试算通过")
        resp = await call(client, "POST", plan_url(PLAN_C["prefix"], "rate-limits"), {
            "link": {"src": "r3", "dst": "exit"},
            "new_capacity": 70,
            "guarantees": [
                {"node": "r1", "min_rate": 30},
                {"node": "r2", "min_rate": 20},
                {"node": "r4", "min_rate": 10},
            ],
        })
        summarize_order(resp)
        archive["plan_c"].append({"step": "create_rate_limit_fixed", "response": resp})
        assert resp["decision"] == "trial_passed"
        order2 = resp["order_id"]

        banner("场景 C：登记修正后的限速单 —— 预期登记生效")
        resp = await call(client, "POST", plan_url(PLAN_C["prefix"], "rate-limits/register"),
                          {"order_id": order2})
        print(f"  {resp['message']}")
        archive["plan_c"].append({"step": "register_success", "response": resp})
        assert resp["status"] == "registered"

        banner("场景 C：登记后申请 [r1] —— 按新容量和限速核算，预期 approved 且带生效单号")
        resp = await call(client, "POST", plan_url(PLAN_C["prefix"], "applications"), {"devices": ["r1"]})
        summarize_decision(resp)
        print(f"  生效限速单: {resp['rate_limit_order_id']}")
        archive["plan_c"].append({"step": "apply_r1_after_register", "response": resp})
        assert resp["decision"] == "approved" and resp["rate_limit_order_id"] == order2

    OUTPUT_FILE.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整 JSON 响应已写入: {OUTPUT_FILE}")
    print("演示完成：回执到达前 r4 被退回，回执到达后 r4 放行；")
    print("限速单试算后又放行设备，登记复核超容被拒，修正保底后登记生效。")


if __name__ == "__main__":
    asyncio.run(main())
