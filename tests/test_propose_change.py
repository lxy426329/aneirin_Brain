# ============================================================
# Test: propose_change / apply_change (P0-2)
# 测试：AI 管家修改候选提案机制（P0-2）
#
# 需求 6：AI 管家只能提出修改候选，不能自动修改或删除记忆。
# 验证（housekeeper 提案执行层）：
#   1. add_pending_action 返回 action_id
#   2. change 提案执行：修改记忆元数据
#   3. change 提案执行：删除记忆
#   4. execute_change_proposal 拒绝非 pending 提案（幂等保护）
#   5. organize 提案执行：批量降权
# ============================================================

import os
import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import frontmatter


@pytest.fixture
def identity_mgr(test_config):
    from identity_manager import IdentityManager
    return IdentityManager(test_config)


@pytest.fixture
def hk(test_config, bucket_mgr, identity_mgr):
    from housekeeper import Housekeeper
    return Housekeeper(test_config, bucket_mgr, None, identity_mgr=identity_mgr)


async def _write_change_proposal(hk, data):
    """通过 echo_chamber 生成 change 提案，返回 action_id。"""
    return await hk.echo_chamber.add_pending_action("change", data)


# ---------------------------------------------------------
# 1. add_pending_action 返回 action_id
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_add_pending_action_returns_id(hk):
    aid = await hk.echo_chamber.add_pending_action("change", {"bucket_id": "x"})
    assert aid
    path = os.path.join(hk.echo_chamber.pending_actions_dir, f"{aid}.json")
    assert os.path.exists(path)


# ---------------------------------------------------------
# 2. change 提案执行：修改记忆元数据
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_execute_change_proposal_updates_bucket(hk, bucket_mgr):
    bid = await bucket_mgr.create(content="测试记忆", importance=3)
    aid = await _write_change_proposal(hk, {"bucket_id": bid, "updates": {"importance": 8}})

    success, message = await hk.execute_change_proposal(aid)
    assert success
    assert "已执行" in message
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["importance"] == 8


# ---------------------------------------------------------
# 3. change 提案执行：删除记忆
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_execute_change_proposal_deletes_bucket(hk, bucket_mgr):
    bid = await bucket_mgr.create(content="待删除记忆", importance=3)
    aid = await _write_change_proposal(hk, {"bucket_id": bid, "delete": True})

    success, message = await hk.execute_change_proposal(aid)
    assert success
    assert await bucket_mgr.get(bid) is None


# ---------------------------------------------------------
# 4. execute_change_proposal 拒绝非 pending 提案（幂等保护）
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_execute_change_proposal_rejects_non_pending(hk, bucket_mgr):
    bid = await bucket_mgr.create(content="测试记忆", importance=3)
    aid = await _write_change_proposal(hk, {"bucket_id": bid, "updates": {"importance": 8}})

    # 第一次执行成功
    success, _ = await hk.execute_change_proposal(aid)
    assert success
    # 第二次执行应被拒绝（状态已为 executed）
    success, message = await hk.execute_change_proposal(aid)
    assert not success
    assert "无法执行" in message


# ---------------------------------------------------------
# 5. organize 提案执行：批量降权
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_execute_organize_proposal_batch_drop(hk, bucket_mgr):
    bids = []
    for content in ("旧记忆A", "旧记忆B"):
        bid = await bucket_mgr.create(content=content, importance=5)
        bids.append(bid)
        fpath = bucket_mgr._find_bucket_file(bid)
        post = frontmatter.load(fpath)
        post["last_active"] = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

    aid = await hk.echo_chamber.add_pending_action("organize", {
        "candidates": [{"bucket_id": b, "current_importance": 5} for b in bids],
        "importance_drop": 2,
        "days": 30,
    })

    success, message = await hk.execute_change_proposal(aid)
    assert success
    assert "2/2" in message
    for bid in bids:
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["importance"] == 3  # 5 - 2