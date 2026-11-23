"""入口限速单的 pytest 自测：试算、登记复核、生效后核算与各类拒绝。"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.engine import compute_rate_limits, evaluate
from app.main import app
from app.models import PlanIn
from app.storage import store

client = TestClient(app)


def rl(path_prefix: str, suffix: str = "") -> str:
    base = "/plans/rate-limits" + suffix
    return f"{base}?prefix={quote(path_prefix, safe='')}"


def apply_url(path_prefix: str) -> str:
    return f"/plans/applications?prefix={quote(path_prefix, safe='')}"


def summary_url(path_prefix: str) -> str:
    return f"/plans/detail?prefix={quote(path_prefix, safe='')}"


def make_plan_c(prefix: str = "10.13.0.0/16") -> PlanIn:
    """限速演示拓扑：检修链路 r3->exit（170 -> 调低）。

    r1(50) old: r1->r2->r3->exit；r2(40) 恒走 r2->r3->exit；
    r4(80) old: r4->exit，new: r4->r3->exit（会把未限速流量引到检修链路）。
    """
    return PlanIn(
        prefix=prefix,
        exit_node="exit",
        routers=["r1", "r2", "r3", "r4"],
        links=[
            {"src": "r1", "dst": "r2", "capacity": 100},
            {"src": "r1", "dst": "r3", "capacity": 100},
            {"src": "r2", "dst": "r3", "capacity": 100},
            {"src": "r3", "dst": "exit", "capacity": 170},
            {"src": "r4", "dst": "exit", "capacity": 150},
            {"src": "r4", "dst": "r3", "capacity": 100},
        ],
        ingresses=[
            {"node": "r1", "traffic": 50},
            {"node": "r2", "traffic": 40},
            {"node": "r4", "traffic": 80},
        ],
        routes=[
            {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r3"},
            {"device": "r2", "old_next_hop": "r3", "new_next_hop": "r3"},
            {"device": "r3", "old_next_hop": "exit", "new_next_hop": "exit"},
            {"device": "r4", "old_next_hop": "exit", "new_next_hop": "r3"},
        ],
    )


def make_simple_plan(prefix: str) -> PlanIn:
    return PlanIn(
        prefix=prefix,
        exit_node="exit",
        routers=["r1", "r2"],
        links=[
            {"src": "r1", "dst": "r2", "capacity": 100},
            {"src": "r2", "dst": "exit", "capacity": 100},
        ],
        ingresses=[{"node": "r1", "traffic": 10}],
        routes=[
            {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r2"},
            {"device": "r2", "old_next_hop": "exit", "new_next_hop": "exit"},
        ],
    )


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    store._prefixes.clear()
    yield
    store._prefixes.clear()


# ---------- 限速分配算法与引擎覆盖 ----------


def test_compute_rate_limits_reserves_guarantees_then_priority() -> None:
    # 先留足保底（30+20），剩余 20 按优先级补给 r1 => r1 保住原速率 50，r2 被压到 20
    rates = compute_rate_limits(70, [("r1", 30), ("r2", 20)], {"r1": 50.0, "r2": 40.0})
    assert rates == {"r1": 50.0, "r2": 20.0}

    # 优先级靠后的入口先被压缩
    rates = compute_rate_limits(60, [("a", 10), ("b", 10)], {"a": 50.0, "b": 50.0})
    assert rates == {"a": 50.0, "b": 10.0}

    # 保底高于原速率：限速值给足保底（核算流量取 min(限速值, 原速率)，由调用方处理）
    rates = compute_rate_limits(100, [("a", 80)], {"a": 50.0})
    assert rates == {"a": 80.0}


def test_engine_evaluate_with_capacity_and_traffic_override() -> None:
    plan = make_simple_plan("10.30.0.0/16")
    assert evaluate(plan, confirmed=set(), uncertain=set())["approved"] is True
    # 容量调低到 5 < 入口流量 10 => 超载
    result = evaluate(
        plan, confirmed=set(), uncertain=set(), capacity_override={("r1", "r2"): 5.0}
    )
    assert result["approved"] is False
    assert result["fault_summary"]["超载"] == 1
    # 入口限速到 5 => 调低后的容量不再超载
    result = evaluate(
        plan,
        confirmed=set(),
        uncertain=set(),
        capacity_override={("r1", "r2"): 5.0},
        traffic_override={"r1": 5.0},
    )
    assert result["approved"] is True


# ---------- 录入校验 ----------


def test_rate_limit_validation_errors() -> None:
    prefix = "10.13.0.0/16"
    client.put("/plans", json=make_plan_c(prefix).model_dump())

    # 网段不存在 => 404
    resp = client.post(rl("9.9.9.0/24"), json={"link": {"src": "r3", "dst": "exit"}, "new_capacity": 70})
    assert resp.status_code == 404

    # 链路未录入 => 400
    resp = client.post(rl(prefix), json={"link": {"src": "r2", "dst": "r1"}, "new_capacity": 10})
    assert resp.status_code == 400

    # 保底入口不是该网段入口（r3 是路由器但不是入口）=> 400
    resp = client.post(
        rl(prefix),
        json={
            "link": {"src": "r3", "dst": "exit"},
            "new_capacity": 70,
            "guarantees": [{"node": "r3", "min_rate": 10}],
        },
    )
    assert resp.status_code == 400

    # 新容量高于当前容量（不是调低）=> 400
    resp = client.post(rl(prefix), json={"link": {"src": "r3", "dst": "exit"}, "new_capacity": 200})
    assert resp.status_code == 400

    # 重复保底入口 / 超 100 万 / 非整数 / 负数 => 422
    resp = client.post(
        rl(prefix),
        json={
            "link": {"src": "r3", "dst": "exit"},
            "new_capacity": 70,
            "guarantees": [{"node": "r1", "min_rate": 10}, {"node": "r1", "min_rate": 20}],
        },
    )
    assert resp.status_code == 422
    resp = client.post(rl(prefix), json={"link": {"src": "r3", "dst": "exit"}, "new_capacity": 1_000_001})
    assert resp.status_code == 422
    resp = client.post(rl(prefix), json={"link": {"src": "r3", "dst": "exit"}, "new_capacity": 70.5})
    assert resp.status_code == 422
    resp = client.post(
        rl(prefix),
        json={
            "link": {"src": "r3", "dst": "exit"},
            "new_capacity": 70,
            "guarantees": [{"node": "r1", "min_rate": 1_000_001}],
        },
    )
    assert resp.status_code == 422
    resp = client.post(
        rl(prefix),
        json={
            "link": {"src": "r3", "dst": "exit"},
            "new_capacity": 70,
            "guarantees": [{"node": "r1", "min_rate": -1}],
        },
    )
    assert resp.status_code == 422


def test_guarantee_exceeds_new_capacity_trial_failed() -> None:
    prefix = "10.13.0.0/16"
    client.put("/plans", json=make_plan_c(prefix).model_dump())

    # 保底合计 60 > 新容量 50 => 保底已超容，试算不通过
    resp = client.post(
        rl(prefix),
        json={
            "link": {"src": "r3", "dst": "exit"},
            "new_capacity": 50,
            "guarantees": [{"node": "r1", "min_rate": 30}, {"node": "r2", "min_rate": 30}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "trial_failed"
    assert body["status"] == "trial_failed"
    assert body["scenarios_evaluated"] == 0
    assert body["faults"][0]["type"] == "超载"
    assert body["faults"][0]["location"] == "r3 -> exit"
    assert body["faults"][0]["load"] == 60
    assert body["faults"][0]["capacity"] == 50

    # 试算未通过的单不能登记 => 409 trial_failed，记录保留
    resp = client.post(rl(prefix, "/register"), json={"order_id": body["order_id"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "trial_failed"
    orders = client.get(rl(prefix)).json()
    assert len(orders) == 1
    assert orders[0]["status"] == "trial_failed"
    assert orders[0]["registration_attempts"][0]["reason"] == "trial_failed"


# ---------- 主流程：试算 -> 放行设备 -> 登记复核超容被拒 -> 修正后登记生效 ----------


def test_trial_then_approval_then_recheck_rejected_then_fixed() -> None:
    prefix = "10.13.0.0/16"
    other = "10.11.0.0/16"
    client.put("/plans", json=make_plan_c(prefix).model_dump())
    client.put("/plans", json=make_simple_plan(other).model_dump())

    # 1. 开立限速单：r3->exit 170 -> 70，保底 r1=30、r2=20（r4 未列出，不限速）
    resp = client.post(
        rl(prefix),
        json={
            "link": {"src": "r3", "dst": "exit"},
            "new_capacity": 70,
            "guarantees": [{"node": "r1", "min_rate": 30}, {"node": "r2", "min_rate": 20}],
            "note": "检修 r3->exit",
        },
    )
    assert resp.status_code == 200
    order1 = resp.json()
    assert order1["decision"] == "trial_passed"
    # 先留足保底 50，剩余 20 全给优先级最高的 r1 => r1 保住 50，r2 压到 20；r4 不限速
    assert order1["rate_limits"] == {"r1": 50.0, "r2": 20.0}
    assert order1["verification_traffic"] == {"r1": 50.0, "r2": 20.0, "r4": 80.0}
    assert order1["scenarios_evaluated"] == 1  # 无待确认设备

    # 2. 试算期间其他网段照常申请换路并拿到裁决
    resp = client.post(apply_url(other), json={"devices": ["r1"]})
    assert resp.json()["decision"] == "approved"

    # 3. 试算未登记，本网段申请仍按原容量核算：r4 放行（r4=new 时 r3->exit 170 刚好不超）
    resp = client.post(apply_url(prefix), json={"devices": ["r4"]})
    body = resp.json()
    assert body["decision"] == "approved"
    assert body["rate_limit_order_id"] is None

    # 4. 登记限速单：按最新放行状态复核，r4=new 时 r3->exit 50+20+80=150 > 70 => 409
    resp = client.post(rl(prefix, "/register"), json={"order_id": order1["order_id"]})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "recheck_failed"
    assert detail["fault_summary"]["超载"] >= 1
    failing = detail["failing_scenarios"][0]
    assert failing["device_states"] == {"r4": "new"}
    overload = [f for f in failing["faults"] if f["type"] == "超载"][0]
    assert overload["location"] == "r3 -> exit"
    assert overload["load"] == 150
    assert overload["capacity"] == 70

    # 5. 原记录与核算参数保留，登记尝试留痕
    orders = client.get(rl(prefix)).json()
    assert len(orders) == 1
    kept = orders[0]
    assert kept["status"] == "trial_passed"
    assert kept["rate_limits"] == {"r1": 50.0, "r2": 20.0}
    assert kept["registration_attempts"][0]["result"] == "rejected"
    assert kept["registration_attempts"][0]["reason"] == "recheck_failed"

    # 6. 修正限速单：把 r4 也列入保底（r1=30, r2=20, r4=10），重新试算通过
    resp = client.post(
        rl(prefix),
        json={
            "link": {"src": "r3", "dst": "exit"},
            "new_capacity": 70,
            "guarantees": [
                {"node": "r1", "min_rate": 30},
                {"node": "r2", "min_rate": 20},
                {"node": "r4", "min_rate": 10},
            ],
        },
    )
    order2 = resp.json()
    assert order2["decision"] == "trial_passed"
    # 保底合计 60，剩余 10 补给 r1 => r1=40, r2=20, r4=10
    assert order2["rate_limits"] == {"r1": 40.0, "r2": 20.0, "r4": 10.0}
    assert order2["scenarios_evaluated"] == 2  # r4 已在途

    # 7. 登记成功：复核基于最新状态（r4 在途），此后按新容量和限速核算
    resp = client.post(rl(prefix, "/register"), json={"order_id": order2["order_id"]})
    assert resp.status_code == 200
    reg = resp.json()
    assert reg["status"] == "registered"
    assert reg["recheck_scenarios_evaluated"] == 2
    summary = client.get(summary_url(prefix)).json()
    assert summary["active_rate_limit_order"] == order2["order_id"]

    # 8. 重复登记同一单 => 409 already_registered
    resp = client.post(rl(prefix, "/register"), json={"order_id": order2["order_id"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "already_registered"

    # 9. 登记后换路放行按新容量和限速核算：响应带生效单号
    resp = client.post(apply_url(prefix), json={"devices": ["r1"]})
    body = resp.json()
    assert body["decision"] == "approved"
    assert body["rate_limit_order_id"] == order2["order_id"]
    assert "限速单" in body["message"]

    # 10. 旧单 r4 未限速，复核仍超容 => 409 recheck_failed
    resp = client.post(rl(prefix, "/register"), json={"order_id": order1["order_id"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "recheck_failed"


def test_registered_limit_changes_future_decisions() -> None:
    """登记后，原本能放行的批次因新容量+限速被退回；未开单的网段不受影响。"""
    plan = PlanIn(
        prefix="10.31.0.0/16",
        exit_node="exit",
        routers=["r1", "r2", "r3"],
        links=[
            {"src": "r1", "dst": "exit", "capacity": 150},
            {"src": "r1", "dst": "r3", "capacity": 100},
            {"src": "r2", "dst": "r3", "capacity": 100},
            {"src": "r3", "dst": "exit", "capacity": 150},
        ],
        ingresses=[{"node": "r1", "traffic": 90}, {"node": "r2", "traffic": 40}],
        routes=[
            {"device": "r1", "old_next_hop": "exit", "new_next_hop": "r3"},
            {"device": "r2", "old_next_hop": "r3", "new_next_hop": "r3"},
            {"device": "r3", "old_next_hop": "exit", "new_next_hop": "exit"},
        ],
    )
    prefix = plan.prefix
    client.put("/plans", json=plan.model_dump())

    # 无限速单时 r1 可放行（r1=new 时 r3->exit 合计 130 <= 150）
    resp = client.post(apply_url(prefix), json={"devices": ["r1"]})
    assert resp.json()["decision"] == "approved"
    # 回执让 r1 落定旧路由之外的状态不影响后续：重新覆盖方案，换一条对照网段
    client.put("/plans", json=plan.model_dump())

    # 开立并登记限速单：r3->exit 150 -> 60，只保 r2=20（r1 未列出，保持 90）
    resp = client.post(
        rl(prefix),
        json={
            "link": {"src": "r3", "dst": "exit"},
            "new_capacity": 60,
            "guarantees": [{"node": "r2", "min_rate": 20}],
        },
    )
    order = resp.json()
    assert order["decision"] == "trial_passed"
    resp = client.post(rl(prefix, "/register"), json={"order_id": order["order_id"]})
    assert resp.status_code == 200

    # 登记后再申请 r1：r1=new 时 r3->exit 90+40=130 > 60 => 整批退回
    resp = client.post(apply_url(prefix), json={"devices": ["r1"]})
    body = resp.json()
    assert body["decision"] == "rejected"
    assert body["rate_limit_order_id"] == order["order_id"]
    overload = [f for s in body["failing_scenarios"] for f in s["faults"] if f["type"] == "超载"][0]
    assert overload["location"] == "r3 -> exit"
    assert overload["load"] == 130
    assert overload["capacity"] == 60

    # 对照网段（同方案、无限速单）：同一批仍可放行
    other = PlanIn(**{**plan.model_dump(), "prefix": "10.32.0.0/16"})
    client.put("/plans", json=other.model_dump())
    resp = client.post(apply_url(other.prefix), json={"devices": ["r1"]})
    assert resp.json()["decision"] == "approved"


# ---------- 方案覆盖 / 单号覆盖 / 幂等 ----------


def test_plan_overwrite_blocks_registration() -> None:
    prefix = "10.13.0.0/16"
    client.put("/plans", json=make_plan_c(prefix).model_dump())
    resp = client.post(
        rl(prefix),
        json={
            "link": {"src": "r3", "dst": "exit"},
            "new_capacity": 70,
            "guarantees": [{"node": "r1", "min_rate": 30}, {"node": "r2", "min_rate": 20}],
        },
    )
    order = resp.json()
    assert order["decision"] == "trial_passed"

    # 方案被覆盖（重新 PUT）=> 登记拒绝，原记录与核算参数保留
    client.put("/plans", json=make_plan_c(prefix).model_dump())
    resp = client.post(rl(prefix, "/register"), json={"order_id": order["order_id"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "plan_overwritten"
    orders = client.get(rl(prefix)).json()
    assert len(orders) == 1
    assert orders[0]["status"] == "trial_passed"
    assert orders[0]["plan_generation"] == 1
    assert orders[0]["rate_limits"] == order["rate_limits"]
    # 方案覆盖后生效单被清空
    assert client.get(summary_url(prefix)).json()["active_rate_limit_order"] is None


def test_new_registered_order_supersedes_previous() -> None:
    prefix = "10.33.0.0/16"
    client.put("/plans", json=make_simple_plan(prefix).model_dump())

    # 单 A：r1->r2 100 -> 70，登记生效
    resp = client.post(
        rl(prefix),
        json={"link": {"src": "r1", "dst": "r2"}, "new_capacity": 70,
              "guarantees": [{"node": "r1", "min_rate": 10}]},
    )
    order_a = resp.json()
    assert order_a["decision"] == "trial_passed"
    resp = client.post(rl(prefix, "/register"), json={"order_id": order_a["order_id"]})
    assert resp.status_code == 200

    # 新容量不得高于当前生效容量（70）：开 80 => 400
    resp = client.post(rl(prefix), json={"link": {"src": "r1", "dst": "r2"}, "new_capacity": 80})
    assert resp.status_code == 400

    # 单 B：继续调低到 60，登记后 A 被覆盖
    resp = client.post(
        rl(prefix),
        json={"link": {"src": "r1", "dst": "r2"}, "new_capacity": 60,
              "guarantees": [{"node": "r1", "min_rate": 10}]},
    )
    order_b = resp.json()
    assert order_b["decision"] == "trial_passed"
    resp = client.post(rl(prefix, "/register"), json={"order_id": order_b["order_id"]})
    assert resp.status_code == 200

    orders = {o["order_id"]: o for o in client.get(rl(prefix)).json()}
    assert orders[order_a["order_id"]]["status"] == "superseded"
    assert orders[order_b["order_id"]]["status"] == "registered"
    assert client.get(summary_url(prefix)).json()["active_rate_limit_order"] == order_b["order_id"]

    # 被覆盖的单不能再登记 => 409 superseded；已登记的单重复登记 => 409 already_registered
    resp = client.post(rl(prefix, "/register"), json={"order_id": order_a["order_id"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "superseded"
    resp = client.post(rl(prefix, "/register"), json={"order_id": order_b["order_id"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "already_registered"


def test_register_unknown_order_404() -> None:
    prefix = "10.13.0.0/16"
    client.put("/plans", json=make_plan_c(prefix).model_dump())
    resp = client.post(rl(prefix, "/register"), json={"order_id": "rl-nonexistent"})
    assert resp.status_code == 404
