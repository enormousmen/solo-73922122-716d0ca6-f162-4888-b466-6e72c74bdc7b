"""进程内存储与并发控制。

每个目的网段维护独立的放行状态；所有写操作在同一把 asyncio 锁内完成，
保证并发申请按“裁决时刻已登记的批次和回执”串行裁决：后一个申请看到的
一定是前一个申请裁决并落库后的状态。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import ApplicationOut, PlanIn


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RateLimitOrderRecord:
    """一张入口限速单的完整留痕（含核算参数与登记尝试历史）。"""

    order_id: str
    link: tuple[str, str]
    new_capacity: int
    # [(入口节点, 保底速率)]，顺序即优先级
    guarantees: list[tuple[str, int]]
    # 核算参数：各被限速入口的限速值 / 核算采用的各入口流量
    rate_limits: dict[str, float]
    verification_traffic: dict[str, float]
    # 试算所基于的方案版本
    plan_generation: int
    # trial_passed / trial_failed / registered / superseded
    status: str
    decision: str
    # 试算级故障（如保底合计超新容量）
    top_faults: list[dict]
    scenarios_evaluated: int
    failing_scenarios_count: int
    fault_summary: dict[str, int]
    failing_scenarios: list[dict]
    failing_scenarios_truncated: bool
    message: str
    note: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    registered_at: str | None = None
    registration_attempts: list[dict] = field(default_factory=list)


@dataclass
class PrefixState:
    plan: PlanIn
    # 已放行且已登记成功回执：按新路由转发
    confirmed: set[str] = field(default_factory=set)
    # 已放行但尚未确认：新旧路由都可能生效
    pending: set[str] = field(default_factory=set)
    # 回执编号留痕
    receipts: dict[str, str] = field(default_factory=dict)
    # 全部申请历史（含退回记录，退回不改变放行状态）
    applications: list[ApplicationOut] = field(default_factory=list)
    # 方案版本：每次 PUT 覆盖 +1；限速单登记时据此识别“方案被覆盖”
    generation: int = 1
    # 限速单历史（方案覆盖后仍保留，供审计与“方案被覆盖”判定）
    rate_limit_orders: list[RateLimitOrderRecord] = field(default_factory=list)
    # 当前生效的限速单单号；登记后换路放行按新容量和限速核算
    active_rate_limit: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def find_order(self, order_id: str) -> RateLimitOrderRecord | None:
        for order in self.rate_limit_orders:
            if order.order_id == order_id:
                return order
        return None

    def active_order(self) -> RateLimitOrderRecord | None:
        if self.active_rate_limit is None:
            return None
        return self.find_order(self.active_rate_limit)

    def rate_limit_overrides(
        self,
    ) -> tuple[dict[tuple[str, str], float], dict[str, float]] | tuple[None, None]:
        """当前生效限速单对核算的覆盖：链路新容量 + 限速后入口流量。"""
        order = self.active_order()
        if order is None:
            return None, None
        return {order.link: float(order.new_capacity)}, dict(order.verification_traffic)


class Store:
    def __init__(self) -> None:
        self._prefixes: dict[str, PrefixState] = {}
        # 单把全局锁：临界区内只做内存计算，持锁时间短（最坏 4096 场景）
        self.lock = asyncio.Lock()

    async def get(self, prefix: str) -> PrefixState | None:
        return self._prefixes.get(prefix)

    async def require(self, prefix: str) -> PrefixState:
        state = self._prefixes.get(prefix)
        if state is None:
            raise KeyError(prefix)
        return state

    async def put_plan(self, plan: PlanIn) -> PrefixState:
        """全量覆盖录入；覆盖后原放行记录重置（重新核算的基线），方案版本 +1。

        限速单历史保留（登记时按版本号识别“方案被覆盖”并拒绝），
        但生效中的限速单随方案覆盖一并失效。
        """
        existing = self._prefixes.get(plan.prefix)
        if existing is None:
            state = PrefixState(plan=plan)
            self._prefixes[plan.prefix] = state
        else:
            existing.plan = plan
            existing.confirmed = set()
            existing.pending = set()
            existing.receipts = {}
            existing.applications = []
            existing.generation += 1
            existing.active_rate_limit = None
            existing.updated_at = utc_now_iso()
            state = existing
        return state

    async def list_prefixes(self) -> list[PrefixState]:
        return list(self._prefixes.values())


store = Store()
