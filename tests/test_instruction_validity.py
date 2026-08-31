# ============================================================
# Test: Instruction validity (修正 2)
# 测试：指令有效性（修正 2）
#
# 修正 2：不要把历史 instruction 自动视为当前有效指令。
#         历史要求默认仅背景；只有 active 且未过期（满足触发条件）才可作为行动依据。
# 验证：
#   1. instruction=True 但 active=False → 不作为行动依据
#   2. instruction=True + active=True + 未过期 → 可作为行动依据
#   3. instruction=True + active=True + 已过期 → 不作为行动依据
#   4. 非指令记忆不受影响
# ============================================================

import pytest


@pytest.mark.asyncio
async def test_instruction_inactive_not_active(bucket_mgr):
    """历史 instruction 默认仅背景：active=False 时不可作为行动依据。"""
    bid = await bucket_mgr.create(
        content="用户曾要求：每天提醒喝水",
        instruction=True,
        active=False,
    )
    bucket = await bucket_mgr.get(bid)
    meta = bucket["metadata"]
    assert meta["instruction"] is True
    assert meta["active"] is False
    assert bucket_mgr._is_instruction_active(meta) is False


@pytest.mark.asyncio
async def test_instruction_active_not_expired(bucket_mgr):
    """当前仍有效且未过期的指令可作为行动依据。"""
    bid = await bucket_mgr.create(
        content="用户要求：本周内完成项目报告",
        instruction=True,
        active=True,
        expires_at="2099-12-31T23:59:59+00:00",
    )
    bucket = await bucket_mgr.get(bid)
    meta = bucket["metadata"]
    assert bucket_mgr._is_instruction_active(meta) is True


@pytest.mark.asyncio
async def test_instruction_active_but_expired(bucket_mgr):
    """已过期的指令自动失效，不作为行动依据。"""
    bid = await bucket_mgr.create(
        content="用户曾要求：明天早上叫我",
        instruction=True,
        active=True,
        expires_at="2020-01-01T00:00:00+00:00",
    )
    bucket = await bucket_mgr.get(bid)
    meta = bucket["metadata"]
    assert bucket_mgr._is_instruction_active(meta) is False


@pytest.mark.asyncio
async def test_non_instruction_unaffected(bucket_mgr):
    """非指令记忆不受指令有效性检查影响。"""
    bid = await bucket_mgr.create(content="普通记忆")
    bucket = await bucket_mgr.get(bid)
    meta = bucket["metadata"]
    assert meta["instruction"] is False
    assert bucket_mgr._is_instruction_active(meta) is False


@pytest.mark.asyncio
async def test_instruction_trigger_condition_saved(bucket_mgr):
    """触发条件字段被正确保存。"""
    bid = await bucket_mgr.create(
        content="用户约定：每次吵架后先冷静再沟通",
        instruction=True,
        active=True,
        trigger_condition="检测到争吵情绪时",
    )
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["trigger_condition"] == "检测到争吵情绪时"