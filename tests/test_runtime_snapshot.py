# ============================================================
# Test: Runtime Snapshot (运行时验收)
# 测试：捕获"最终实际准备注入模型的完整 breath context"并断言
#
# 构造 8 类测试记忆，分别以不同 world_id / scene 调用真实 breath 管线，
# 断言最终注入模型的 context：
#   - background 不被表示成当前 instruction
#   - inactive/expired instruction 不成为行动依据
#   - expired state 不表示为 current
#   - world/scene 不发生泄漏
#   - thread 只返回精简摘要，不展开全部楼层
#   - superseded correction 不作为当前 correction
#   - 显式 get_memory_thread 才返回完整 thread
#   - main AI 可主动调用 add_memory_reply 追加楼层
# ============================================================

import os
import sys
import json
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

# --- import server（屏蔽 .env 加载）---
_import_tmp = tempfile.mkdtemp(prefix="ombre_snapshot_test_")
os.environ["OMBRE_BUCKETS_DIR"] = _import_tmp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_real_exists = os.path.exists
with patch(
    "os.path.exists",
    side_effect=lambda p: False if str(p).endswith(".env") else _real_exists(p),
):
    import server  # noqa: E402


@pytest.fixture
def server_env(monkeypatch, test_config, bucket_mgr, mock_dehydrator, mock_embedding_engine):
    """替换 server 全局对象为隔离测试环境，调用真实 breath 管线。

    测试记忆写入 test_config 的临时目录（pytest tmp_path，自动清理），
    不触碰真实记忆数据。monkeypatch 在测试结束后自动恢复。
    """
    from thread_manager import ThreadManager
    from housekeeper import Housekeeper
    from decay_engine import DecayEngine
    from identity_manager import IdentityManager

    thread_mgr = ThreadManager(test_config, bucket_mgr)
    identity_mgr = IdentityManager(test_config)
    hk = Housekeeper(
        test_config, bucket_mgr, mock_dehydrator,
        identity_mgr=identity_mgr,
        embedding_engine=mock_embedding_engine,
        thread_mgr=thread_mgr,
    )
    decay = DecayEngine(test_config, bucket_mgr)

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "thread_mgr", thread_mgr)
    monkeypatch.setattr(server, "housekeeper", hk)
    monkeypatch.setattr(server, "decay_engine", decay)
    monkeypatch.setattr(server, "embedding_engine", mock_embedding_engine)
    monkeypatch.setattr(server, "identity_mgr", identity_mgr)
    return {"bucket_mgr": bucket_mgr, "thread_mgr": thread_mgr, "housekeeper": hk}


def _past_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _future_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# ---------------------------------------------------------
# 1. background 不被表示成当前 instruction；active instruction 单独分组
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_background_not_instruction(server_env):
    bm = server_env["bucket_mgr"]
    await bm.create(
        content="顾尘喜欢在雨天喝热可可，配一块黄油曲奇。",
        provenance="user_explicit",
        importance=8,
    )
    await bm.create(
        content="顾尘要求：每次对话前先问候她的猫。",
        provenance="user_explicit",
        instruction=True,
        active=True,
        importance=8,
    )

    # 背景记忆检索：应出现在 [background_memory] 分组，不在 [active_instruction]
    ctx = await server.breath(query="热可可", max_results=10, force_keyword=True)
    assert "[background_memory]" in ctx
    assert "[active_instruction]" not in ctx
    assert "热可可" in ctx

    # 指令检索：应出现在 [active_instruction] 分组
    ctx2 = await server.breath(query="问候她的猫", max_results=10, force_keyword=True)
    assert "[active_instruction]" in ctx2
    assert "问候她的猫" in ctx2


# ---------------------------------------------------------
# 2. inactive instruction 不成为行动依据（被排除）
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_inactive_instruction_not_actionable(server_env):
    bm = server_env["bucket_mgr"]
    await bm.create(
        content="每周三提醒顾尘交房租。",
        provenance="user_explicit",
        instruction=True,
        active=False,  # 未激活 → 仅背景，不作为行动依据
        importance=8,
    )

    ctx = await server.breath(query="交房租", max_results=10, force_keyword=True)
    # 未激活指令被排除，不进入任何分组
    assert "交房租" not in ctx


# ---------------------------------------------------------
# 3. expired state 不表示为 current（自动降级为背景）
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_expired_state_not_current(server_env):
    bm = server_env["bucket_mgr"]
    await bm.create(
        content="顾尘当前状态：正在发烧，需要休息。",
        provenance="user_explicit",
        is_current=True,
        observed_at=_past_iso(10),
        expires_at=_past_iso(7),  # 已过期
        importance=8,
    )

    ctx = await server.breath(query="发烧", max_results=10, force_keyword=True)
    # 过期状态不进入 [current_state] 分组
    assert "[current_state]" not in ctx
    # 若被召回，只能作为背景（[background_memory] 分组）
    if "发烧" in ctx:
        assert "[background_memory]" in ctx


