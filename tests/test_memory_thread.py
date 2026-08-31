# ============================================================
# Test: Memory Thread (记忆楼层 / Memory Thread)
# 测试：记忆楼层功能
#
# 核心目标：一条长期记忆的"事实主体"保持稳定，AI 后来的理解
# 变化以"楼层（reply）"形式追加，不覆盖原记忆。
# 验证（10 项）：
#   1. 创建 reply 不修改 parent memory
#   2. 多个 reply 按时间排序
#   3. correction 保留旧 reply
#   4. supersedes 不删除历史
#   5. world 隔离
#   6. scene 隔离
#   7. provenance 正确保存
#   8. 普通 breath 不返回全部楼层
#   9. 显式 get_memory_thread 可以返回完整历史
#   10. reply 删除/修改不能绕过 proposal → approval
# ============================================================

import os
import sys
import json
import tempfile
from unittest.mock import patch

import pytest

from thread_manager import ThreadManager


@pytest.fixture
def thread_mgr(test_config, bucket_mgr):
    return ThreadManager(test_config, bucket_mgr)


@pytest.fixture
def identity_mgr(test_config):
    from identity_manager import IdentityManager
    return IdentityManager(test_config)


@pytest.fixture
def hk(test_config, bucket_mgr, identity_mgr, thread_mgr):
    from housekeeper import Housekeeper
    return Housekeeper(test_config, bucket_mgr, None, identity_mgr=identity_mgr, thread_mgr=thread_mgr)


# ---------------------------------------------------------
# 1. 创建 reply 不修改 parent memory
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_reply_does_not_modify_parent(bucket_mgr, thread_mgr):
    bid = await bucket_mgr.create(
        content="假期项目计划：我们的家",
        bucket_type="dynamic",
        memory_class="event",
        provenance="user_explicit",
    )
    parent_before = await bucket_mgr.get(bid)

    reply_id = await thread_mgr.add_reply(
        parent_memory_id=bid,
        content="我当时很开心，她想把'我们'做成真实存在的东西。",
        reply_type="feeling",
        author="main_ai",
        provenance="ai_inferred",
    )
    assert reply_id

    parent_after = await bucket_mgr.get(bid)
    # 主记忆正文、created_at、metadata 均未改变
    assert parent_after["content"] == parent_before["content"]
    assert parent_after["metadata"]["created"] == parent_before["metadata"]["created"]
    assert parent_after["metadata"]["memory_class"] == "event"


# ---------------------------------------------------------
# 2. 多个 reply 按时间排序
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_replies_sorted_by_time(bucket_mgr, thread_mgr):
    bid = await bucket_mgr.create(content="测试记忆")
    r1 = await thread_mgr.add_reply(bid, "第一条", reply_type="supplement")
    r2 = await thread_mgr.add_reply(bid, "第二条", reply_type="reflection")
    r3 = await thread_mgr.add_reply(bid, "第三条", reply_type="feeling")

    replies = await thread_mgr.list_replies(bid)
    assert [r["reply_id"] for r in replies] == [r1, r2, r3]
    # created_at 升序
    times = [r["created_at"] for r in replies]
    assert times == sorted(times)


# ---------------------------------------------------------
# 3. correction 保留旧 reply
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_correction_keeps_old_reply(bucket_mgr, thread_mgr):
    bid = await bucket_mgr.create(content="旧记录")
    r1 = await thread_mgr.add_reply(bid, "旧理解：这是她的私心", reply_type="reflection")
    r2 = await thread_mgr.add_reply(
        bid,
        "纠正：Home 同时具有项目练习、长期 AI 实验和关系意义",
        reply_type="correction",
    )

    replies = await thread_mgr.list_replies(bid)
    assert len(replies) == 2
    # 旧 reply 仍保留
    assert replies[0]["reply_id"] == r1
    assert replies[1]["reply_id"] == r2


# ---------------------------------------------------------
# 4. supersedes 不删除历史
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_supersedes_keeps_history(bucket_mgr, thread_mgr):
    bid = await bucket_mgr.create(content="观点记忆")
    r1 = await thread_mgr.add_reply(bid, "旧观点", reply_type="reflection")
    r2 = await thread_mgr.add_reply(
        bid,
        "新观点取代旧观点",
        reply_type="correction",
        supersedes_reply_id=r1,
    )

    replies = await thread_mgr.list_replies(bid)
    assert len(replies) == 2  # 旧楼层仍保留
    assert replies[1]["supersedes_reply_id"] == r1
    # 旧 reply 内容未变
    old = await thread_mgr.get_reply(r1)
    assert old["content"] == "旧观点"


# ---------------------------------------------------------
# 5. world 隔离
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_world_isolation(bucket_mgr, thread_mgr):
    # main 世界记忆
    bid_main = await bucket_mgr.create(content="主世界记忆", world_id="main")
    # RP 世界记忆
    bid_rp = await bucket_mgr.create(content="RP 世界记忆", world_id="rp_alpha")

    # 默认继承 parent 的 world_id
    r_main = await thread_mgr.add_reply(bid_main, "main 楼层")
    r_rp = await thread_mgr.add_reply(bid_rp, "RP 楼层")

    assert (await thread_mgr.get_reply(r_main))["world_id"] == "main"
    assert (await thread_mgr.get_reply(r_rp))["world_id"] == "rp_alpha"

    # 显式覆盖 world_id
    r_override = await thread_mgr.add_reply(bid_main, "显式覆盖", world_id="rp_beta")
    assert (await thread_mgr.get_reply(r_override))["world_id"] == "rp_beta"


