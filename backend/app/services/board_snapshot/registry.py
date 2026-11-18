"""消费范围与 V011 口径登记表。

这里集中维护两类"受控元数据"：

1. 消费范围（scope）：每个看板只能被授权范围内的成员消费，范围之外的实验
   属于越权实验，刷新快照时必须裁剪掉，不能计入指标卡。
2. V011 指标口径登记：指标定义 V011 模块只接受已登记的指标口径，快照卡片
   携带的指标版本必须能在这里查到，否则卡片会被拒绝（跨模块联动 V011）。

内存实现只依赖标准库；真实项目会替换成数据库访问层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class Scope:
    """消费范围定义。"""

    code: str
    name: str
    # 获准读取该范围的成员（消费端身份标识）
    members: frozenset[str]
    # 该范围允许消费的实验编号
    experiments: frozenset[str]


@dataclass(frozen=True)
class MetricCaliber:
    """V011 指标口径登记条目。"""

    metric_code: str
    metric_version: str
    formula: str
    status: str  # 已登记 / 已停用
    registered_at: str


class ScopeError(PermissionError):
    """越权访问或越权操作。"""


class Registry:
    """消费范围 + V011 口径的内存登记表（进程内单例，带锁保证并发一致）。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._scopes: dict[str, Scope] = {}
        self._calibers: dict[tuple[str, str], MetricCaliber] = {}
        self._reset_seed()

    # ------------------------------------------------------------------ 初始化
    def _reset_seed(self) -> None:
        """预置两个消费范围，故意让实验 EXPE-0003 只属于范围二，制造越权样本。"""
        with self._lock:
            self._scopes = {
                "SCOPE_A": Scope(
                    code="SCOPE_A",
                    name="核心业务消费范围",
                    members=frozenset({"u001", "u002", "u003"}),
                    experiments=frozenset({"EXPE-0001", "EXPE-0002"}),
                ),
                "SCOPE_B": Scope(
                    code="SCOPE_B",
                    name="增长实验消费范围",
                    members=frozenset({"u004", "u005"}),
                    experiments=frozenset({"EXPE-0002", "EXPE-0003"}),
                ),
            }
            # V011 已登记口径；V099 故意不登记，用于验证"未登记口径被拒绝"
            self._calibers = {
                ("METR-0001", "V011"): MetricCaliber(
                    "METR-0001", "V011", "clicks / valid_impressions", "已登记", "2026-11-01"),
                ("METR-0002", "V011"): MetricCaliber(
                    "METR-0002", "V011", "pay_users / active_users", "已登记", "2026-11-02"),
                ("METR-0003", "V011"): MetricCaliber(
                    "METR-0003", "V011", "revenue / exposure_count", "已登记", "2026-11-03"),
                ("METR-0001", "V010"): MetricCaliber(
                    "METR-0001", "V010", "clicks / impressions", "已停用", "2026-09-01"),
            }

    # ------------------------------------------------------------------ 消费范围
    def scope_codes(self) -> list[str]:
        with self._lock:
            return sorted(self._scopes)

    def get_scope(self, scope_code: str) -> Scope | None:
        with self._lock:
            return self._scopes.get(scope_code)

    def require_scope(self, scope_code: str) -> Scope:
        scope = self.get_scope(scope_code)
        if scope is None:
            raise ScopeError(f"消费范围 {scope_code} 不存在或已下线")
        return scope

    def list_scopes(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "scope_code": s.code,
                    "scope_name": s.name,
                    "members": sorted(s.members),
                    "member_count": len(s.members),
                    "experiments": sorted(s.experiments),
                }
                for s in self._scopes.values()
            ]

    def assert_member(self, scope_code: str, operator: str) -> None:
        """核对归属：操作者必须是该消费范围的获准成员，否则拒绝越权操作。"""
        scope = self.require_scope(scope_code)
        if operator not in scope.members:
            raise ScopeError(
                f"成员 {operator} 不在消费范围 {scope_code}（{scope.name}）的获准名单内，操作拒绝")

    def scope_experiments(self, scope_code: str) -> frozenset[str]:
        return self.require_scope(scope_code).experiments

    def update_scope(
        self,
        scope_code: str,
        *,
        members: set[str] | frozenset[str] | None = None,
        experiments: set[str] | frozenset[str] | None = None,
    ) -> Scope:
        """调整消费范围。调整与快照刷新并发时，由快照版本号做乐观锁拦截。"""
        with self._lock:
            current = self.require_scope(scope_code)
            new_scope = Scope(
                code=current.code,
                name=current.name,
                members=frozenset(members) if members is not None else current.members,
                experiments=frozenset(experiments) if experiments is not None else current.experiments,
            )
            self._scopes[scope_code] = new_scope
            return new_scope

    # ------------------------------------------------------------------ V011 口径
    def is_caliber_registered(self, metric_code: str, metric_version: str) -> bool:
        with self._lock:
            caliber = self._calibers.get((metric_code, metric_version))
            return caliber is not None and caliber.status == "已登记"

    def get_caliber(self, metric_code: str, metric_version: str) -> MetricCaliber | None:
        with self._lock:
            return self._calibers.get((metric_code, metric_version))

    def register_caliber(
        self, metric_code: str, metric_version: str, formula: str, registered_at: str
    ) -> MetricCaliber:
        with self._lock:
            caliber = MetricCaliber(metric_code, metric_version, formula, "已登记", registered_at)
            self._calibers[(metric_code, metric_version)] = caliber
            return caliber

    def list_calibers(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "metric_code": c.metric_code,
                    "metric_version": c.metric_version,
                    "formula": c.formula,
                    "status": c.status,
                    "registered_at": c.registered_at,
                }
                for c in self._calibers.values()
            ]


# 进程内单例：路由层、服务层共享同一份受控登记表
registry = Registry()
