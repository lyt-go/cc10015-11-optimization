"""评估看板聚合快照子包：消费范围裁剪、V011 口径联动与原子快照发布。"""
from app.services.board_snapshot.registry import ScopeError, registry
from app.services.board_snapshot.snapshot_store import (
    SnapshotConflictError,
    SnapshotNotFoundError,
    snapshot_store,
)
from app.services.board_snapshot.board_snapshot import (
    BoardSnapshotError,
    BoardSnapshotService,
    board_snapshot_service,
)

__all__ = [
    "registry",
    "snapshot_store",
    "board_snapshot_service",
    "BoardSnapshotService",
    "BoardSnapshotError",
    "ScopeError",
    "SnapshotConflictError",
    "SnapshotNotFoundError",
]
