# ============================================================
# Test: Current-state expiry (修正 3)
# 测试：当前状态过期机制（修正 3）
#
# 修正 3：state / is_current 必须有过期机制。
#         所有"当前状态"必须记录 observed_at，并拥有 expires_at 或 TTL。
#         状态过期后自动降级为历史背景，不删除原记录。
#         禁止永久保存 is_current=True 而没有时间约束。
# 验证：
#   1. is_current=True 自动补 observed_at 与默认 TTL（state_expires_at）
#   2. 显式提供 expires_at 时 state_expires_at 使用该值
#   3. 已过期的当前状态读取时自动降级 is_current=False（原记录保留）
#   4. is_current=False 时 observed_at/state_expires_at 为 None
# ============================================================

from datetime import datetime, timedelta, timezone

import pytest

import frontmatter

from bucket_manager import DEFAULT_STATE_TTL_DAYS


@pytest.mark.asyncio
async def test_is_current_auto_ttl(bucket_mgr):
    """is_current=True 自动补 observed_at 与默认 TTL，禁止永久保存无时间约束。"""
    bid = await bucket_mgr.create(content="用户当前状态：正在备考", is_current=True)
    bucket = await bucket_mgr.get(bid)
    meta = bucket["metadata"]
    assert meta["is_current"] is True
    assert meta["observed_at"] is not None
    assert meta["state_expires_at"] is not None
    # 默认 TTL 应为 DEFAULT_STATE_TTL_DAYS 天
    expiry = datetime.fromisoformat(meta["state_expires_at"])
    observed = datetime.fromisoformat(meta["observed_at"])
    assert (expiry - observed).days == DEFAULT_STATE_TTL_DAYS


@pytest.mark.asyncio
async def test_is_current_explicit_expiry(bucket_mgr):
    """显式提供 expires_at 时，state_expires_at 使用该值。"""
    bid = await bucket_mgr.create(
        content="用户当前状态：出差中",
        is_current=True,
        expires_at="2099-12-31T23:59:59+00:00",
    )
    bucket = await bucket_mgr.get(bid)
    meta = bucket["metadata"]
    assert meta["is_current"] is True
    assert meta["state_expires_at"] == "2099-12-31T23:59:59+00:00"


@pytest.mark.asyncio
async def test_is_current_expired_downgrades(bucket_mgr):
    """已过期的当前状态读取时自动降级 is_current=False，原记录不删除。"""
    bid = await bucket_mgr.create(content="用户当前状态：感冒中", is_current=True)
    fpath = bucket_mgr._find_bucket_file(bid)
    post = frontmatter.load(fpath)
    # 模拟状态已过期：把 state_expires_at 改为过去时间
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    post["state_expires_at"] = past
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))

    bucket = await bucket_mgr.get(bid)
    meta = bucket["metadata"]
    # 自动降级为历史背景，不删除原记录
    assert meta["is_current"] is False
    assert bucket is not None


@pytest.mark.asyncio
async def test_not_current_no_expiry_fields(bucket_mgr):
    """is_current=False 时 observed_at/state_expires_at 为 None。"""
    bid = await bucket_mgr.create(content="普通记忆")
    bucket = await bucket_mgr.get(bid)
    meta = bucket["metadata"]
    assert meta["is_current"] is False
    assert meta["observed_at"] is None
    assert meta["state_expires_at"] is None