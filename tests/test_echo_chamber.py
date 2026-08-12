# ============================================================
# Test: Echo Chamber & Global Audit Regression
# 测试：回声壁闭环 + 全局审计修复回归
#
# Verifies:
#   1. EchoChamber 提案读写与审批状态同步
#   2. approve_action 四种类型自动执行落地
#      (cleanup 删除+清向量 / conflict 标记resolved+superseded_by /
#       chain_merge 合并去重+删次链 / identity_proposal 身份层创建档案)
#   3. reject_action 状态同步
#   4. EventChain.from_dict 损坏数据容错
#   5. 身份检测对已收录人物不重复提案
#   6. 冲突提案包含 old_memory_id / new_memory_id / 冲突原因
#   7. ensure_started 并发启动竞态（只启动一个后台任务）
#   8. identity 时区混算修复（衰减权重不再抛 TypeError）
#   9. bucket 安全转换（valence=0.0 不被吞掉）
# ============================================================

import os
import json
import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import frontmatter


# ---------------------------------------------------------
# 本地 fixtures
# ---------------------------------------------------------
@pytest.fixture
def identity_mgr(test_config):
    from identity_manager import IdentityManager
    return IdentityManager(test_config)


@pytest.fixture
def housekeeper(test_config, bucket_mgr, identity_mgr):
    from housekeeper import Housekeeper
    return Housekeeper(test_config, bucket_mgr, None, identity_mgr=identity_mgr)


async def _patch_created(bucket_mgr, bucket_id, days_ago):
    """Patch a bucket's created timestamp for conflict tests."""
    fpath = bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(fpath)
    post["created"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))


def _write_action(hk, action_type, data):
    """Directly write a pending action JSON file."""
    import uuid
    aid = str(uuid.uuid4())[:8]
    path = os.path.join(hk.echo_chamber.pending_actions_dir, f"{aid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "action_id": aid,
            "action_type": action_type,
            "status": "pending",
            "data": data,
            "created": datetime.now(timezone.utc).isoformat(),
        }, f, ensure_ascii=False, indent=2)
    return aid


# ---------------------------------------------------------
# 1. EchoChamber 提案读写与状态同步
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_echo_chamber_add_and_status_sync(test_config):
    from housekeeper import EchoChamber
    chamber = EchoChamber(os.path.join(test_config["buckets_dir"], "echo_chamber"))

    await chamber.add_pending_action("cleanup", {"bucket_id": "x", "reason": "测试"})
    actions = await chamber.get_pending_actions()
    assert len(actions) == 1
    aid = actions[0]["action_id"]

    # approve → 不再出现在 pending 中
    assert await chamber.update_action_status(aid, "approved") is True
    assert len(await chamber.get_pending_actions()) == 0

    # reject → 同理
    await chamber.add_pending_action("conflict", {"old_memory_id": "y"})
    a2 = (await chamber.get_pending_actions())[0]
    assert await chamber.update_action_status(a2["action_id"], "rejected") is True
    assert len(await chamber.get_pending_actions()) == 0


# ---------------------------------------------------------
# 2. cleanup 审批：删除桶 + 清理向量
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_approve_cleanup_deletes_bucket_and_embedding(housekeeper, bucket_mgr):
    bid = await bucket_mgr.create(content="过期的低权重记忆", importance=2)
    mock_ee = MagicMock()
    mock_ee.delete_embedding = MagicMock()
    housekeeper.embedding_engine = mock_ee

    aid = _write_action(housekeeper, "cleanup", {"bucket_id": bid, "reason": "30天未访问"})
    assert await housekeeper.approve_action(aid) is True

    # 桶文件已被移入回收站（不再可读）
    assert await bucket_mgr.get(bid) is None
    # 向量库已同步清理
    mock_ee.delete_embedding.assert_called_once_with(bid)


