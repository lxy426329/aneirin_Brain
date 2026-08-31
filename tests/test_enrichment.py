# ============================================================
# Test: Legacy memory metadata enrichment proposal (旧记忆补全提案)
# 测试：旧记忆 metadata 补全提案
#
# 为 memory_class 缺失的旧记忆生成 enrich 提案（不直接修改），
# 由用户/主 AI 审批后执行补全。
# 验证：
#   1. _infer_memory_class 映射正确
#   2. propose_metadata_enrichment 生成提案，不直接修改记忆
#   3. 批准后 memory_class 被补全
#   4. 已有 memory_class 的记忆不生成提案
# ============================================================

import os
import json

import pytest

from housekeeper import _infer_memory_class


@pytest.fixture
def identity_mgr(test_config):
    from identity_manager import IdentityManager
    return IdentityManager(test_config)


@pytest.fixture
def hk(test_config, bucket_mgr, identity_mgr):
    from housekeeper import Housekeeper
    return Housekeeper(test_config, bucket_mgr, None, identity_mgr=identity_mgr)


# ---------------------------------------------------------
# 1. _infer_memory_class 映射
# ---------------------------------------------------------
def test_infer_memory_class_mapping():
    assert _infer_memory_class("boundary") == "boundary"
    assert _infer_memory_class("permanent") == "principle"
    assert _infer_memory_class("pattern") == "experience"
    assert _infer_memory_class("identity") == "person"
    assert _infer_memory_class("dynamic") == "event"
    assert _infer_memory_class("feel") == "experience"
    # 无法可靠推断 → None（不强行推断）
    assert _infer_memory_class("unknown_type") is None
    assert _infer_memory_class("") is None


# ---------------------------------------------------------
# 2. 生成提案，不直接修改记忆
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_propose_enrichment_generates_proposal(hk, bucket_mgr):
    # 旧记忆：type=permanent，无 memory_class
    bid = await bucket_mgr.create(
        content="永久原则：诚实",
        bucket_type="permanent",
    )
    # 确认当前无 memory_class
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["memory_class"] is None

    result = await hk.propose_metadata_enrichment(limit=10)
    assert result["enrichment_proposals_created"] >= 1

    # 记忆未被直接修改（只生成提案）
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["memory_class"] is None

    # 提案文件已生成
    pending_dir = hk.echo_chamber.pending_actions_dir
    proposals = [
        json.load(open(os.path.join(pending_dir, f), encoding="utf-8"))
        for f in os.listdir(pending_dir)
        if f.endswith(".json")
    ]
    enrich_proposals = [p for p in proposals if p["action_type"] == "enrich"]
    assert len(enrich_proposals) >= 1
    assert enrich_proposals[0]["data"]["updates"]["memory_class"] == "principle"


# ---------------------------------------------------------
# 3. 批准后 memory_class 被补全
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_approve_enrichment_applies(hk, bucket_mgr):
    bid = await bucket_mgr.create(
        content="永久原则：诚实",
        bucket_type="permanent",
    )
    await hk.propose_metadata_enrichment(limit=10)

    # 找到 enrich 提案并批准
    pending_dir = hk.echo_chamber.pending_actions_dir
    for f in os.listdir(pending_dir):
        if not f.endswith(".json"):
            continue
        with open(os.path.join(pending_dir, f), encoding="utf-8") as fh:
            data = json.load(fh)
        if data["action_type"] == "enrich" and data["data"]["bucket_id"] == bid:
            ok = await hk.approve_action(data["action_id"], approved_by="user")
            assert ok is True
            break

    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["memory_class"] == "principle"


# ---------------------------------------------------------
# 4. 已有 memory_class 的记忆不生成提案
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_no_proposal_for_complete_memory(hk, bucket_mgr):
    await bucket_mgr.create(
        content="已有类别的记忆",
        bucket_type="permanent",
        memory_class="principle",
    )
    result = await hk.propose_metadata_enrichment(limit=10)
    # 该记忆已有 memory_class，不应为其生成提案
    pending_dir = hk.echo_chamber.pending_actions_dir
    proposals = [
        json.load(open(os.path.join(pending_dir, f), encoding="utf-8"))
        for f in os.listdir(pending_dir)
        if f.endswith(".json")
    ]
    enrich_proposals = [p for p in proposals if p["action_type"] == "enrich"]
    assert len(enrich_proposals) == 0