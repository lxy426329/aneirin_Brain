# ============================================================
# Test: Memory class (修正 5)
# 测试：记忆类别（修正 5）
#
# 修正 5：本轮不要破坏性替换现有 type 体系。
#         新增 memory_class 字段（event/experience/person/boundary/plan/principle）。
#         现有 type 字段继续保留用于兼容旧逻辑，先建立映射层。
# 验证：
#   1. create 显式 memory_class 被正确保存
#   2. 非法 memory_class 回退为 None（不强行推断）
#   3. 旧记忆（无 memory_class）读取时为 None
#   4. 现有 type 字段不受影响（兼容旧逻辑）
#   5. memory_class 与 type 可同时存在（加法式兼容）
# ============================================================

import pytest

import frontmatter


@pytest.mark.asyncio
async def test_create_explicit_memory_class(bucket_mgr):
    bid = await bucket_mgr.create(content="一次旅行事件", memory_class="event")
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["memory_class"] == "event"


@pytest.mark.asyncio
async def test_create_invalid_memory_class_none(bucket_mgr):
    """非法 memory_class 回退 None，不强行推断。"""
    bid = await bucket_mgr.create(content="非法类别", memory_class="bogus")
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["memory_class"] is None


@pytest.mark.asyncio
async def test_legacy_bucket_memory_class_none(bucket_mgr):
    """旧记忆（无 memory_class 字段）读取时为 None。"""
    bid = await bucket_mgr.create(content="旧记忆")
    fpath = bucket_mgr._find_bucket_file(bid)
    post = frontmatter.load(fpath)
    if "memory_class" in post:
        del post["memory_class"]
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))

    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["memory_class"] is None


@pytest.mark.asyncio
async def test_type_preserved_with_memory_class(bucket_mgr):
    """memory_class 与现有 type 可同时存在，type 不受影响（加法式兼容）。"""
    bid = await bucket_mgr.create(
        content="用户边界：不接受被欺骗",
        bucket_type="boundary",
        memory_class="boundary",
    )
    bucket = await bucket_mgr.get(bid)
    meta = bucket["metadata"]
    # 现有 type 体系继续工作
    assert meta["type"] == "boundary"
    # 新增 memory_class 维度并存
    assert meta["memory_class"] == "boundary"


@pytest.mark.asyncio
async def test_memory_class_vocabulary(bucket_mgr):
    """各 memory_class 枚举值均被正确保存。"""
    for cls in ("experience", "person", "plan", "principle"):
        bid = await bucket_mgr.create(content=f"类别 {cls}", memory_class=cls)
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["memory_class"] == cls