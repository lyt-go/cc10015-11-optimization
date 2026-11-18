"""AB 对比结果表：看板聚合快照的数据源。

每条 AB 结果都带：
- 归属实验编号（experiment_code），用于按消费范围裁剪；
- 指标编号与指标版本（metric_code / metric_version），用于 V011 口径核对；
- 确定性的成员数与聚合数值，保证"获准范围的成员数与快照聚合值保持一致"，
  也让测试可重复、不依赖 random。

数据是演示用的受控内存表，真实项目里会换成 AB 结果分析模块的结果表读取。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class AbRow:
    """AB 对比结果表的一行（一个实验 × 一个指标口径的聚合结果）。"""

    row_id: int
    report_code: str
    experiment_code: str
    experiment_name: str
    metric_code: str
    metric_name: str
    metric_version: str
    control_members: int
    treatment_members: int
    control_mean: float
    treatment_mean: float

    @property
    def member_count(self) -> int:
        """参与该实验该指标统计的总成员数（对照 + 实验）。"""
        return self.control_members + self.treatment_members

    @property
    def diff(self) -> float:
        return round(self.treatment_mean - self.control_mean, 4)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["member_count"] = self.member_count
        data["diff"] = self.diff
        return data


class AbResultTable:
    """带消费范围标注能力的 AB 对比结果表。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._rows: list[AbRow] = []
        self._reset_seed()

    def _reset_seed(self) -> None:
        with self._lock:
            # 三个实验；EXPE-0003 不在 SCOPE_A 内，是越权实验样本。
            # METR-0003/V099 故意使用未登记口径，验证 V011 联动拒绝。
            self._rows = [
                AbRow(1, "AB_R-0001", "EXPE-0001", "新首页布局",
                      "METR-0001", "点击率", "V011",
                      1200, 1250, 0.3120, 0.3480),
                AbRow(2, "AB_R-0002", "EXPE-0001", "新首页布局",
                      "METR-0002", "付费转化率", "V011",
                      1200, 1250, 0.0810, 0.0905),
                AbRow(3, "AB_R-0003", "EXPE-0002", "结算链路优化",
                      "METR-0001", "点击率", "V011",
                      860, 840, 0.4010, 0.4277),
                AbRow(4, "AB_R-0004", "EXPE-0002", "结算链路优化",
                      "METR-0003", "人均收入", "V011",
                      860, 840, 52.40, 55.18),
                AbRow(5, "AB_R-0005", "EXPE-0003", "推送时段实验",
                      "METR-0002", "付费转化率", "V011",
                      600, 610, 0.0720, 0.0766),
                AbRow(6, "AB_R-0006", "EXPE-0003", "推送时段实验",
                      "METR-0003", "人均收入", "V099",  # 未登记口径
                      600, 610, 41.20, 43.05),
            ]

    def all_rows(self) -> list[AbRow]:
        with self._lock:
            return list(self._rows)

    def rows_for_scope(self, allowed_experiments: frozenset[str]) -> tuple[list[AbRow], list[AbRow]]:
        """按消费范围裁剪 AB 结果。

        返回 (获准行, 被裁剪的越权行)，越权行只用于留档核对，绝不进入快照。
        """
        with self._lock:
            allowed = [r for r in self._rows if r.experiment_code in allowed_experiments]
            excluded = [r for r in self._rows if r.experiment_code not in allowed_experiments]
            return allowed, excluded

    def experiments(self) -> list[str]:
        with self._lock:
            return sorted({r.experiment_code for r in self._rows})


# 进程内单例
ab_result_table = AbResultTable()