# ---------------------------------------------------------
# 3. conflict 审批：旧记忆标记 resolved + superseded_by
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_approve_conflict_marks_old_resolved(housekeeper, bucket_mgr):
    old_id = await bucket_mgr.create(content="我不喜欢吃甜的", importance=5)
    new_id = await bucket_mgr.create(content="我今天喜欢吃全糖奶茶", importance=5)

    aid = _write_action(housekeeper, "conflict", {
        "old_memory_id": old_id,
        "new_memory_id": new_id,
        "conflict_type": "preference",
        "conflict_reason": "偏好冲突测试",
    })
    assert await housekeeper.approve_action(aid) is True

    old = await bucket_mgr.get(old_id)
    assert old["metadata"].get("resolved") is True
    assert old["metadata"].get("superseded_by") == new_id


# ---------------------------------------------------------
# 4. chain_merge 审批：合并 + 去重 + 删除次链
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_approve_chain_merge(housekeeper):
    from housekeeper import EventChain

    primary = EventChain("chainA", "项目A开发", "in_progress")
    primary.timeline = [
        {"memory_id": "m1", "timestamp": "2026-08-01T00:00:00", "content_preview": "启动"},
        {"memory_id": "m2", "timestamp": "2026-08-02T00:00:00", "content_preview": "开发"},
    ]
    primary.source_bucket_ids = ["m1", "m2"]
    await housekeeper._save_event_chain(primary)

    secondary = EventChain("chainB", "项目A进度", "in_progress")
    secondary.timeline = [
        {"memory_id": "m2", "timestamp": "2026-08-02T00:00:00", "content_preview": "开发"},
        {"memory_id": "m3", "timestamp": "2026-08-03T00:00:00", "content_preview": "测试"},
    ]
    secondary.source_bucket_ids = ["m2", "m3"]
    await housekeeper._save_event_chain(secondary)

    aid = _write_action(housekeeper, "chain_merge", {
        "primary_chain_id": "chainA",
        "secondary_chain_id": "chainB",
        "similarity": 80,
    })
    assert await housekeeper.approve_action(aid) is True

    merged = await housekeeper.get_event_chain("chainA")
    # 时间线按时间排序 + 按 memory_id 去重（m2 只出现一次）
    assert [e["memory_id"] for e in merged.timeline] == ["m1", "m2", "m3"]
    # 来源桶去重
    assert sorted(merged.source_bucket_ids) == ["m1", "m2", "m3"]
    # 次链文件被删除
    assert await housekeeper.get_event_chain("chainB") is None
    # 摘要重构包含次链主题
    assert "项目A进度" in merged.summary


# ---------------------------------------------------------
# 5. identity_proposal 审批：身份层创建档案
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_approve_identity_proposal_creates_identity(housekeeper, identity_mgr):
    aid = _write_action(housekeeper, "identity_proposal", {
        "person_name": "小明",
        "mention_count": 5,
        "dominant_emotion": "正面",
        "sample_context": "小明最近经常找我聊天",
    })
    assert await housekeeper.approve_action(aid) is True

    ident = await identity_mgr.get_by_name("小明")
    assert ident is not None
    assert ident["metadata"]["core_traits"] == ["正面"]


