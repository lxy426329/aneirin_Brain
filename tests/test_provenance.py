# ============================================================
# Test: Provenance field (P1-1 + 修正 1)
# 测试：记忆来源字段（P1-1 + 修正 1）
#
# 需求 5：必须区分"用户明确说过的内容"和"AI 自己推测出的内容"。
# 修正 1：旧记忆绝不能默认 provenance="user_explicit"；
#         无法确认来源的旧记忆统一标记 legacy；
#         只有新写入且能明确确认来源时才标记 user_explicit。
# 验证：
#   1. create 默认 provenance="user_explicit"（新写入默认用户明确）
#   2. create 显式 provenance="ai_inferred" 被正确保存
#   3. 非法 provenance 值回退为 "legacy"（来源不明，绝不默认 user_explicit）
#   4. 旧记忆（无 provenance 字段）读取时补默认值 "legacy"
#   5. 各来源枚举值（ai_observed/system_event/imported）被正确保存
# ============================================================

import pytest

import frontmatter


@pytest.mark.asyncio
async def test_create_default_provenance_user_explicit(bucket_mgr):
    """新写入且能明确确认来源（调用方默认）→ user_explicit。"""
    bid = await bucket_mgr.create(content="用户明确说的内容")
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["provenance"] == "user_explicit"


@pytest.mark.asyncio
async def test_create_explicit_provenance_ai_inferred(bucket_mgr):
    bid = await bucket_mgr.create(content="AI 推测的内容", provenance="ai_inferred")
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["provenance"] == "ai_inferred"


@pytest.mark.asyncio
async def test_create_invalid_provenance_falls_back_legacy(bucket_mgr):
    """非法来源值回退 legacy，绝不默认 user_explicit。"""
    bid = await bucket_mgr.create(content="非法来源", provenance="hacker")
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["provenance"] == "legacy"


@pytest.mark.asyncio
async def test_legacy_bucket_defaults_provenance_legacy(bucket_mgr):
    """旧记忆（frontmatter 无 provenance 字段）读取时补默认值 legacy。"""
    bid = await bucket_mgr.create(content="旧记忆")
    fpath = bucket_mgr._find_bucket_file(bid)
    post = frontmatter.load(fpath)
    # 模拟旧数据：删除 provenance 字段
    if "provenance" in post:
        del post["provenance"]
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))

    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["provenance"] == "legacy"


@pytest.mark.asyncio
async def test_provenance_vocabulary_values(bucket_mgr):
    """各来源枚举值均被正确保存。"""
    for prov in ("ai_observed", "system_event", "imported"):
        bid = await bucket_mgr.create(content=f"来源 {prov}", provenance=prov)
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["provenance"] == prov