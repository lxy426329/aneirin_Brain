# ============================================================
# Test: Unified type vocabulary (P1-2)
# 测试：统一类型词表（P1-2）
#
# 需求 3：事件/经验/人物/边界/计划/永久原则 职责明确。
# 调用方可使用新枚举，存储层映射到现有类型（不破坏目录/衰减/检索）。
# 验证：
#   1. experience → pattern
#   2. principle → permanent
#   3. person → identity
#   4. plan → dynamic
#   5. 未知类型保持原样
# ============================================================

import pytest


@pytest.mark.asyncio
async def test_type_experience_maps_to_pattern(bucket_mgr):
    bid = await bucket_mgr.create(content="经验记忆", bucket_type="experience")
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["type"] == "pattern"


@pytest.mark.asyncio
async def test_type_principle_maps_to_permanent(bucket_mgr):
    bid = await bucket_mgr.create(content="永久原则", bucket_type="principle")
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["type"] == "permanent"


@pytest.mark.asyncio
async def test_type_person_maps_to_identity(bucket_mgr):
    bid = await bucket_mgr.create(content="人物档案", bucket_type="person")
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["type"] == "identity"


@pytest.mark.asyncio
async def test_type_plan_maps_to_dynamic(bucket_mgr):
    bid = await bucket_mgr.create(content="计划", bucket_type="plan", task_flag=True)
    bucket = await bucket_mgr.get(bid)
    # plan → dynamic 存储，读取时 dynamic 归一为 event（既有行为）
    assert bucket["metadata"]["type"] == "event"
    assert bucket["metadata"]["task_flag"] is True


@pytest.mark.asyncio
async def test_unknown_type_preserved(bucket_mgr):
    bid = await bucket_mgr.create(content="未知类型", bucket_type="custom_layer")
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["type"] == "custom_layer"