"""评估看板接口：看板配置 + 按消费范围裁剪的聚合快照。

原有 KPI/运行分析/导出接口保持不变（兼容分析看板 E2E）；新增的快照接口把
权限核对、V011 校验、原子发布等业务判断全部下沉到 BoardSnapshotService，
路由层只负责 HTTP 语义与错误码转换。
"""
from __future__ import annotations

import csv
import io
import random
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.board_snapshot import (
    BoardSnapshotError,
    ScopeError,
    SnapshotConflictError,
    SnapshotNotFoundError,
    board_snapshot_service,
    registry,
    snapshot_store,
)
from app.services.evaluate_board import EvaluateBoardService

router = APIRouter(prefix="/api/evaluate_board", tags=["评估看板"])
service = EvaluateBoardService()
LIST_FIELDS = ["看板编号", "看板名称", "关联实验", "展示指标", "数据范围", "刷新策略", "消费范围", "看板版本"]
STATUSES = ["草稿", "待校验", "已发布", "已停用"]


# ---------------------------------------------------------------- 请求模型
class ScopeBindPayload(BaseModel):
    scope_code: str
    operator: str = Field(min_length=1)


class RefreshPayload(BaseModel):
    operator: str = Field(min_length=1)
    strategy: str = Field(default="rebuild", pattern="^(rebuild|incremental)$")
    expected_scope_version: int | None = None


class ScopeAdjustPayload(BaseModel):
    operator: str = Field(min_length=1)
    members: list[str] | None = None
    experiments: list[str] | None = None


class OperatorPayload(BaseModel):
    operator: str = Field(min_length=1)


def _handle_domain_error(exc: Exception) -> HTTPException:
    """把领域异常映射成合适的 HTTP 状态码。"""
    if isinstance(exc, ScopeError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, SnapshotConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, SnapshotNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, BoardSnapshotError):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


# ---------------------------------------------------------------- 消费范围元数据
@router.get("/scopes")
def list_scopes() -> dict[str, Any]:
    """消费范围注册表：成员名单与获准实验，供前端切换范围时展示。"""
    return {"items": registry.list_scopes()}


@router.get("/v011/calibers")
def list_calibers() -> dict[str, Any]:
    """V011 已登记指标口径清单（跨模块联动的登记侧）。"""
    return {"items": registry.list_calibers()}


# ---------------------------------------------------------------- 看板配置 + 快照
@router.get("/boards")
def list_boards() -> dict[str, Any]:
    """列表侧栏数据：每条看板带当前发布快照，侧栏与详情页共用这一组数据。"""
    return {"items": board_snapshot_service.list_boards()}


@router.get("/boards/{board_id}")
def get_board(board_id: int) -> dict[str, Any]:
    """看板详情：配置与快照同源，保证列表侧栏和详情页是同一组数据。"""
    try:
        return board_snapshot_service.get_board(board_id)
    except SnapshotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/boards/{board_id}/scope")
def bind_scope(board_id: int, payload: ScopeBindPayload) -> dict[str, Any]:
    """绑定看板消费范围；越权成员（不在该范围名单）直接 403。"""
    try:
        return board_snapshot_service.configure_scope(board_id, payload.scope_code, payload.operator)
    except (ScopeError, SnapshotNotFoundError) as exc:
        raise _handle_domain_error(exc) from exc


@router.get("/boards/{board_id}/snapshot")
def read_snapshot(board_id: int, operator: str = Query(min_length=1)) -> dict[str, Any]:
    """消费端只读获取已发布快照；先核对归属，越权返回 403。"""
    try:
        return board_snapshot_service.read_snapshot(board_id, operator)
    except (ScopeError, SnapshotNotFoundError, BoardSnapshotError) as exc:
        raise _handle_domain_error(exc) from exc


@router.post("/boards/{board_id}/snapshot/refresh")
def refresh_snapshot(board_id: int, payload: RefreshPayload) -> dict[str, Any]:
    """按消费范围裁剪并聚合刷新快照（原子发布，失败回滚保留上一份）。"""
    try:
        return board_snapshot_service.refresh(
            board_id,
            payload.operator,
            strategy=payload.strategy,
            expected_scope_version=payload.expected_scope_version,
        )
    except (ScopeError, SnapshotConflictError, SnapshotNotFoundError, BoardSnapshotError) as exc:
        raise _handle_domain_error(exc) from exc


@router.post("/boards/{board_id}/scope/adjust")
def adjust_scope(board_id: int, payload: ScopeAdjustPayload) -> dict[str, Any]:
    """调整消费范围（成员/获准实验），范围版本号 +1，与后续刷新做乐观锁对账。"""
    try:
        return board_snapshot_service.adjust_scope(
            board_id,
            payload.operator,
            members=set(payload.members) if payload.members is not None else None,
            experiments=set(payload.experiments) if payload.experiments is not None else None,
        )
    except (ScopeError, SnapshotNotFoundError) as exc:
        raise _handle_domain_error(exc) from exc


@router.get("/boards/{board_id}/archive")
def get_archive(board_id: int, operator: str = Query(min_length=1)) -> dict[str, Any]:
    """V011 留档核对：历史发布与失败回滚记录。"""
    try:
        return {"items": board_snapshot_service.archive(board_id, operator)}
    except (ScopeError, SnapshotNotFoundError) as exc:
        raise _handle_domain_error(exc) from exc


@router.post("/boards/{board_id}/v011/verify")
def v011_verify(board_id: int, payload: OperatorPayload) -> dict[str, Any]:
    """按 V011 调用链核对快照卡片的指标口径，未登记口径返回拒绝明细。"""
    try:
        return board_snapshot_service.v011_verify(board_id, payload.operator)
    except (ScopeError, SnapshotNotFoundError, BoardSnapshotError) as exc:
        raise _handle_domain_error(exc) from exc


@router.post("/test-fault/{board_id}")
def arm_fault(board_id: int) -> dict[str, Any]:
    """测试辅助：让下一次刷新在发布前失败，验证回滚保留旧快照（不对外暴露业务语义）。"""
    snapshot_store.arm_failure(board_id)
    return {"ok": True, "message": f"看板 {board_id} 下一次刷新将注入发布前故障"}


# ---------------------------------------------------------------- 原有分析接口（兼容）
def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    return {"metrics": [
        {"label": "覆盖数据源", "value": random.randint(12, 45), "trend": random.randint(-12, 25)},
        {"label": "最新指标值", "value": round(random.uniform(0.72, 0.99), 3), "trend": random.randint(-8, 15)},
        {"label": "异常记录", "value": random.randint(0, 8), "trend": -random.randint(1, 10)},
    ]}


@router.get("")
def list_results() -> dict[str, Any]:
    return {"items": service.list_results()}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    results = service.run_analysis(payload.values)
    return {"items": results}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.list_results()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=evaluate_board.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"看板配置 {entry_id} 不存在")
    return entry
