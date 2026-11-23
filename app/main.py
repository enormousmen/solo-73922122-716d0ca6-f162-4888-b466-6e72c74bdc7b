"""FastAPI 路由：换路方案录入、放行裁决、回执登记、入口限速单与状态查询。"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException

from .engine import MAX_REPORTED_SCENARIOS, compute_rate_limits, evaluate
from .models import (
    ApplicationIn,
    ApplicationOut,
    Fault,
    PlanDetail,
    PlanIn,
    PlanSummary,
    RateLimitGuaranteeIn,
    RateLimitLinkIn,
    RateLimitOrderIn,
    RateLimitOrderOut,
    RateLimitRegisterIn,
    RateLimitRegisterOut,
    ReceiptIn,
    ReceiptOut,
    RegistrationAttempt,
    ScenarioOut,
)
from .storage import PrefixState, RateLimitOrderRecord, store, utc_now_iso

app = FastAPI(
    title="机房换路放行服务",
    version="1.1.0",
    description=(
        "值班员录入目的网段的出口、路由器新旧下一跳、入口流量与有向链路容量，"
        "按批申请下发新配置。服务端穷举本批设备与已放行未确认设备的所有新旧"
        "组合，保证任意切换次序下流量均可到达出口且链路不超载；有风险则整批退回。"
        "机房检修调低链路容量时，可开立入口限速单试算，装好限速后凭单号登记，"
        "登记时按最新放行/回执状态复核，登记后换路放行按新容量和限速核算。"
    ),
)


def _summary(state) -> PlanSummary:
    return PlanSummary(
        prefix=state.plan.prefix,
        exit_node=state.plan.exit_node,
        routers=list(state.plan.routers),
        confirmed_devices=sorted(state.confirmed),
        pending_devices=sorted(state.pending),
        application_count=len(state.applications),
        updated_at=state.updated_at,
        active_rate_limit_order=state.active_rate_limit,
    )


def _order_out(state: PrefixState, order: RateLimitOrderRecord) -> RateLimitOrderOut:
    return RateLimitOrderOut(
        order_id=order.order_id,
        prefix=state.plan.prefix,
        link=RateLimitLinkIn(src=order.link[0], dst=order.link[1]),
        new_capacity=order.new_capacity,
        guarantees=[RateLimitGuaranteeIn(node=n, min_rate=r) for n, r in order.guarantees],
        rate_limits=order.rate_limits,
        verification_traffic=order.verification_traffic,
        plan_generation=order.plan_generation,
        status=order.status,
        decision=order.decision,
        scenarios_evaluated=order.scenarios_evaluated,
        failing_scenarios_count=order.failing_scenarios_count,
        fault_summary=order.fault_summary,
        faults=[Fault(**f) for f in order.top_faults],
        failing_scenarios=[ScenarioOut(**s) for s in order.failing_scenarios],
        failing_scenarios_truncated=order.failing_scenarios_truncated,
        message=order.message,
        note=order.note,
        created_at=order.created_at,
        registered_at=order.registered_at,
        registration_attempts=[RegistrationAttempt(**a) for a in order.registration_attempts],
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.put("/plans", response_model=PlanSummary)
async def put_plan(plan: PlanIn) -> PlanSummary:
    """录入或全量覆盖一个目的网段的换路方案。"""
    async with store.lock:
        state = await store.put_plan(plan)
        return _summary(state)


@app.get("/plans", response_model=list[PlanSummary])
async def list_plans() -> list[PlanSummary]:
    async with store.lock:
        return [_summary(state) for state in await store.list_prefixes()]


@app.get("/plans/detail", response_model=PlanDetail)
async def get_plan(prefix: str) -> PlanDetail:
    async with store.lock:
        try:
            state = await store.require(prefix)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"目的网段 {prefix} 尚未录入方案")
        return PlanDetail(
            **_summary(state).model_dump(),
            plan=state.plan,
            applications=list(state.applications),
            created_at=state.created_at,
            rate_limit_orders=[_order_out(state, o) for o in state.rate_limit_orders],
        )


@app.post("/plans/applications", response_model=ApplicationOut)
async def apply(prefix: str, body: ApplicationIn) -> ApplicationOut:
    """申请一批设备下发新配置；整批通过或整批退回。"""
    async with store.lock:
        try:
            state = await store.require(prefix)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"目的网段 {prefix} 尚未录入方案")

        routers = set(state.plan.routers)
        unknown = [d for d in body.devices if d not in routers]
        if unknown:
            raise HTTPException(status_code=400, detail=f"设备不属于该网段: {unknown}")

        batch = set(body.devices)
        conflict_pending = sorted(batch & state.pending)
        conflict_confirmed = sorted(batch & state.confirmed)
        if conflict_pending or conflict_confirmed:
            raise HTTPException(
                status_code=409,
                detail=(
                    "存在已放行设备，不能重复申请；"
                    f"待确认={conflict_pending}，已确认={conflict_confirmed}。"
                    "待确认设备请先登记成功回执"
                ),
            )

        # 不确定设备 = 本批 ∪ 已放行未确认；已确认设备强制新路由
        uncertain = batch | state.pending
        # 已登记限速单生效期间，按新容量和限速后流量核算
        capacity_override, traffic_override = state.rate_limit_overrides()
        result = evaluate(
            state.plan,
            confirmed=set(state.confirmed),
            uncertain=uncertain,
            capacity_override=capacity_override,
            traffic_override=traffic_override,
        )

        application_id = f"app-{uuid.uuid4().hex[:12]}"
        decided_at = utc_now_iso()
        failing_report = result["failing_scenarios"][:MAX_REPORTED_SCENARIOS]
        truncated = result["failing_scenarios_count"] > len(failing_report)
        limit_note = (
            f"；本次按已登记限速单 {state.active_rate_limit} 的新容量与限速核算"
            if state.active_rate_limit
            else ""
        )

        if result["approved"]:
            decision = "approved"
            message = (
                f"全部 {result['scenarios_evaluated']} 种新旧组合核算通过，"
                f"本批 {len(batch)} 台设备放行；登记成功回执前按不确定设备处理"
            ) + limit_note
            # 放行：本批进入“已放行未确认”，原放行记录保持不变
            state.pending |= batch
        else:
            decision = "rejected"
            message = (
                f"在 {result['failing_scenarios_count']}/{result['scenarios_evaluated']} "
                "种新旧组合下存在断路/环路/超载，整批退回，原有放行记录不变"
            ) + limit_note

        out = ApplicationOut(
            application_id=application_id,
            prefix=prefix,
            decision=decision,
            batch=sorted(batch),
            uncertain_devices=sorted(uncertain),
            scenarios_evaluated=result["scenarios_evaluated"],
            failing_scenarios_count=result["failing_scenarios_count"],
            fault_summary=result["fault_summary"],
            failing_scenarios=[ScenarioOut(**s) for s in failing_report],
            failing_scenarios_truncated=truncated,
            message=message,
            decided_at=decided_at,
            rate_limit_order_id=state.active_rate_limit,
        )
        # 退回记录同样留痕，便于审计“保留原放行记录”的裁决过程
        state.applications.append(out)
        state.updated_at = decided_at
        return out


@app.post("/plans/receipts", response_model=ReceiptOut)
async def register_receipt(prefix: str, body: ReceiptIn) -> ReceiptOut:
    """登记已放行设备的成功回执；之后该设备按新路由参与核算。"""
    async with store.lock:
        try:
            state = await store.require(prefix)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"目的网段 {prefix} 尚未录入方案")

        if body.device not in state.plan.routers:
            raise HTTPException(status_code=400, detail=f"设备 {body.device} 不属于该网段")

        recorded_at = utc_now_iso()
        if body.device in state.confirmed:
            # 幂等：重复回执不改变状态
            return ReceiptOut(
                prefix=prefix,
                device=body.device,
                receipt_id=state.receipts[body.device],
                status="already_registered",
                confirmed_devices=sorted(state.confirmed),
                pending_devices=sorted(state.pending),
                recorded_at=recorded_at,
            )

        if body.device not in state.pending:
            raise HTTPException(
                status_code=409,
                detail=f"设备 {body.device} 尚未放行，不能登记回执",
            )

        receipt_id = body.receipt_id or f"rcpt-{uuid.uuid4().hex[:12]}"
        state.pending.discard(body.device)
        state.confirmed.add(body.device)
        state.receipts[body.device] = receipt_id
        state.updated_at = recorded_at
        return ReceiptOut(
            prefix=prefix,
            device=body.device,
            receipt_id=receipt_id,
            status="registered",
            confirmed_devices=sorted(state.confirmed),
            pending_devices=sorted(state.pending),
            recorded_at=recorded_at,
        )


# ---------- 入口限速单 ----------


def _require_state_or_404(prefix: str, state: PrefixState | None) -> PrefixState:
    if state is None:
        raise HTTPException(status_code=404, detail=f"目的网段 {prefix} 尚未录入方案")
    return state


@app.post("/plans/rate-limits", response_model=RateLimitOrderOut)
async def create_rate_limit_order(prefix: str, body: RateLimitOrderIn) -> RateLimitOrderOut:
    """开立入口限速单并试算。

    试算以待确认设备（pending）的所有新旧路由组合，按“链路新容量 + 限速后入口流量”
    核算能否到达出口且各链路不超容。试算只留痕，不改变任何裁决基线：
    登记前本网段与其他网段的换路申请仍按方案原始容量核算。
    """
    async with store.lock:
        state = _require_state_or_404(prefix, await store.get(prefix))
        plan = state.plan

        link = (body.link.src, body.link.dst)
        capacity_map = {(l.src, l.dst): l.capacity for l in plan.links}
        if link not in capacity_map:
            raise HTTPException(
                status_code=400,
                detail=f"链路 {body.link.src} -> {body.link.dst} 未在该网段方案中录入",
            )

        ingress_traffic = {ing.node: ing.traffic for ing in plan.ingresses}
        unknown_nodes = [g.node for g in body.guarantees if g.node not in ingress_traffic]
        if unknown_nodes:
            raise HTTPException(
                status_code=400,
                detail=f"保底入口不属于该网段的入口集合: {unknown_nodes}",
            )

        # 调低校验：新容量不得高于当前生效容量（生效中的同链路限速单优先）
        current_capacity = capacity_map[link]
        active = state.active_order()
        if active is not None and active.link == link:
            current_capacity = active.new_capacity
        if body.new_capacity > current_capacity:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"新容量 {body.new_capacity} 高于链路 {body.link.src} -> {body.link.dst} "
                    f"当前生效容量 {current_capacity:g}，限速单仅用于调低容量"
                ),
            )

        guarantees = [(g.node, g.min_rate) for g in body.guarantees]
        total_guarantee = sum(rate for _, rate in guarantees)
        created_at = utc_now_iso()

        top_faults: list[dict] = []
        rate_limits: dict[str, float] = {}
        verification_traffic = dict(ingress_traffic)
        scenarios_evaluated = 0
        failing_scenarios_count = 0
        fault_summary = {"断路": 0, "环路": 0, "超载": 0}
        failing_scenarios: list[dict] = []
        truncated = False

        if total_guarantee > body.new_capacity:
            # 保底已超容：无需穷举场景，直接判试算不通过
            location = f"{body.link.src} -> {body.link.dst}"
            top_faults.append(
                {
                    "type": "超载",
                    "location": location,
                    "detail": (
                        f"各入口保底速率合计 {total_guarantee} 已超过链路 {location} "
                        f"的新容量 {body.new_capacity}，保底已超容"
                    ),
                    "load": float(total_guarantee),
                    "capacity": float(body.new_capacity),
                }
            )
            fault_summary["超载"] = 1
            decision = "trial_failed"
            message = (
                f"保底速率合计 {total_guarantee} 超过链路新容量 {body.new_capacity}，"
                "试算不通过；请调低保底或提高新容量后重新开立限速单"
            )
        else:
            # 先留足所有保底，再按优先级让各入口尽量保住原速率
            rate_limits = compute_rate_limits(body.new_capacity, guarantees, ingress_traffic)
            traffic_override = {
                node: min(rate, ingress_traffic[node]) for node, rate in rate_limits.items()
            }
            verification_traffic = {**ingress_traffic, **traffic_override}
            # 待确认设备的每种新旧路由组合都须能转发到出口且各链路不超容
            result = evaluate(
                plan,
                confirmed=set(state.confirmed),
                uncertain=set(state.pending),
                capacity_override={link: float(body.new_capacity)},
                traffic_override=traffic_override,
            )
            scenarios_evaluated = result["scenarios_evaluated"]
            failing_scenarios_count = result["failing_scenarios_count"]
            fault_summary = result["fault_summary"]
            failing_report = result["failing_scenarios"][:MAX_REPORTED_SCENARIOS]
            truncated = failing_scenarios_count > len(failing_report)
            failing_scenarios = failing_report
            if result["approved"]:
                decision = "trial_passed"
                message = (
                    f"试算通过：{scenarios_evaluated} 种新旧组合在新容量 {body.new_capacity} "
                    "与限速后流量下均可到达出口且不超容；装好限速后请凭单号登记，"
                    "登记前换路放行仍按原容量核算"
                )
            else:
                decision = "trial_failed"
                message = (
                    f"试算不通过：在 {failing_scenarios_count}/{scenarios_evaluated} "
                    "种新旧组合下存在断路/环路/超载；限速单已留痕，不能登记"
                )

        order = RateLimitOrderRecord(
            order_id=f"rl-{uuid.uuid4().hex[:12]}",
            link=link,
            new_capacity=body.new_capacity,
            guarantees=guarantees,
            rate_limits=rate_limits,
            verification_traffic=verification_traffic,
            plan_generation=state.generation,
            status=decision,
            decision=decision,
            top_faults=top_faults,
            scenarios_evaluated=scenarios_evaluated,
            failing_scenarios_count=failing_scenarios_count,
            fault_summary=fault_summary,
            failing_scenarios=failing_scenarios,
            failing_scenarios_truncated=truncated,
            message=message,
            note=body.note,
            created_at=created_at,
        )
        state.rate_limit_orders.append(order)
        state.updated_at = created_at
        return _order_out(state, order)


@app.get("/plans/rate-limits", response_model=list[RateLimitOrderOut])
async def list_rate_limit_orders(prefix: str) -> list[RateLimitOrderOut]:
    """列出网段的全部限速单（含试算未通过与被覆盖的历史单）。"""
    async with store.lock:
        state = _require_state_or_404(prefix, await store.get(prefix))
        return [_order_out(state, o) for o in state.rate_limit_orders]


@app.post("/plans/rate-limits/register", response_model=RateLimitRegisterOut)
async def register_rate_limit_order(prefix: str, body: RateLimitRegisterIn) -> RateLimitRegisterOut:
    """凭单号登记限速单：值班员装好限速后、实际降容前调用。

    登记时按最新的放行（pending）和回执（confirmed）状态复核限速；
    方案被覆盖、复核未通过或已有登记时拒绝请求，原记录与核算参数保留。
    登记成功后，本网段换路放行按新容量和限速核算。
    """
    async with store.lock:
        state = _require_state_or_404(prefix, await store.get(prefix))
        order = state.find_order(body.order_id)
        if order is None:
            raise HTTPException(
                status_code=404, detail=f"限速单 {body.order_id} 不存在于网段 {prefix}"
            )

        attempted_at = utc_now_iso()

        def reject(reason: str, message: str, extra: dict | None = None) -> None:
            # 拒绝请求并保留原记录和核算参数：登记尝试同样留痕
            order.registration_attempts.append(
                {
                    "attempted_at": attempted_at,
                    "result": "rejected",
                    "reason": reason,
                    "detail": message,
                }
            )
            state.updated_at = attempted_at
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": reason,
                    "message": message,
                    "order_id": order.order_id,
                    **(extra or {}),
                },
            )

        if order.status == "registered":
            reject("already_registered", "该限速单已登记，不能重复登记")
        if order.status == "superseded":
            reject("superseded", "该限速单已被后续登记的限速单覆盖，不能再次登记")
        if order.plan_generation != state.generation:
            reject(
                "plan_overwritten",
                "限速单试算所依据的方案已被覆盖，核算参数已失效；请基于新方案重新开立限速单",
            )
        if order.status == "trial_failed":
            reject("trial_failed", "该限速单试算未通过，不能登记；请调整保底或新容量后重新开立")

        # 按最新的放行和回执状态复核限速
        capacity_override = {order.link: float(order.new_capacity)}
        traffic_override = dict(order.verification_traffic)
        result = evaluate(
            state.plan,
            confirmed=set(state.confirmed),
            uncertain=set(state.pending),
            capacity_override=capacity_override,
            traffic_override=traffic_override,
        )
        if not result["approved"]:
            failing_report = result["failing_scenarios"][:MAX_REPORTED_SCENARIOS]
            reject(
                "recheck_failed",
                (
                    f"按最新放行/回执状态复核未通过：{result['failing_scenarios_count']}/"
                    f"{result['scenarios_evaluated']} 种新旧组合存在断路/环路/超载；"
                    "限速单维持未登记状态，原记录与核算参数保留"
                ),
                extra={
                    "scenarios_evaluated": result["scenarios_evaluated"],
                    "failing_scenarios_count": result["failing_scenarios_count"],
                    "fault_summary": result["fault_summary"],
                    "failing_scenarios": failing_report,
                    "failing_scenarios_truncated": result["failing_scenarios_count"]
                    > len(failing_report),
                },
            )

        # 复核通过：登记生效；同网段新登记的单覆盖旧生效单
        previous = state.active_order()
        if previous is not None and previous.order_id != order.order_id:
            previous.status = "superseded"
        order.status = "registered"
        order.registered_at = attempted_at
        state.active_rate_limit = order.order_id
        order.registration_attempts.append(
            {
                "attempted_at": attempted_at,
                "result": "registered",
                "reason": None,
                "detail": (
                    f"复核通过（{result['scenarios_evaluated']} 种新旧组合均安全），限速单登记生效"
                ),
            }
        )
        state.updated_at = attempted_at
        return RateLimitRegisterOut(
            order_id=order.order_id,
            prefix=prefix,
            status="registered",
            link=RateLimitLinkIn(src=order.link[0], dst=order.link[1]),
            new_capacity=order.new_capacity,
            rate_limits=order.rate_limits,
            recheck_scenarios_evaluated=result["scenarios_evaluated"],
            recheck_fault_summary=result["fault_summary"],
            registered_at=attempted_at,
            message=(
                f"限速单 {order.order_id} 已登记生效：链路 {order.link[0]} -> {order.link[1]} "
                f"按新容量 {order.new_capacity} Mbps 与限速后流量核算后续换路放行"
            ),
        )
