# ============================================================
# Test: Scene field (P2 + 修正 4)
# 测试：场景字段（P2 + 修正 4）
#
# 需求 4：记忆需要区分不同场景（chat/rp/intimate/home）。
# 修正 4：场景隔离拆成两个维度——world_id（main/rp_xxx）+ scene（chat/intimate/home/roleplay）。
#         正常聊天、亲密互动和 Home 属于 main 世界；RP 必须独立 world_id，禁止污染 main。
#         旧记忆默认 main + chat，不默认全场景可见。
# 验证：
#   1. create 默认 scene=["chat"]、world_id="main"
#   2. create 显式 scene=["rp"] 被正确保存
#   3. 非法/空 scene 回退为 ["chat"]
#   4. 旧记忆（无 scene 字段）读取时补默认值 ["chat"]、world_id="main"
#   5. server 场景解析：合法场景保留、非法场景过滤、空返回 None
#   6. server 场景匹配：无过滤全通过、有过滤按交集匹配、旧记忆按 ["chat"] 处理
#   7. breath 轻量检索按 scene 过滤召回
#   8. world_id 双维度：RP 记忆独立 world_id，breath 按 world 过滤隔离
# ============================================================

import os
import sys
import tempfile
from unittest.mock import patch

import pytest

import frontmatter

# --- Redirect buckets dir to a temp location BEFORE importing server ---
# --- 在导入 server 前将记忆目录重定向到临时位置，避免触碰真实数据库 ---
# server.py 模块级会加载 .env（其中 OMBRE_BUCKETS_DIR 指向真实目录），
# 因此导入期间临时屏蔽 .env 文件，确保使用测试临时目录。
_import_tmp = tempfile.mkdtemp(prefix="ombre_scene_test_")
os.environ["OMBRE_BUCKETS_DIR"] = _import_tmp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_real_exists = os.path.exists
with patch(
    "os.path.exists",
    side_effect=lambda p: False if str(p).endswith(".env") else _real_exists(p),
):
    import server  # noqa: E402  (module-level init is redirected to temp dir)


# ---------------------------------------------------------
# 1-4. bucket_manager scene 字段
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_create_default_scene_chat(bucket_mgr):
    bid = await bucket_mgr.create(content="普通聊天记忆")
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["scene"] == ["chat"]
    assert bucket["metadata"]["world_id"] == "main"


@pytest.mark.asyncio
async def test_create_explicit_scene_rp(bucket_mgr):
    bid = await bucket_mgr.create(content="RP 场景记忆", scene=["rp"])
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["scene"] == ["rp"]


@pytest.mark.asyncio
async def test_create_explicit_world_id(bucket_mgr):
    """RP 记忆必须使用独立 world_id，禁止污染 main。"""
    bid = await bucket_mgr.create(content="RP 世界记忆", scene=["roleplay"], world_id="rp_alpha")
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["scene"] == ["roleplay"]
    assert bucket["metadata"]["world_id"] == "rp_alpha"


@pytest.mark.asyncio
async def test_create_invalid_scene_falls_back(bucket_mgr):
    bid = await bucket_mgr.create(content="非法场景", scene=["bogus", "  "])
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["scene"] == ["chat"]


@pytest.mark.asyncio
async def test_legacy_bucket_defaults_scene_chat(bucket_mgr):
    """旧记忆（frontmatter 无 scene 字段）读取时补默认值 ["chat"]、world_id="main"。"""
    bid = await bucket_mgr.create(content="旧记忆")
    fpath = bucket_mgr._find_bucket_file(bid)
    post = frontmatter.load(fpath)
    # 模拟旧数据：删除 scene / world_id 字段
    if "scene" in post:
        del post["scene"]
    if "world_id" in post:
        del post["world_id"]
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))

    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"]["scene"] == ["chat"]
    assert bucket["metadata"]["world_id"] == "main"


# ---------------------------------------------------------
# 5. server 场景解析
# ---------------------------------------------------------
def test_parse_scene_filter_valid():
    assert server._parse_scene_filter("rp,chat") == ["rp", "chat"]
    assert server._parse_scene_filter("RP") == ["rp"]


def test_parse_scene_filter_invalid_and_empty():
    assert server._parse_scene_filter("bogus") is None
    assert server._parse_scene_filter("") is None
    assert server._parse_scene_filter("  ") is None
    assert server._parse_scene_filter("rp,bogus") == ["rp"]


