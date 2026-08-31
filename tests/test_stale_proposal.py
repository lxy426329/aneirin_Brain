# ============================================================
# Test: Stale proposal protection (stale proposal 防护)
# 测试：过期提案防护
#
# 提案默认带有效期（PROPOSAL_TTL_DAYS 天），过期后禁止执行，
# 防止很久以前的旧提案在状态已变化后被批准执行。
# 验证：
#   1. add_pending_action 生成的提案带 expires_at
#   2. 过期提案 approve_action 拒绝执行
#   3. 过期提案 execute_change_proposal 拒绝执行
#   4. 未过期提案正常执行
#   5. 旧提案（无 expires_at 字段）按未过期处理（兼容历史数据）
# ============================================================

import os
import json
from datetime import datetime, timedelta, timezone

import pytest

from housekeeper import PROPOSAL_TTL_DAYS


@pytest.fixture
def identity_mgr(test_config):
    from identity_manager import IdentityManager
    return IdentityManager(test_config)


@pytest.fixture
def hk(test_config, bucket_mgr, identity_mgr):
    from housekeeper import Housekeeper
    return Housekeeper(test_config, bucket_mgr, None, identity_mgr=identity_mgr)


def _load_proposal(hk, aid):
    path = os.path.join(hk.echo_chamber.pending_actions_dir, f"{aid}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _force_expire(hk, aid):
    """把提案的 expires_at 改为过去时间，模拟过期。"""
    path = os.path.join(hk.echo_chamber.pending_actions_dir, f"{aid}.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------
# 1. 提案默认带 expires_at
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_proposal_has_expires_at(hk):
    aid = await hk.echo_chamber.add_pending_action("change", {"bucket_id": "x"})
    data = _load_proposal(hk, aid)
    assert data["expires_at"] is not None
    expiry = datetime.fromisoformat(data["expires_at"])
    created = datetime.fromisoformat(data["created"])
    assert (expiry - created).days == PROPOSAL_TTL_DAYS


# ---------------------------------------------------------
# 2. 过期提案 approve_action 拒绝
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_approve_expired_proposal_rejected(hk, bucket_mgr):
    bid = await bucket_mgr.create(content="待审批记忆", importance=3)
    aid = await hk.echo_chamber.add_pending_action(
        "change", {"bucket_id": bid, "updates": {"importance": 8}}
    )
    _force_expire(hk, aid)

    ok = await hk.approve_action(aid)
    assert ok is False
    # 记忆未被修改
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["importance"] == 3


# ---------------------------------------------------------
# 3. 过期提案 execute_change_proposal 拒绝
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_execute_expired_proposal_rejected(hk, bucket_mgr):
    bid = await bucket_mgr.create(content="待审批记忆", importance=3)
    aid = await hk.echo_chamber.add_pending_action(
        "change", {"bucket_id": bid, "updates": {"importance": 8}}
    )
    _force_expire(hk, aid)

    success, message = await hk.execute_change_proposal(aid)
    assert success is False
    assert "过期" in message
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["importance"] == 3


# ---------------------------------------------------------
# 4. 未过期提案正常执行
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_fresh_proposal_executes(hk, bucket_mgr):
    bid = await bucket_mgr.create(content="待审批记忆", importance=3)
    aid = await hk.echo_chamber.add_pending_action(
        "change", {"bucket_id": bid, "updates": {"importance": 8}}
    )

    ok = await hk.approve_action(aid)
    assert ok is True
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["importance"] == 8


# ---------------------------------------------------------
# 5. 旧提案（无 expires_at）按未过期处理
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_legacy_proposal_without_expiry_executes(hk, bucket_mgr):
    bid = await bucket_mgr.create(content="待审批记忆", importance=3)
    aid = await hk.echo_chamber.add_pending_action(
        "change", {"bucket_id": bid, "updates": {"importance": 8}}
    )
    # 模拟旧提案：删除 expires_at 字段
    path = os.path.join(hk.echo_chamber.pending_actions_dir, f"{aid}.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.pop("expires_at", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    ok = await hk.approve_action(aid)
    assert ok is True
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["importance"] == 8