# ============================================================
# Test: Approval flow with approved_by (修正 6)
# 测试：审批流程与审批者记录（修正 6）
#
# 修正 6：管家审批机制不要把审批者硬编码为 user。
#         所有改动仍必须先生成 proposal。
#         审批记录保留 approved_by 字段，为未来支持 main_ai / user 两级权限留接口。
#         禁止任何管家或 ai_manage 路径绕过 proposal 直接删除/修改长期记忆。
# 验证：
#   1. add_pending_action 记录 proposed_by（默认 ai_manage）
#   2. approve_action 记录 approved_by="user"（默认）
#   3. approve_action 记录 approved_by="main_ai"（两级权限接口）
#   4. 提案状态流转：pending → approved，approved_by/approved_at 被持久化
# ============================================================

import os
import json
import pytest


@pytest.fixture
def identity_mgr(test_config):
    from identity_manager import IdentityManager
    return IdentityManager(test_config)


@pytest.fixture
def hk(test_config, bucket_mgr, identity_mgr):
    from housekeeper import Housekeeper
    return Housekeeper(test_config, bucket_mgr, None, identity_mgr=identity_mgr)


# ---------------------------------------------------------
# 1. add_pending_action 记录 proposed_by
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_add_pending_action_records_proposed_by(hk):
    aid = await hk.echo_chamber.add_pending_action("change", {"bucket_id": "x"})
    path = os.path.join(hk.echo_chamber.pending_actions_dir, f"{aid}.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["proposed_by"] == "ai_manage"
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_add_pending_action_custom_proposed_by(hk):
    aid = await hk.echo_chamber.add_pending_action(
        "change", {"bucket_id": "x"}, proposed_by="main_ai"
    )
    path = os.path.join(hk.echo_chamber.pending_actions_dir, f"{aid}.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["proposed_by"] == "main_ai"


# ---------------------------------------------------------
# 2. approve_action 记录 approved_by="user"（默认）
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_approve_action_records_approved_by_user(hk, bucket_mgr):
    bid = await bucket_mgr.create(content="待审批记忆", importance=3)
    aid = await hk.echo_chamber.add_pending_action(
        "change", {"bucket_id": bid, "updates": {"importance": 8}}
    )

    ok = await hk.approve_action(aid)
    assert ok

    path = os.path.join(hk.echo_chamber.pending_actions_dir, f"{aid}.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["status"] == "approved"
    assert data["approved_by"] == "user"
    assert data["approved_at"] is not None
    # 提案已实际执行
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["importance"] == 8


# ---------------------------------------------------------
# 3. approve_action 记录 approved_by="main_ai"（两级权限接口）
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_approve_action_records_approved_by_main_ai(hk, bucket_mgr):
    bid = await bucket_mgr.create(content="待审批记忆", importance=3)
    aid = await hk.echo_chamber.add_pending_action(
        "change", {"bucket_id": bid, "updates": {"importance": 9}}
    )

    ok = await hk.approve_action(aid, approved_by="main_ai")
    assert ok

    path = os.path.join(hk.echo_chamber.pending_actions_dir, f"{aid}.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["status"] == "approved"
    assert data["approved_by"] == "main_ai"


# ---------------------------------------------------------
# 4. 提案状态流转：pending → approved，字段持久化
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_approval_flow_status_transition(hk, bucket_mgr):
    bid = await bucket_mgr.create(content="状态流转记忆", importance=3)
    aid = await hk.echo_chamber.add_pending_action(
        "change", {"bucket_id": bid, "updates": {"importance": 7}}
    )

    # 初始 pending
    path = os.path.join(hk.echo_chamber.pending_actions_dir, f"{aid}.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["status"] == "pending"

    # 批准后 approved + approved_by + approved_at
    ok = await hk.approve_action(aid, approved_by="user")
    assert ok
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["status"] == "approved"
    assert data["approved_by"] == "user"
    assert data["approved_at"]
    assert data["executed_at"]

    # 重复批准应失败（幂等保护）
    ok2 = await hk.approve_action(aid, approved_by="user")
    assert ok2 is False