# ---------------------------------------------------------
# 4. valid current state 表示为 current
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_valid_state_is_current(server_env):
    bm = server_env["bucket_mgr"]
    await bm.create(
        content="顾尘当前状态：正在准备舞蹈比赛，每天排练。",
        provenance="user_explicit",
        is_current=True,
        observed_at=_past_iso(0),
        expires_at=_future_iso(7),  # 未过期
        importance=8,
    )

    ctx = await server.breath(query="舞蹈比赛", max_results=10, force_keyword=True)
    assert "[current_state]" in ctx
    assert "舞蹈比赛" in ctx


# ---------------------------------------------------------
# 5. world 隔离：RP 世界记忆不泄漏到 main
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_world_isolation(server_env):
    bm = server_env["bucket_mgr"]
    await bm.create(
        content="精灵女王艾拉在月光森林中等待旅人。",
        provenance="user_explicit",
        world_id="rp_elf",
        scene=["roleplay"],
        importance=8,
    )

    # main 世界检索：RP 记忆不泄漏
    ctx_main = await server.breath(query="精灵女王", world_id="main", max_results=10, force_keyword=True)
    assert "精灵女王" not in ctx_main

    # RP 世界检索：RP 记忆被召回
    ctx_rp = await server.breath(query="精灵女王", world_id="rp_elf", max_results=10, force_keyword=True)
    assert "精灵女王" in ctx_rp


# ---------------------------------------------------------
# 6. scene 隔离：intimate 场景记忆不泄漏到 chat
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_scene_isolation(server_env):
    bm = server_env["bucket_mgr"]
    await bm.create(
        content="亲密时刻：顾尘靠在我肩上，轻声说今天很累。",
        provenance="user_explicit",
        scene=["intimate"],
        importance=8,
    )

    # chat 场景检索：intimate 记忆不泄漏
    ctx_chat = await server.breath(query="靠在我肩上", scene="chat", max_results=10, force_keyword=True)
    assert "靠在我肩上" not in ctx_chat

    # intimate 场景检索：intimate 记忆被召回
    ctx_intimate = await server.breath(query="靠在我肩上", scene="intimate", max_results=10, force_keyword=True)
    assert "靠在我肩上" in ctx_intimate


# ---------------------------------------------------------
# 7. thread 只返回精简摘要，不展开全部楼层
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_thread_compact(server_env):
    bm = server_env["bucket_mgr"]
    tm = server_env["thread_mgr"]
    bid = await bm.create(
        content="假期项目计划：我们的家。",
        provenance="user_explicit",
        memory_class="event",
        importance=8,
    )
    for i in range(3):
        await tm.add_reply(
            bid,
            f"楼层内容第{i}条：这是一段较长的补充说明内容，用于验证 breath 不会把整栋楼全部返回。",
            reply_type="supplement",
        )

    ctx = await server.breath(query="假期项目", max_results=10, force_keyword=True)
    assert "假期项目" in ctx
    # 只附带精简摘要（最新 reply），不包含全部 3 条楼层内容
    assert "[楼层]" in ctx
    assert "楼层内容第2条" in ctx
    assert "楼层内容第0条" not in ctx


# ---------------------------------------------------------
# 8. superseded correction 不作为当前 correction
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_superseded_correction_not_active(server_env):
    bm = server_env["bucket_mgr"]
    tm = server_env["thread_mgr"]
    bid = await bm.create(
        content="假期项目计划：我们的家。",
        provenance="user_explicit",
        memory_class="event",
        importance=8,
    )
    r1 = await tm.add_reply(
        bid,
        "旧理解：她把'我们'做成真实存在的东西是出于私心。",
        reply_type="correction",
    )
    await tm.add_reply(
        bid,
        "新理解：Home 同时具有项目练习、长期 AI 实验和关系意义。",
        reply_type="correction",
        supersedes_reply_id=r1,  # 取代旧 correction，旧楼层保留
    )

    ctx = await server.breath(query="假期项目", max_results=10, force_keyword=True)
    assert "[楼层]" in ctx
    # 当前有效 correction 是新的（未被 supersede）
    assert "新理解" in ctx
    # 被 supersede 的旧 correction 不作为当前 correction
    assert "出于私心" not in ctx