# ---------------------------------------------------------
# 6. reject_action 状态同步
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_reject_action(housekeeper):
    aid = _write_action(housekeeper, "cleanup", {"bucket_id": "zz", "reason": "测试"})
    assert await housekeeper.reject_action(aid) is True
    path = os.path.join(housekeeper.echo_chamber.pending_actions_dir, f"{aid}.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["status"] == "rejected"


# ---------------------------------------------------------
# 7. EventChain.from_dict 损坏数据容错
# ---------------------------------------------------------
def test_event_chain_from_dict_tolerates_missing_keys():
    from housekeeper import EventChain

    # 空 dict 不抛 KeyError
    chain = EventChain.from_dict({})
    assert chain.chain_id == ""

    # None 不抛异常
    chain2 = EventChain.from_dict(None)
    assert chain2.topic == "未命名事件链"

    # timeline 非 list 时降级为空列表
    chain3 = EventChain.from_dict({"chain_id": "c1", "timeline": "bad"})
    assert chain3.timeline == []

    # 正常数据保持
    chain4 = EventChain.from_dict({
        "chain_id": "c4", "topic": "主题",
        "timeline": [{"memory_id": "m1", "timestamp": "2026-01-01"}],
    })
    assert len(chain4.timeline) == 1


# ---------------------------------------------------------
# 8. 身份检测：已收录人物不再重复提案
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_identity_detection_skips_existing(housekeeper, bucket_mgr, identity_mgr):
    # 已有身份档案 小明
    await identity_mgr.create(name="小明", aliases=["阿明"])
    # 近7天3条提及 小明 的记忆
    for i in range(3):
        await bucket_mgr.create(content=f"昨天跟小明一起吃饭聊了很久{i}")
    # 未收录的新人物
    await bucket_mgr.create(content="昨天跟小红一起去逛街聊了很多")
    await bucket_mgr.create(content="昨天跟小红一起吃饭很开心")
    await bucket_mgr.create(content="小红给我打电话说想我了")

    result = await housekeeper._daily_identity_detection()
    assert result.get("identity_proposals_created", 0) == 1

    actions = await housekeeper.echo_chamber.get_pending_actions(action_type="identity_proposal")
    names = [a["data"]["person_name"] for a in actions]
    assert "小明" not in names
    assert "小红" in names


# ---------------------------------------------------------
# 9. 冲突检测提案字段完整性
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_conflict_proposal_contains_memory_ids(housekeeper, bucket_mgr):
    old_id = await bucket_mgr.create(content="我以前不喜欢吃辣", importance=5)
    await _patch_created(bucket_mgr, old_id, days_ago=3)
    new_id = await bucket_mgr.create(content="我今天特别想吃辣", importance=5)

    result = await housekeeper._daily_conflict_detection()
    assert result.get("conflicts_found", 0) >= 1

    actions = await housekeeper.echo_chamber.get_pending_actions(action_type="conflict")
    data = actions[0]["data"]
    assert data["old_memory_id"] == old_id
    assert data["new_memory_id"] == new_id
    assert data["old_bucket_id"] == old_id
    assert data["new_bucket_id"] == new_id
    assert data["conflict_type"]
    assert data["conflict_reason"]


# ---------------------------------------------------------
# 10. ensure_started 并发竞态：只启动一个后台任务
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_ensure_started_race(decay_eng):
    calls = 0
    orig_start = decay_eng.start

    async def counting_start():
        nonlocal calls
        calls += 1
        await orig_start()

    decay_eng.start = counting_start
    await asyncio.gather(decay_eng.ensure_started(), decay_eng.ensure_started())
    assert calls == 1
    assert decay_eng.is_running
    await decay_eng.stop()


# ---------------------------------------------------------
# 11. identity 时区混算修复（不再抛 TypeError）
# ---------------------------------------------------------
def test_identity_decay_weight_timezone(identity_mgr):
    # 最近提及（aware ISO 时间）→ 权重接近基础值，不抛异常
    w1 = identity_mgr.calculate_decayed_weight(10.0, datetime.now(timezone.utc).isoformat())
    assert w1 > 9.0
    # naive 时间戳也可计算（不再 naive/aware 混算崩溃）
    w2 = identity_mgr.calculate_decayed_weight(10.0, datetime.now().isoformat())
    assert 0.0 < w2 <= 10.0


# ---------------------------------------------------------
# 12. bucket 安全转换：valence=0.0 不被吞掉
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_bucket_valence_zero_not_swallowed(bucket_mgr):
    bid = await bucket_mgr.create(content="测试", importance=5, valence=0.0, arousal=0.1)
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["valence"] == 0.0

    # 脏数据（字符串 valence）不导致加载崩溃
    fpath = bucket_mgr._find_bucket_file(bid)
    post = frontmatter.load(fpath)
    post["valence"] = "不是数字"
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    bucket2 = await bucket_mgr.get(bid)
    assert bucket2 is not None
    assert bucket2["metadata"].get("valence", "不是数字") == "不是数字"