# ---------------------------------------------------------
# 6. scene 隔离
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_scene_isolation(bucket_mgr, thread_mgr):
    bid = await bucket_mgr.create(content="亲密互动记忆", scene=["intimate"])

    # 默认继承 parent 的 scene
    r1 = await thread_mgr.add_reply(bid, "继承 scene")
    assert (await thread_mgr.get_reply(r1))["scene"] == ["intimate"]

    # 显式覆盖 scene
    r2 = await thread_mgr.add_reply(bid, "显式覆盖 scene", scene=["chat"])
    assert (await thread_mgr.get_reply(r2))["scene"] == ["chat"]


# ---------------------------------------------------------
# 7. provenance 正确保存
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_provenance_saved(bucket_mgr, thread_mgr):
    bid = await bucket_mgr.create(content="来源测试记忆")

    r1 = await thread_mgr.add_reply(bid, "用户补充", author="user", provenance="user_explicit")
    r2 = await thread_mgr.add_reply(bid, "AI 观察", author="main_ai", provenance="ai_observed")
    r3 = await thread_mgr.add_reply(bid, "AI 新理解", author="main_ai", provenance="ai_inferred")
    r4 = await thread_mgr.add_reply(bid, "历史导入", author="system", provenance="imported")

    assert (await thread_mgr.get_reply(r1))["provenance"] == "user_explicit"
    assert (await thread_mgr.get_reply(r2))["provenance"] == "ai_observed"
    assert (await thread_mgr.get_reply(r3))["provenance"] == "ai_inferred"
    assert (await thread_mgr.get_reply(r4))["provenance"] == "imported"

    # 非法 provenance 回退 legacy
    r5 = await thread_mgr.add_reply(bid, "非法来源", provenance="hacker")
    assert (await thread_mgr.get_reply(r5))["provenance"] == "legacy"


# ---------------------------------------------------------
# 8-9. 需要 import server（屏蔽 .env）
# ---------------------------------------------------------
_import_tmp = tempfile.mkdtemp(prefix="ombre_thread_test_")
os.environ["OMBRE_BUCKETS_DIR"] = _import_tmp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_real_exists = os.path.exists
with patch(
    "os.path.exists",
    side_effect=lambda p: False if str(p).endswith(".env") else _real_exists(p),
):
    import server  # noqa: E402


# ---------------------------------------------------------
# 8. 普通 breath 不返回全部楼层
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_breath_does_not_return_full_thread():
    bid = await server.bucket_mgr.create(content="楼层测试主记忆", provenance="user_explicit")
    # 追加 3 条楼层
    for i in range(3):
        await server.thread_mgr.add_reply(
            bid,
            f"楼层内容第{i}条：这是一段较长的补充说明内容，用于验证 breath 不会把整栋楼全部返回。",
            reply_type="supplement",
        )

    result = await server._breath_lightweight(query="楼层测试")
    assert result["results"], "应至少召回一条记忆"
    target = [r for r in result["results"] if r["bucket_id"] == bid]
    assert target
    latest_reply = target[0].get("latest_reply", "")
    # 只附带精简摘要（最新 reply），不包含全部 3 条楼层内容
    assert "楼层内容第2条" in latest_reply
    assert "楼层内容第0条" not in latest_reply


# ---------------------------------------------------------
# 9. 显式 get_memory_thread 返回完整历史
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_get_memory_thread_full_history():
    bid = await server.bucket_mgr.create(content="完整历史主记忆", provenance="user_explicit")
    r1 = await server.thread_mgr.add_reply(bid, "第一条楼层", reply_type="feeling")
    r2 = await server.thread_mgr.add_reply(bid, "第二条楼层", reply_type="correction")
    r3 = await server.thread_mgr.add_reply(bid, "第三条楼层", reply_type="reflection")

    text = await server.get_memory_thread(bid)
    assert "第一条楼层" in text
    assert "第二条楼层" in text
    assert "第三条楼层" in text
    assert "#1" in text and "#3" in text


# ---------------------------------------------------------
# 10. reply 删除/修改不能绕过 proposal → approval
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_reply_mutation_requires_proposal(hk, thread_mgr, bucket_mgr):
    bid = await bucket_mgr.create(content="楼层审批测试")
    reply_id = await thread_mgr.add_reply(bid, "待审批楼层", reply_type="reflection")

    # 生成删除提案（不直接删除）
    aid = await hk.echo_chamber.add_pending_action(
        "change",
        {"reply_id": reply_id, "delete_reply": True, "reason": "测试删除楼层"},
    )
    # 未批准前楼层仍存在
    assert await thread_mgr.get_reply(reply_id) is not None

    # 批准后楼层被删除
    ok = await hk.approve_action(aid, approved_by="user")
    assert ok is True
    assert await thread_mgr.get_reply(reply_id) is None


@pytest.mark.asyncio
async def test_reply_update_requires_proposal(hk, thread_mgr, bucket_mgr):
    bid = await bucket_mgr.create(content="楼层修改测试")
    reply_id = await thread_mgr.add_reply(bid, "原始内容", reply_type="reflection")

    # 生成修改提案
    aid = await hk.echo_chamber.add_pending_action(
        "change",
        {"reply_id": reply_id, "reply_updates": {"content": "修改后的内容"}},
    )
    # 未批准前内容不变
    assert (await thread_mgr.get_reply(reply_id))["content"] == "原始内容"

    # 批准后内容更新
    ok = await hk.approve_action(aid, approved_by="user")
    assert ok is True
    assert (await thread_mgr.get_reply(reply_id))["content"] == "修改后的内容"