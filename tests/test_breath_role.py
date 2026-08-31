# ============================================================
# Test: Breath context role labeling (上下文角色显式化)
# 测试：breath 上下文角色显式化
#
# 每条记忆在 breath 输出中显式标注其在当前上下文中的角色，
# 让主模型明确区分：当前有效指令 / 当前状态 / 边界 / 身份 / 背景等。
# 验证：
#   1. _memory_role：当前有效指令 → "指令"
#   2. _memory_role：is_current → "当前状态"
#   3. _memory_role：type=boundary → "边界"；type=identity → "身份"
#   4. _memory_role：未知类型 → "背景"
#   5. _breath_lightweight 每条结果带 role 字段
# ============================================================

import os
import sys
import tempfile
from unittest.mock import patch

import pytest

# --- Redirect buckets dir to a temp location BEFORE importing server ---
# --- 在导入 server 前将记忆目录重定向到临时位置，避免触碰真实数据库 ---
_import_tmp = tempfile.mkdtemp(prefix="ombre_breath_role_test_")
os.environ["OMBRE_BUCKETS_DIR"] = _import_tmp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_real_exists = os.path.exists
with patch(
    "os.path.exists",
    side_effect=lambda p: False if str(p).endswith(".env") else _real_exists(p),
):
    import server  # noqa: E402


# ---------------------------------------------------------
# 1-4. _memory_role 推断
# ---------------------------------------------------------
def test_role_instruction():
    assert server._memory_role({"instruction": True, "active": True}) == "指令"


def test_role_current_state():
    assert server._memory_role({"is_current": True}) == "当前状态"


def test_role_instruction_beats_current_state():
    """当前有效指令优先于当前状态。"""
    assert server._memory_role({"instruction": True, "active": True, "is_current": True}) == "指令"


def test_role_by_type():
    assert server._memory_role({"type": "boundary"}) == "边界"
    assert server._memory_role({"type": "identity"}) == "身份"
    assert server._memory_role({"type": "pattern"}) == "行为模式"
    assert server._memory_role({"type": "permanent"}) == "永久原则"


def test_role_default_background():
    assert server._memory_role({"type": "unknown_type"}) == "背景"
    assert server._memory_role({}) == "背景"
    assert server._memory_role(None) == "背景"


# ---------------------------------------------------------
# 5. _breath_lightweight 每条结果带 role 字段
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_breath_lightweight_has_role():
    bid = await server.bucket_mgr.create(
        content="用户边界：不接受被欺骗",
        bucket_type="boundary",
        provenance="user_explicit",
    )
    result = await server._breath_lightweight(query="边界")
    assert result["results"], "应至少召回一条记忆"
    for r in result["results"]:
        assert "role" in r
    # 边界记忆的角色应为"边界"
    target = [r for r in result["results"] if r["bucket_id"] == bid]
    assert target and target[0]["role"] == "边界"