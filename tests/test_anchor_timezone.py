# ============================================================
# Test: Anchor timezone consistency (P0-1)
# 测试：锚点时区一致性（P0-1 修复）
#
# 背景：add_anchor 的 created/updated 用 now_iso()（UTC 带时区），
# 但 expires_at 曾用 datetime.now()（本地时间无时区），导致非 UTC
# 时区下 TTL 计算偏差。修复后 expires_at 统一 UTC，并兼容旧的无时区数据。
# ============================================================

import json
import os
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.asyncio
async def test_add_anchor_expires_at_is_utc(bucket_mgr):
    """add_anchor 生成的 expires_at 必须带 UTC 时区（与 created 一致）。"""
    anchor = await bucket_mgr.add_anchor(
        triggers=["测试"], anchor_type="dynamic", ttl_hours=48
    )
    expires = datetime.fromisoformat(anchor["expires_at"])
    assert expires.tzinfo is not None
    assert expires.utcoffset() == timedelta(0)  # UTC


@pytest.mark.asyncio
async def test_check_expired_legacy_naive_local(bucket_mgr):
    """旧格式（无时区本地时间）的过期锚点应被正确判定为过期。"""
    anchor = {
        "id": "legacy1",
        "anchor_type": "dynamic",
        "is_active": True,
        "expires_at": (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),
    }
    file_path = os.path.join(bucket_mgr.anchor_dir, "legacy1.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(anchor, f, ensure_ascii=False)

    assert bucket_mgr._check_and_deactivate_if_expired(anchor, file_path) is True
    assert anchor["is_active"] is False


@pytest.mark.asyncio
async def test_check_not_expired_legacy_naive_local(bucket_mgr):
    """旧格式（无时区本地时间）的未过期锚点不应被误判。"""
    anchor = {
        "id": "legacy2",
        "anchor_type": "dynamic",
        "is_active": True,
        "expires_at": (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"),
    }
    file_path = os.path.join(bucket_mgr.anchor_dir, "legacy2.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(anchor, f, ensure_ascii=False)

    assert bucket_mgr._check_and_deactivate_if_expired(anchor, file_path) is False
    assert anchor["is_active"] is True