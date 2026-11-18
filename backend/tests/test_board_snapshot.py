"""评估看板聚合快照：消费范围裁剪 / V011 联动 / 原子发布 / 版本冲突 全链路测试。

覆盖需求点：
1. 按消费范围裁剪，越权实验不进指标卡，成员数与快照聚合值一致；
2. 归属核对：越权成员刷新/读取/调整一律拒绝；受控快照只读；
3. 消费范围版本与聚合版本同步发布；
4. 刷新失败保留上一份获准范围结果，不发布无范围限制空壳；
5. 事务残留回滚 + V011 留档可核对；
6. V011 跨模块联动：未登记口径卡片被拒绝；
7. 版本冲突：rebuild 全量重建 vs incremental 保留旧范围 + 待移除清单；
8. 列表侧栏与详情页同源；
9. 消费端取得全量获准范围快照。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.board_snapshot import (  # noqa: E402
    BoardSnapshotError,
    BoardSnapshotService,
    ScopeError,
    SnapshotConflictError,
    SnapshotNotFoundError,
    registry,
    snapshot_store,
)
from app.services.board_snapshot.ab_source import ab_result_table  # noqa: E402
from app.store import store  # noqa: E402

BOARD_MODULE = "evaluate_board"


@pytest.fixture(autouse=True)
def isolated_state():
    """每个用例独立状态：清业务表后重建 3 张看板，并重置快照/登记表。"""
    for key in list(store._tables.keys()):
        store._tables[key] = []
    snapshot_store.reset()
    registry._reset_seed()
    ab_result_table._reset_seed()
    for i in range(3):
        store.rows(BOARD_MODULE).append({
            "id": i + 1,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "看板编号": f"EVAL-{i + 1:04d}",
            "看板名称": f"评估看板样例{i + 1}",
            "关联实验": f"评估看板样例{i + 1}",
            "展示指标": "点击率",
            "数据范围": "全量",
            "刷新策略": "手动",
            "消费范围": "",
            "看板版本": "",
        })
    yield


def _bind_and_refresh(board_id=1, scope_code="SCOPE_A", operator="u001", strategy="rebuild"):
    svc = BoardSnapshotService()
    svc.configure_scope(board_id, scope_code, operator)
    return svc.refresh(board_id, operator, strategy=strategy)


# ---------------------------------------------------------------- 1. 消费范围裁剪
def test_scope_trim_excludes_unauthorized_experiment():
    """SCOPE_A 只获准 EXPE-0001/0002，EXPE-0003 必须被裁剪且不计入卡片。"""
    snapshot = _bind_and_refresh()
    card_experiments = {c["experiment_code"] for c in snapshot["cards"]}
    assert "EXPE-0003" not in card_experiments
    assert card_experiments <= {"EXPE-0001", "EXPE-0002"}

    excluded = {e["experiment_code"] for e in snapshot["excluded_experiments"]}
    assert excluded == {"EXPE-0003"}
    # 越权实验的成员数只出现在留档里，不进聚合
    assert snapshot["aggregate"]["member_total"] == sum(
        c["member_count"] for c in snapshot["cards"])
    assert snapshot["aggregate"]["experiments"] == ["EXPE-0001", "EXPE-0002"]


def test_different_scopes_produce_different_snapshots():
    """同一数据源，不同消费范围裁剪出不同卡片集合，不能跨范围合并。"""
    svc = BoardSnapshotService()
    svc.configure_scope(1, "SCOPE_A", "u001")
    snap_a = svc.refresh(1, "u001")
    svc.configure_scope(2, "SCOPE_B", "u004")
    snap_b = svc.refresh(2, "u004")
    experiments_a = {c["experiment_code"] for c in snap_a["cards"]}
    experiments_b = {c["experiment_code"] for c in snap_b["cards"]}
    assert "EXPE-0003" not in experiments_a
    assert "EXPE-0003" in experiments_b
    assert "EXPE-0001" in experiments_a
    assert "EXPE-0001" not in experiments_b


def test_member_count_matches_aggregate():
    """获准范围的成员数与快照聚合值保持一致。"""
    snapshot = _bind_and_refresh()
    expected = sum(c["member_count"] for c in snapshot["cards"])
    assert snapshot["aggregate"]["member_total"] == expected
    assert snapshot["aggregate"]["card_count"] == len(snapshot["cards"])


# ---------------------------------------------------------------- 2. 归属核对/只读
def test_unauthorized_member_cannot_refresh():
    """越权成员触发刷新直接拒绝。"""
    svc = BoardSnapshotService()
    svc.configure_scope(1, "SCOPE_A", "u001")
    with pytest.raises(ScopeError):
        svc.refresh(1, "u004")  # u004 属于 SCOPE_B，不在 SCOPE_A 名单


def test_unauthorized_member_cannot_read():
    """越权成员不能读取受控快照。"""
    _bind_and_refresh()
    svc = BoardSnapshotService()
    with pytest.raises(ScopeError):
        svc.read_snapshot(1, "u005")


def test_unauthorized_member_cannot_adjust_scope():
    """越权成员不能调整消费范围。"""
    _bind_and_refresh()
    svc = BoardSnapshotService()
    with pytest.raises(ScopeError):
        svc.adjust_scope(1, "u004", experiments={"EXPE-0001"})


def test_authorized_member_readonly_snapshot_unchanged():
    """消费端读取是只读行为：连续读取返回同一版本、同一聚合值。"""
    published = _bind_and_refresh()
    svc = BoardSnapshotService()
    again = svc.read_snapshot(1, "u002")  # u002 是 SCOPE_A 成员
    assert again["snapshot_version"] == published["snapshot_version"]
    assert again["aggregate"] == published["aggregate"]
    assert again["status"] == "published"


def test_bind_scope_rejects_outside_member():
    svc = BoardSnapshotService()
    with pytest.raises(ScopeError):
        svc.configure_scope(1, "SCOPE_A", "u004")


# ---------------------------------------------------------------- 3. 版本同步发布
def test_scope_version_and_snapshot_version_published_together():
    snapshot = _bind_and_refresh()
    assert snapshot["scope_version"] == 1
    assert snapshot["snapshot_version"] == 1
    svc = BoardSnapshotService()
    # 调整范围后刷新：两个版本号在同一份快照里同步推进
    svc.adjust_scope(1, "u001", experiments={"EXPE-0001"})
    with pytest.raises(SnapshotConflictError):
        # 旧基线版本号刷新会被乐观锁拦截
        svc.refresh(1, "u001", expected_scope_version=1)
    refreshed = svc.refresh(1, "u001")
    assert refreshed["scope_version"] == 2
    assert refreshed["snapshot_version"] == 2


# ---------------------------------------------------------------- 4. 失败保留旧快照
def test_refresh_failure_keeps_previous_snapshot():
    """注入发布前故障：新快照回滚，上一份获准范围结果继续可读。"""
    first = _bind_and_refresh()
    snapshot_store.arm_failure(1)
    svc = BoardSnapshotService()
    with pytest.raises(BoardSnapshotError):
        svc.refresh(1, "u001")
    kept = svc.read_snapshot(1, "u001")
    assert kept["snapshot_version"] == first["snapshot_version"]
    assert kept["aggregate"] == first["aggregate"]
    # 留档里能看到失败记录
    archive = svc.archive(1, "u001")
    assert any(item["status"] == "failed" for item in archive)


def test_no_empty_shell_when_no_valid_card():
    """范围收窄到没有任何通过 V011 的卡片时，中止刷新，不发空壳。"""
    _bind_and_refresh()
    svc = BoardSnapshotService()
    svc.adjust_scope(1, "u001", experiments={"EXPE-9999"})  # 无任何 AB 结果
    with pytest.raises(BoardSnapshotError):
        svc.refresh(1, "u001")
    kept = svc.read_snapshot(1, "u001")
    assert kept["cards"], "旧快照必须保留，不能出现空壳"


# ---------------------------------------------------------------- 5. V011 联动
def test_v011_rejects_unregistered_caliber_card():
    """EXPE-0003 的 METR-0003/V099 未登记，卡片必须被拒绝。"""
    svc = BoardSnapshotService()
    svc.configure_scope(1, "SCOPE_B", "u004")
    snapshot = svc.refresh(1, "u004")
    rejected = snapshot["aggregate"]["rejected_cards"]
    assert any(r["metric_version"] == "V099" for r in rejected)
    assert all(c["metric_version"] != "V099" for c in snapshot["cards"])
    # V011 核对明细里留下拒绝记录
    check_results = {(c["report_code"], c["result"]) for c in snapshot["v011_checks"]}
    assert ("AB_R-0006", "拒绝：未登记口径") in check_results


def test_v011_verify_endpoint_logic_all_pass_after_clean_scope():
    """干净范围下 V011 核对全部通过。"""
    _bind_and_refresh()
    svc = BoardSnapshotService()
    report = svc.v011_verify(1, "u001")
    assert report["all_pass"] is True
    assert all(c["registered"] for c in report["checks"])


def test_archive_keeps_every_published_version():
    """V011 留档核对：每次发布都留一份历史快照。"""
    _bind_and_refresh()
    svc = BoardSnapshotService()
    svc.adjust_scope(1, "u001", experiments={"EXPE-0001", "EXPE-0002"})
    svc.refresh(1, "u001")
    archive = svc.archive(1, "u001")
    assert len(archive) == 1  # 第二次发布时，第一版进入留档
    assert archive[0]["snapshot_version"] == 1


# ---------------------------------------------------------------- 6. 版本冲突两种策略
def test_conflict_rebuild_shows_only_new_scope():
    """全量重建：快照只显示新范围的完整聚合，旧范围实验全部移出。"""
    svc = BoardSnapshotService()
    svc.configure_scope(1, "SCOPE_A", "u001")
    svc.refresh(1, "u001")
    # 范围收窄：只保留 EXPE-0001
    svc.adjust_scope(1, "u001", experiments={"EXPE-0001"})
    rebuilt = svc.refresh(1, "u001", strategy="rebuild")
    assert {c["experiment_code"] for c in rebuilt["cards"]} == {"EXPE-0001"}
    assert rebuilt["pending_removals"] == []
    assert rebuilt["strategy"] == "rebuild"


def test_conflict_incremental_keeps_old_with_removal_list():
    """增量策略：保留旧范围聚合卡片，并附待移除实验清单。"""
    svc = BoardSnapshotService()
    svc.configure_scope(1, "SCOPE_A", "u001")
    svc.refresh(1, "u001")
    svc.adjust_scope(1, "u001", experiments={"EXPE-0001"})
    incremental = svc.refresh(1, "u001", strategy="incremental")
    # 旧范围的 EXPE-0002 卡片仍在展示
    shown = {c["experiment_code"] for c in incremental["cards"]}
    assert {"EXPE-0001", "EXPE-0002"} <= shown
    pending = {p["experiment_code"] for p in incremental["pending_removals"]}
    assert pending == {"EXPE-0002"}
    # 待移除清单按实验去重，同一实验的多张指标卡只列一条
    assert len(incremental["pending_removals"]) == 1
    assert set(incremental["pending_removals"][0]["metric_codes"]) == {"METR-0001", "METR-0003"}
    # 暂留旧范围卡片时，快照明确标记范围尚未完全收敛
    assert incremental["scope_consistent"] is False
    assert incremental["strategy"] == "incremental"
    # 聚合自解释：待移出成员 > 0，且展示成员 = 收敛成员 + 待移出成员
    agg = incremental["aggregate"]
    assert agg["pending_member_total"] > 0
    assert agg["member_total"] == agg["scoped_member_total"] + agg["pending_member_total"]


def test_rebuild_snapshot_is_scope_consistent():
    """全量重建后快照与当前消费范围完全一致。"""
    svc = BoardSnapshotService()
    svc.configure_scope(1, "SCOPE_A", "u001")
    svc.refresh(1, "u001")
    svc.adjust_scope(1, "u001", experiments={"EXPE-0001"})
    rebuilt = svc.refresh(1, "u001", strategy="rebuild")
    assert rebuilt["scope_consistent"] is True
    assert rebuilt["pending_removals"] == []
    assert rebuilt["aggregate"]["pending_member_total"] == 0
    assert rebuilt["aggregate"]["scoped_member_total"] == rebuilt["aggregate"]["member_total"]


def test_stale_baseline_refresh_raises_conflict():
    """带着过期范围版本号刷新，直接 409 语义冲突。"""
    _bind_and_refresh()
    svc = BoardSnapshotService()
    svc.adjust_scope(1, "u001", experiments={"EXPE-0001"})
    with pytest.raises(SnapshotConflictError):
        svc.refresh(1, "u001", expected_scope_version=1)


# ---------------------------------------------------------------- 7. 列表/详情同源
def test_list_and_detail_share_same_snapshot_data():
    """列表侧栏和详情页保留同一组数据。"""
    published = _bind_and_refresh(board_id=1)
    svc = BoardSnapshotService()
    boards = svc.list_boards()
    board_one = next(b for b in boards if b["id"] == 1)
    detail = svc.get_board(1)
    assert board_one["snapshot"]["snapshot_version"] == published["snapshot_version"]
    assert detail["snapshot"] == board_one["snapshot"]
    # 未刷新的看板快照为 None，不串数据
    board_three = next(b for b in boards if b["id"] == 3)
    assert board_three["snapshot"] is None


def test_consumer_gets_full_allowed_snapshot():
    """消费端拿到的快照包含获准范围内全部卡片与聚合，且不含越权实验。"""
    _bind_and_refresh()
    svc = BoardSnapshotService()
    snapshot = svc.read_snapshot(1, "u003")  # SCOPE_A 的另一名成员
    allowed = registry.scope_experiments("SCOPE_A")
    assert set(snapshot["aggregate"]["allowed_experiments"]) == set(allowed)
    assert all(c["experiment_code"] in allowed for c in snapshot["cards"])
    assert snapshot["scope_code"] == "SCOPE_A"


def test_read_before_any_refresh_raises_not_found():
    svc = BoardSnapshotService()
    svc.configure_scope(1, "SCOPE_A", "u001")
    with pytest.raises(SnapshotNotFoundError):
        svc.read_snapshot(1, "u001")