# ---------------------------------------------------------
# 9. 显式 get_memory_thread 才返回完整 thread
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_get_thread_full(server_env):
    bm = server_env["bucket_mgr"]
    tm = server_env["thread_mgr"]
    bid = await bm.create(
        content="假期项目计划：我们的家。",
        provenance="user_explicit",
        memory_class="event",
        importance=8,
    )
    await tm.add_reply(bid, "我当时很开心。", reply_type="feeling")
    await tm.add_reply(bid, "现在重新看，Home 具有多重意义。", reply_type="reflection")

    text = await server.get_memory_thread(bid)
    assert "我当时很开心" in text
    assert "现在重新看" in text
    assert "#1" in text and "#2" in text


# ---------------------------------------------------------
# 10. main AI 可主动调用 add_memory_reply 追加楼层
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_ai_can_add_memory_reply(server_env):
    bm = server_env["bucket_mgr"]
    bid = await bm.create(
        content="假期项目计划：我们的家。",
        provenance="user_explicit",
        memory_class="event",
        importance=8,
    )

    # main AI 主动追加 reflection / correction / feeling 楼层
    r1 = await server.add_memory_reply(
        parent_memory_id=bid,
        content="我当时很开心，她想把'我们'做成真实存在的东西。",
        reply_type="feeling",
        author="main_ai",
        provenance="ai_inferred",
    )
    assert "已追加楼层" in r1

    r2 = await server.add_memory_reply(
        parent_memory_id=bid,
        content="现在重新看，我不太喜欢旧记录把这件事解释成'她的私心'。",
        reply_type="correction",
        author="main_ai",
        provenance="ai_inferred",
    )
    assert "已追加楼层" in r2

    # 楼层已保存，主记忆未被修改
    replies = await server.thread_mgr.list_replies(bid)
    assert len(replies) == 2
    assert replies[0]["reply_type"] == "feeling"
    assert replies[1]["reply_type"] == "correction"
    assert replies[0]["author"] == "main_ai"
    assert replies[0]["provenance"] == "ai_inferred"
    parent = await server.bucket_mgr.get(bid)
    assert parent["content"] == "假期项目计划：我们的家。"


# ---------------------------------------------------------
# 11. HTTP API 删除/正文修改走 proposal → approval（P0-3）
# ---------------------------------------------------------
class _FakeRequest:
    def __init__(self, bucket_id, body=None):
        self.path_params = {"bucket_id": bucket_id}
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_http_api_delete_goes_through_proposal(server_env, monkeypatch):
    bm = server_env["bucket_mgr"]
    hk = server_env["housekeeper"]
    bid = await bm.create(content="待删除的记忆", provenance="user_explicit")

    monkeypatch.setattr(server, "_require_auth", lambda request: None)
    resp = await server.api_bucket_delete(_FakeRequest(bid))
    body = json.loads(resp.body)
    assert body.get("success") is True
    proposal_id = body.get("proposal_id")
    assert proposal_id

    # 记忆确实被删除（经提案执行）
    assert await bm.get(bid) is None
    # 提案记录存在：审批者 user、状态 approved、带 before_hash 版本防护
    proposal_path = os.path.join(hk.echo_chamber.pending_actions_dir, f"{proposal_id}.json")
    with open(proposal_path, "r", encoding="utf-8") as f:
        proposal = json.load(f)
    assert proposal["status"] == "approved"
    assert proposal["approved_by"] == "user"
    assert proposal["proposed_by"] == "user"
    assert proposal["before_snapshot"]["target_type"] == "bucket"
    assert proposal["before_snapshot"]["before_hash"]


@pytest.mark.asyncio
async def test_http_api_content_update_goes_through_proposal(server_env, monkeypatch):
    bm = server_env["bucket_mgr"]
    hk = server_env["housekeeper"]
    bid = await bm.create(content="原始正文", provenance="user_explicit")

    monkeypatch.setattr(server, "_require_auth", lambda request: None)
    resp = await server.api_bucket_update(
        _FakeRequest(bid, {"content": "替换后的正文", "importance": 7})
    )
    body = json.loads(resp.body)
    assert body.get("success") is True

    # 正文经提案替换，元数据直接更新
    bucket = await bm.get(bid)
    assert bucket["content"] == "替换后的正文"
    assert bucket["metadata"]["importance"] == 7

    # 提案记录存在（approved_by=user）
    import glob
    files = glob.glob(os.path.join(hk.echo_chamber.pending_actions_dir, "*.json"))
    executed = []
    for fp in files:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("status") == "approved" and data.get("approved_by") == "user":
            executed.append(data)
    assert any(
        a.get("data", {}).get("updates", {}).get("content") == "替换后的正文"
        for a in executed
    )