# ---------------------------------------------------------
# 6. server 场景匹配
# ---------------------------------------------------------
def test_bucket_matches_scene_no_filter():
    assert server._bucket_matches_scene({"metadata": {"scene": ["rp"]}}, None) is True
    assert server._bucket_matches_scene({"metadata": {}}, None) is True


def test_bucket_matches_scene_overlap():
    assert server._bucket_matches_scene({"metadata": {"scene": ["rp"]}}, ["rp"]) is True
    assert server._bucket_matches_scene({"metadata": {"scene": ["chat"]}}, ["rp"]) is False
    assert server._bucket_matches_scene({"metadata": {"scene": ["chat", "rp"]}}, ["rp"]) is True


def test_bucket_matches_scene_legacy_defaults_chat():
    # 旧记忆无 scene 字段 → 按默认 ["chat"] 处理
    assert server._bucket_matches_scene({"metadata": {}}, ["chat"]) is True
    assert server._bucket_matches_scene({"metadata": {}}, ["rp"]) is False


def test_bucket_matches_scene_string_scene():
    # 兼容 scene 以字符串存储的脏数据
    assert server._bucket_matches_scene({"metadata": {"scene": "rp"}}, ["rp"]) is True
    assert server._bucket_matches_scene({"metadata": {"scene": "chat"}}, ["rp"]) is False


# ---------------------------------------------------------
# 7. breath 轻量检索按 scene 过滤
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_breath_lightweight_scene_filter():
    # 使用 server 全局 bucket_mgr（与 _breath_lightweight 同一实例/同一目录）
    bid_chat = await server.bucket_mgr.create(content="聊天场景的记忆", scene=["chat"])
    bid_rp = await server.bucket_mgr.create(content="RP 场景的记忆", scene=["rp"])

    result = await server._breath_lightweight(
        query="场景",
        scene_filter=["rp"],
    )
    ids = {r["bucket_id"] for r in result["results"]}
    assert bid_rp in ids
    assert bid_chat not in ids

    # 无过滤时两者都返回
    result_all = await server._breath_lightweight(query="场景")
    ids_all = {r["bucket_id"] for r in result_all["results"]}
    assert bid_chat in ids_all and bid_rp in ids_all


# ---------------------------------------------------------
# 8. world_id 双维度：RP 世界隔离
# ---------------------------------------------------------
def test_parse_world_filter():
    assert server._parse_world_filter("main") == ["main"]
    assert server._parse_world_filter("rp_alpha,rp_beta") == ["rp_alpha", "rp_beta"]
    assert server._parse_world_filter("") is None
    assert server._parse_world_filter("  ") is None


def test_bucket_matches_world():
    assert server._bucket_matches_world({"metadata": {"world_id": "rp_alpha"}}, ["rp_alpha"]) is True
    assert server._bucket_matches_world({"metadata": {"world_id": "rp_alpha"}}, ["main"]) is False
    # 旧记忆无 world_id → 按默认 "main" 处理
    assert server._bucket_matches_world({"metadata": {}}, ["main"]) is True
    assert server._bucket_matches_world({"metadata": {}}, ["rp_alpha"]) is False
    # 无过滤时全部通过
    assert server._bucket_matches_world({"metadata": {"world_id": "rp_alpha"}}, None) is True


@pytest.mark.asyncio
async def test_breath_lightweight_world_filter():
    """RP 记忆独立 world_id，breath 按 world 过滤隔离，不污染 main。"""
    bid_main = await server.bucket_mgr.create(content="主世界记忆", scene=["chat"], world_id="main")
    bid_rp = await server.bucket_mgr.create(content="RP 世界记忆", scene=["roleplay"], world_id="rp_alpha")

    # 只查 main 世界 → 不返回 RP 记忆
    result_main = await server._breath_lightweight(query="世界", world_filter=["main"])
    ids_main = {r["bucket_id"] for r in result_main["results"]}
    assert bid_main in ids_main
    assert bid_rp not in ids_main

    # 只查 rp_alpha 世界 → 不返回 main 记忆
    result_rp = await server._breath_lightweight(query="世界", world_filter=["rp_alpha"])
    ids_rp = {r["bucket_id"] for r in result_rp["results"]}
    assert bid_rp in ids_rp
    assert bid_main not in ids_rp