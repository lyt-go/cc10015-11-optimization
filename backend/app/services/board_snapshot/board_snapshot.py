"""评估看板聚合快照核心服务。

把需求里的几条硬约束固化在这一层（路由层不做业务判断）：

1. 按消费范围裁剪：读取看板配置 + AB 对比结果表，只聚合获准实验，越权实验
   进 excluded 留档，不进任何指标卡；
2. 归属核对：刷新/调整等写操作先核对操作者是否在消费范围成员内，越权拒绝；
   消费端读取受控快照只读；
3. 消费范围版本与聚合版本同步发布：一次原子提交里同时写 scope_version 与
   snapshot_version，杜绝同一看板混入另一个范围的成员；
4. 刷新失败保留上一份获准范围结果：staging 失败即回滚，绝不发布空壳；
5. V011 跨模块联动：卡片指标版本必须在口径登记表里，未登记口径的卡片被拒绝；
6. 版本冲突两种做法：rebuild 全量重建（只显示新范围完整聚合）/ incremental
   保留旧范围聚合并附待移除实验清单。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.board_snapshot.ab_source import AbRow, ab_result_table
from app.services.board_snapshot.registry import Registry, ScopeError, registry
from app.services.board_snapshot.snapshot_store import (
    BoardSnapshot,
    SnapshotConflictError,
    SnapshotNotFoundError,
    SnapshotStore,
    snapshot_store,
)

BOARD_MODULE = "evaluate_board"


class BoardSnapshotError(RuntimeError):
    """快照刷新业务错误（如 V011 校验未通过）。"""


class BoardSnapshotService:
    def __init__(
        self,
        reg: Registry | None = None,
        store: SnapshotStore | None = None,
    ) -> None:
        self.registry = reg or registry
        self.store = store or snapshot_store

    # ================================================================ 看板配置
    def list_boards(self) -> list[dict[str, Any]]:
        """看板列表：侧栏与详情页共用同一份配置 + 同一份快照数据。"""
        from app.store import store as app_store

        boards: list[dict[str, Any]] = []
        for row in app_store.rows(BOARD_MODULE):
            item = dict(row)
            snapshot = self.store.find_current(int(row["id"]))
            item["snapshot"] = snapshot.to_dict() if snapshot else None
            boards.append(item)
        return boards

    def get_board(self, board_id: int) -> dict[str, Any]:
        from app.store import store as app_store

        board = app_store.find(BOARD_MODULE, board_id)
        if board is None:
            raise SnapshotNotFoundError(f"看板配置 {board_id} 不存在")
        item = dict(board)
        snapshot = self.store.find_current(board_id)
        item["snapshot"] = snapshot.to_dict() if snapshot else None
        return item

    def configure_scope(self, board_id: int, scope_code: str, operator: str) -> dict[str, Any]:
        """给看板绑定消费范围（写操作，先核对归属）。"""
        from app.store import store as app_store

        board = app_store.find(BOARD_MODULE, board_id)
        if board is None:
            raise SnapshotNotFoundError(f"看板配置 {board_id} 不存在")
        # 归属核对：操作者必须是目标范围的获准成员
        self.registry.assert_member(scope_code, operator)
        scope = self.registry.require_scope(scope_code)
        # 绑定即登记范围版本基线；范围版本与之后的聚合版本必须成对发布
        scope_version = self.store.register_scope(scope.code)
        board["消费范围"] = scope.code
        board["看板版本"] = str(scope_version)
        app_store.bump_version(BOARD_MODULE)
        return {
            "board_id": board_id,
            "scope_code": scope.code,
            "scope_name": scope.name,
            "scope_version": scope_version,
        }

    # ================================================================ 读取（只读）
    def read_snapshot(self, board_id: int, operator: str) -> dict[str, Any]:
        """消费端读取快照：核对归属后返回只读发布态，不做任何写操作。"""
        board = self._board_or_raise(board_id)
        scope_code = str(board.get("消费范围") or "")
        if not scope_code or scope_code not in self.registry.scope_codes():
            raise BoardSnapshotError(
                f"看板 {board_id} 尚未绑定有效消费范围，受控快照不可读取")
        self.registry.assert_member(scope_code, operator)
        snapshot = self.store.get_current(board_id)
        return snapshot.to_dict()

    # ================================================================ 刷新主流程
    def refresh(
        self,
        board_id: int,
        operator: str,
        *,
        strategy: str = "rebuild",
        expected_scope_version: int | None = None,
    ) -> dict[str, Any]:
        """刷新看板聚合快照。

        strategy:
        - rebuild：按新消费范围全量重建，快照只显示新范围的完整聚合；
        - incremental：先保留旧范围聚合，再附待移除实验清单。
        """
        if strategy not in {"rebuild", "incremental"}:
            raise BoardSnapshotError(f"不支持的冲突处理策略：{strategy}")

        board = self._board_or_raise(board_id)
        scope_code = str(board.get("消费范围") or "")
        scope = self.registry.require_scope(scope_code)
        # 1) 归属核对：越权成员直接拒绝，不允许触发刷新
        self.registry.assert_member(scope_code, operator)

        current_scope_version = self.store.scope_version(scope_code)

        # 2) 版本冲突检测：客户端带着刷新基线版本号提交，若范围在此期间被调整
        #    （登记表当前版本号已推进），判定为并发冲突，要求显式选择处理策略。
        #    未携带基线版本的服务端刷新按当前范围版本直接构建，属于合法路径。
        if (
            expected_scope_version is not None
            and expected_scope_version != current_scope_version
        ):
            raise SnapshotConflictError(
                f"看板 {board_id} 检测到版本冲突：刷新基于消费范围版本 "
                f"{expected_scope_version}，当前范围版本已变更为 {current_scope_version}，"
                "请选择全量重建或增量移出后重试")

        # 3) 先在内存里构建候选快照（staging），任何一步失败都不碰 current
        previous = self.store.find_current(board_id)
        try:
            candidate = self._build_candidate(
                board=board,
                scope_code=scope.code,
                scope_version=current_scope_version,
                strategy=strategy,
                operator=operator,
                previous=previous,
            )
        except BoardSnapshotError:
            raise
        except Exception as exc:  # 裁剪/聚合异常统一按刷新失败处理
            self.store.rollback(board_id, str(exc))
            raise BoardSnapshotError(f"快照聚合失败，已回滚并保留上一份结果：{exc}") from exc

        self.store.stage(candidate)

        # 4) 故障注入点：模拟发布前事务残留，验证回滚不发空壳
        if self.store.consume_failure(board_id):
            reason = "模拟快照发布前事务失败，staging 回滚，保留上一份获准范围结果"
            self.store.rollback(board_id, reason)
            raise BoardSnapshotError(reason)

        # 5) 原子发布：消费范围版本与聚合版本在同一次提交内生效
        published = self.store.commit(candidate)
        board["看板版本"] = str(published.snapshot_version)
        return published.to_dict()

    # ================================================================ 候选快照构建
    def _build_candidate(
        self,
        *,
        board: dict[str, Any],
        scope_code: str,
        scope_version: int,
        strategy: str,
        operator: str,
        previous: BoardSnapshot | None,
    ) -> BoardSnapshot:
        board_id = int(board["id"])
        allowed_experiments = self.registry.scope_experiments(scope_code)

        # ---- 按消费范围裁剪 AB 对比结果表
        allowed_rows, excluded_rows = ab_result_table.rows_for_scope(allowed_experiments)

        # ---- V011 跨模块联动：逐卡片核对指标口径是否已登记
        cards: list[dict[str, Any]] = []
        v011_checks: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for row in allowed_rows:
            registered = self.registry.is_caliber_registered(row.metric_code, row.metric_version)
            check = {
                "report_code": row.report_code,
                "metric_code": row.metric_code,
                "metric_version": row.metric_version,
                "registered": registered,
                "result": "通过" if registered else "拒绝：未登记口径",
            }
            v011_checks.append(check)
            if not registered:
                # 指标定义 V011 模块拒绝未登记口径的卡片：不进快照卡片集
                rejected.append({
                    "report_code": row.report_code,
                    "experiment_code": row.experiment_code,
                    "metric_code": row.metric_code,
                    "metric_version": row.metric_version,
                    "reason": "V011 未登记口径，卡片被拒绝",
                })
                continue
            cards.append(self._row_to_card(row))

        if not cards:
            # 没有任何通过 V011 校验的卡片时，不允许发布"无范围限制的空壳"
            raise BoardSnapshotError(
                f"看板 {board_id} 在消费范围 {scope_code} 内没有通过 V011 校验的指标卡，"
                "本次刷新中止并回滚")

        snapshot_version = (previous.snapshot_version + 1) if previous else 1
        pending_removals: list[dict[str, Any]] = []

        if strategy == "incremental" and previous is not None:
            # 增量策略：保留旧范围聚合卡片，再标出待移除的越权实验（按实验去重）
            kept_keys = {(c["experiment_code"], c["metric_code"]) for c in cards}
            removal_map: dict[str, dict[str, Any]] = {}
            for old_card in previous.cards:
                key = (old_card["experiment_code"], old_card["metric_code"])
                if key not in kept_keys:
                    item = removal_map.setdefault(old_card["experiment_code"], {
                        "experiment_code": old_card["experiment_code"],
                        "experiment_name": old_card["experiment_name"],
                        "metric_codes": [],
                        "reason": "消费范围收窄，该实验已不在获准名单，待移出",
                    })
                    item["metric_codes"].append(old_card["metric_code"])
            pending_removals = list(removal_map.values())
            # 旧卡片仍然展示（快照显示旧范围聚合），新范围卡片补在前面
            merged = {(c["experiment_code"], c["metric_code"]): c for c in cards}
            for old_card in previous.cards:
                merged.setdefault((old_card["experiment_code"], old_card["metric_code"]), old_card)
            cards = list(merged.values())

        candidate = BoardSnapshot(
            board_id=board_id,
            scope_code=scope_code,
            scope_version=scope_version,
            snapshot_version=snapshot_version,
            strategy=strategy,
            status="staging",
            scope_consistent=not pending_removals,
            cards=cards,
            excluded_experiments=self._summarize_excluded(excluded_rows),
            pending_removals=pending_removals,
            v011_checks=v011_checks,
            aggregate=self._aggregate(
                cards, allowed_experiments, scope_code,
                pending_experiments={p["experiment_code"] for p in pending_removals}),
            refreshed_by=operator,
            refreshed_at=datetime.now().isoformat(timespec="seconds"),
        )
        candidate.aggregate["rejected_cards"] = rejected
        return candidate

    # ================================================================ 聚合/辅助
    def _row_to_card(self, row: AbRow) -> dict[str, Any]:
        return {
            "report_code": row.report_code,
            "experiment_code": row.experiment_code,
            "experiment_name": row.experiment_name,
            "metric_code": row.metric_code,
            "metric_name": row.metric_name,
            "metric_version": row.metric_version,
            "control_members": row.control_members,
            "treatment_members": row.treatment_members,
            "member_count": row.member_count,
            "control_mean": row.control_mean,
            "treatment_mean": row.treatment_mean,
            "diff": row.diff,
        }

    def _summarize_excluded(self, rows: list[AbRow]) -> list[dict[str, Any]]:
        """越权实验只做留档汇总，绝不参与卡片聚合。"""
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = grouped.setdefault(row.experiment_code, {
                "experiment_code": row.experiment_code,
                "experiment_name": row.experiment_name,
                "report_codes": [],
                "member_count": 0,
                "reason": "实验不在看板消费范围内，越权数据已裁剪",
            })
            item["report_codes"].append(row.report_code)
            item["member_count"] += row.member_count
        return list(grouped.values())

    def _aggregate(
        self,
        cards: list[dict[str, Any]],
        allowed_experiments: frozenset[str],
        scope_code: str,
        *,
        pending_experiments: set[str] | None = None,
    ) -> dict[str, Any]:
        """聚合值按当前消费范围口径计算。

        - member_total / card_count：当前快照实际展示的卡片口径（增量暂留时含旧范围）；
        - scoped_member_total / scoped_card_count：已收敛到新范围的卡片口径；
        - pending_member_total：增量策略下暂留、待移出的旧范围成员，范围完全收敛时为 0。
        """
        pending_experiments = pending_experiments or set()
        scope = self.registry.require_scope(scope_code)
        member_sum = sum(int(c["member_count"]) for c in cards)
        scoped_cards = [c for c in cards if c["experiment_code"] not in pending_experiments]
        pending_cards = [c for c in cards if c["experiment_code"] in pending_experiments]
        experiments_in_cards = sorted({c["experiment_code"] for c in cards})
        return {
            "card_count": len(cards),
            "scoped_card_count": len(scoped_cards),
            "experiment_count": len(experiments_in_cards),
            "experiments": experiments_in_cards,
            "allowed_experiments": sorted(allowed_experiments),
            "member_total": member_sum,
            "scoped_member_total": sum(int(c["member_count"]) for c in scoped_cards),
            "pending_member_total": sum(int(c["member_count"]) for c in pending_cards),
            "scope_member_total": len(scope.members),
            "avg_diff": round(sum(float(c["diff"]) for c in cards) / len(cards), 4) if cards else 0.0,
        }

    def adjust_scope(
        self,
        board_id: int,
        operator: str,
        *,
        members: set[str] | None = None,
        experiments: set[str] | None = None,
    ) -> dict[str, Any]:
        """调整看板当前消费范围（与刷新并发时由乐观锁拦截冲突）。"""
        board = self._board_or_raise(board_id)
        scope_code = str(board.get("消费范围") or "")
        self.registry.assert_member(scope_code, operator)
        new_scope = self.registry.update_scope(
            scope_code, members=members, experiments=experiments)
        new_version = self.store.bump_scope_version(scope_code)
        return {
            "board_id": board_id,
            "scope_code": new_scope.code,
            "scope_version": new_version,
            "members": sorted(new_scope.members),
            "experiments": sorted(new_scope.experiments),
        }

    def archive(self, board_id: int, operator: str) -> list[dict[str, Any]]:
        """V011 留档核对：只有范围成员能查看本看板留档。"""
        board = self._board_or_raise(board_id)
        scope_code = str(board.get("消费范围") or "")
        self.registry.assert_member(scope_code, operator)
        return self.store.archive(board_id)

    def v011_verify(self, board_id: int, operator: str) -> dict[str, Any]:
        """按 V011 调用链核对当前发布快照里每张卡片的指标口径。"""
        snapshot = self.read_snapshot(board_id, operator)
        checks: list[dict[str, Any]] = []
        all_pass = True
        for card in snapshot["cards"]:
            ok = self.registry.is_caliber_registered(
                card["metric_code"], card["metric_version"])
            all_pass = all_pass and ok
            checks.append({
                "experiment_code": card["experiment_code"],
                "metric_code": card["metric_code"],
                "metric_version": card["metric_version"],
                "registered": ok,
                "result": "通过" if ok else "拒绝：未登记口径",
            })
        return {
            "board_id": board_id,
            "snapshot_version": snapshot["snapshot_version"],
            "scope_code": snapshot["scope_code"],
            "all_pass": all_pass,
            "checks": checks,
        }

    # ------------------------------------------------------------------ 内部
    def _board_or_raise(self, board_id: int) -> dict[str, Any]:
        from app.store import store as app_store

        board = app_store.find(BOARD_MODULE, board_id)
        if board is None:
            raise SnapshotNotFoundError(f"看板配置 {board_id} 不存在")
        return board


board_snapshot_service = BoardSnapshotService()
