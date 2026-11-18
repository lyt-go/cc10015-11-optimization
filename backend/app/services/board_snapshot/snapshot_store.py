"""看板聚合快照存储：发布态 / 暂存态 / 留档三段式。

事务约定（对应需求"事务一旦残留便回滚""失败后保留上一份获准范围结果"）：

- 刷新过程中先把候选快照写入 staging（暂存态），绝不直接覆盖 current；
- 只有裁剪、V011 校验、聚合全部成功后，才在同一把锁内原子替换 current，
  并把旧 current 追加进 archive 留档（按 V011 留档核对）；
- 任一环节失败，staging 直接丢弃并回滚，current 保持上一份获准范围结果，
  不会先发布无范围限制的空壳；
- 消费端只能读取 current（受控内容只读），写操作统一走服务层。
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any


class SnapshotConflictError(RuntimeError):
    """消费范围调整与卡片刷新并发，版本号对不上时抛出。"""


class SnapshotNotFoundError(LookupError):
    """看板还没有任何成功发布的快照。"""


@dataclass
class BoardSnapshot:
    """一张看板在某个消费范围下的聚合快照。"""

    board_id: int
    scope_code: str
    scope_version: int          # 消费范围版本，与聚合版本同步发布
    snapshot_version: int       # 聚合版本（看板版本），每次成功刷新 +1
    strategy: str               # rebuild 全量重建 / incremental 保留旧范围增量移出
    status: str                 # published 已发布 / failed 刷新失败留痕
    scope_consistent: bool = True  # 卡片是否已完全收敛到当前消费范围（增量暂留时为 False）
    cards: list[dict[str, Any]] = field(default_factory=list)
    excluded_experiments: list[dict[str, Any]] = field(default_factory=list)
    pending_removals: list[dict[str, Any]] = field(default_factory=list)
    v011_checks: list[dict[str, Any]] = field(default_factory=list)
    aggregate: dict[str, Any] = field(default_factory=dict)
    refreshed_by: str = ""
    refreshed_at: str = ""
    fail_reason: str | None = None

    def clone(self) -> "BoardSnapshot":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy({
            "board_id": self.board_id,
            "scope_code": self.scope_code,
            "scope_version": self.scope_version,
            "snapshot_version": self.snapshot_version,
            "strategy": self.strategy,
            "status": self.status,
            "scope_consistent": self.scope_consistent,
            "cards": self.cards,
            "excluded_experiments": self.excluded_experiments,
            "pending_removals": self.pending_removals,
            "v011_checks": self.v011_checks,
            "aggregate": self.aggregate,
            "refreshed_by": self.refreshed_by,
            "refreshed_at": self.refreshed_at,
            "fail_reason": self.fail_reason,
        })


class SnapshotStore:
    """按看板维度保存 current / staging / archive。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: dict[int, BoardSnapshot] = {}
        self._staging: dict[int, BoardSnapshot] = {}
        self._archive: dict[int, list[BoardSnapshot]] = {}
        # 消费范围版本号：范围调整时 +1，刷新时用来检测并发冲突
        self._scope_versions: dict[str, int] = {}
        # 故障注入开关：置位后下一次刷新在发布前失败，用于验证回滚
        self._fail_next: set[int] = set()

    # ------------------------------------------------------------- 范围版本/乐观锁
    def register_scope(self, scope_code: str) -> int:
        with self._lock:
            return self._scope_versions.setdefault(scope_code, 1)

    def scope_version(self, scope_code: str) -> int:
        with self._lock:
            return self._scope_versions.get(scope_code, 1)

    def bump_scope_version(self, scope_code: str) -> int:
        with self._lock:
            self._scope_versions[scope_code] = self._scope_versions.get(scope_code, 1) + 1
            return self._scope_versions[scope_code]

    # ------------------------------------------------------------------ 故障注入
    def arm_failure(self, board_id: int) -> None:
        with self._lock:
            self._fail_next.add(board_id)

    def consume_failure(self, board_id: int) -> bool:
        with self._lock:
            if board_id in self._fail_next:
                self._fail_next.discard(board_id)
                return True
            return False

    # ------------------------------------------------------------------ 暂存/提交
    def stage(self, snapshot: BoardSnapshot) -> None:
        with self._lock:
            self._staging[snapshot.board_id] = snapshot.clone()

    def commit(self, snapshot: BoardSnapshot) -> BoardSnapshot:
        """原子发布：留档旧快照 -> 替换 current -> 清理 staging。

        全程持锁，外部观察到的 current 要么是旧的完整快照，要么是新的完整快照，
        不会出现"只显示新事务的一半"或无范围限制的空壳。
        """
        with self._lock:
            board_id = snapshot.board_id
            old = self._current.get(board_id)
            if old is not None:
                self._archive.setdefault(board_id, []).append(old.clone())
            published = snapshot.clone()
            published.status = "published"
            self._current[board_id] = published
            self._staging.pop(board_id, None)
            return published.clone()

    def rollback(self, board_id: int, reason: str) -> None:
        """丢弃暂存快照；current 原封不动，保证上一份获准范围结果继续可读。"""
        with self._lock:
            staged = self._staging.pop(board_id, None)
            if staged is not None:
                staged.status = "failed"
                staged.fail_reason = reason
                self._archive.setdefault(board_id, []).append(staged)

    # ------------------------------------------------------------------ 读取（只读）
    def get_current(self, board_id: int) -> BoardSnapshot:
        with self._lock:
            snapshot = self._current.get(board_id)
            if snapshot is None:
                raise SnapshotNotFoundError(f"看板 {board_id} 尚无已发布的聚合快照")
            return snapshot.clone()

    def find_current(self, board_id: int) -> BoardSnapshot | None:
        with self._lock:
            snapshot = self._current.get(board_id)
            return snapshot.clone() if snapshot is not None else None

    def archive(self, board_id: int) -> list[dict[str, Any]]:
        """V011 留档核对：返回该看板所有历史快照（含失败回滚记录）。"""
        with self._lock:
            return [s.to_dict() for s in self._archive.get(board_id, [])]

    def reset(self) -> None:
        with self._lock:
            self._current.clear()
            self._staging.clear()
            self._archive.clear()
            self._scope_versions.clear()
            self._fail_next.clear()


snapshot_store = SnapshotStore()
