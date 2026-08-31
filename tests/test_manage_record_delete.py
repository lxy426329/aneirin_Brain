# ============================================================
# Test: manage_record delete must go through proposal (修正 6)
# 测试：manage_record 删除必须经过提案审批（修正 6）
#
# 修正 6：禁止任何管家或 ai_manage 路径绕过 proposal 直接删除/修改长期记忆。
# 验证：
#   1. manage_record(delete, bucket) 生成提案，不直接删除
#   2. 提案批准后桶才被真正删除
# ============================================================

import os
import sys
import tempfile
from unittest.mock import patch

import pytest

# --- Redirect buckets dir to a temp location BEFORE importing server ---
# --- 在导入 server 前将记忆目录重定向到临时位置，避免触碰真实数据库 ---
_import_tmp = tempfile.mkdtemp(prefix="ombre_manage_delete_test_")
os.environ["OMBRE_BUCKETS_DIR"] = _import_tmp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_real_exists = os.path.exists
with patch(
    "os.path.exists",
    side_effect=lambda p: False if str(p).endswith(".env") else _real_exists(p),
):
    import server  # noqa: E402  (module-level init is redirected to temp dir)


@pytest.mark.asyncio
async def test_manage_record_delete_generates_proposal():
    """删除记忆桶必须先生成提案，不直接删除。"""
    bid = await server.bucket_mgr.create(content="待删除记忆", importance=3)

    result = await server.manage_record(action="delete", record_type="bucket", record_id=bid)

    # 返回提案 ID 提示，而非"已删除"
    assert "提案" in result
    assert "已删除" not in result

    # 桶仍然存在（未被直接删除）
    assert await server.bucket_mgr.get(bid) is not None

    # 提案文件已生成
    pending_dir = server.housekeeper.echo_chamber.pending_actions_dir
    proposal_files = [f for f in os.listdir(pending_dir) if f.endswith(".json")]
    assert len(proposal_files) == 1


@pytest.mark.asyncio
async def test_manage_record_delete_after_approval():
    """提案批准后桶才被真正删除。"""
    bid = await server.bucket_mgr.create(content="待删除记忆2", importance=3)

    result = await server.manage_record(action="delete", record_type="bucket", record_id=bid)
    # 提取提案 ID
    import re
    m = re.search(r"提案 (\w+)", result)
    assert m
    proposal_id = m.group(1)

    # 批准提案 → 桶被删除
    ok = await server.housekeeper.approve_action(proposal_id, approved_by="user")
    assert ok
    assert await server.bucket_mgr.get(bid) is None