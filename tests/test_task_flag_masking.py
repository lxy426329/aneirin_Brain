# ============================================================
# Test: task_flag auto-masking in vulnerable states
# 测试：脆弱状态下 task_flag 自动屏蔽机制
#
# Verifies:
#   1. bucket_manager.create() stores task_flag correctly
#   2. _mask_task_buckets() filters task_flag=True buckets
#   3. detect_vulnerable_state() detects sick/tired/emotional signals
#   4. retrieve_strong_anchors(mask_tasks=True) filters task buckets
#   5. search(mask_tasks=True) filters task buckets
# ============================================================

import pytest

from utils import detect_vulnerable_state


# -----------------------------------------------------------
# 1. Vulnerable state detection / 脆弱状态检测
# -----------------------------------------------------------

@pytest.mark.parametrize("text,expected_state", [
    ("我发烧了，全身没力气", "sick"),
    ("今天头痛得厉害", "sick"),
    ("胃痛，吃不下东西", "sick"),
    ("I have a fever and feel sick", "sick"),
    ("今天好累啊，什么都不想做", "tired"),
    ("exhausted, can't move", "tired"),
    ("失眠到天亮，太累了", "tired"),
    ("最近好难过，想哭", "emotional"),
    ("焦虑到喘不过气", "emotional"),
    ("I feel so sad and empty", "emotional"),
])
def test_detect_vulnerable_state_triggers(text, expected_state):
    """Vulnerable state keywords should trigger correct state."""
    result = detect_vulnerable_state(text)
    assert result["is_vulnerable"] is True, f"Should detect vulnerability in: {text}"
    assert result["state"] == expected_state, (
        f"Wrong state for '{text}': expected {expected_state}, got {result['state']}"
    )
    assert len(result["matched_keywords"]) > 0


@pytest.mark.parametrize("text", [
    ("今天天气不错"),
    ("我们去吃饭吧"),
    ("Let's work on the project"),
    ("刚看完一本好书"),
    (""),
])
def test_detect_vulnerable_state_no_trigger(text):
    """Non-vulnerable text should not trigger."""
    result = detect_vulnerable_state(text)
    assert result["is_vulnerable"] is False
    assert result["state"] == "normal"


def test_detect_vulnerable_state_priority_sick_over_emotional():
    """Sick state takes priority over emotional."""
    text = "我发烧了，真的好难过想哭"
    result = detect_vulnerable_state(text)
    assert result["state"] == "sick"


# -----------------------------------------------------------
# 2. bucket_manager: task_flag storage / 桶存储 task_flag
# -----------------------------------------------------------

@pytest.mark.asyncio
async def test_create_stores_task_flag(bucket_mgr):
    """create() should persist task_flag in frontmatter."""
    bid = await bucket_mgr.create(
        content="明天需要完成项目报告",
        task_flag=True,
        tags=["工作"],
        domain=["工作"],
    )
    bucket = await bucket_mgr.get(bid)
    assert bucket is not None
    assert bucket["metadata"].get("task_flag") is True


@pytest.mark.asyncio
async def test_create_default_task_flag_false(bucket_mgr):
    """Buckets created without task_flag should default to False."""
    bid = await bucket_mgr.create(
        content="今天和顾尘聊了一个很有意思的话题",
        tags=["聊天"],
        domain=["生活"],
    )
    bucket = await bucket_mgr.get(bid)
    assert bucket is not None
    assert bucket["metadata"].get("task_flag") is False


@pytest.mark.asyncio
async def test_update_task_flag(bucket_mgr):
    """update() should be able to toggle task_flag."""
    bid = await bucket_mgr.create(
        content="待办事项：写文档",
        domain=["工作"],
    )
    assert (await bucket_mgr.get(bid))["metadata"].get("task_flag") is False

    # Set to True
    await bucket_mgr.update(bid, task_flag=True)
    assert (await bucket_mgr.get(bid))["metadata"].get("task_flag") is True

    # Set back to False
    await bucket_mgr.update(bid, task_flag=False)
    assert (await bucket_mgr.get(bid))["metadata"].get("task_flag") is False


# -----------------------------------------------------------
# 3. _mask_task_buckets: filter helper / 屏蔽辅助函数
# -----------------------------------------------------------

@pytest.mark.asyncio
async def test_mask_task_buckets_filters(bucket_mgr):
    """_mask_task_buckets should remove only task_flag=True buckets."""
    task_bid = await bucket_mgr.create(
        content="待办：写项目报告",
        task_flag=True,
        domain=["工作"],
    )
    normal_bid = await bucket_mgr.create(
        content="今天的对话让我很开心",
        task_flag=False,
        domain=["生活"],
    )

    all_buckets = await bucket_mgr.list_all(include_archive=False)
    assert len(all_buckets) == 2

    filtered = bucket_mgr._mask_task_buckets(all_buckets)
    filtered_ids = {b["id"] for b in filtered}

    assert normal_bid in filtered_ids, "Normal bucket should pass through"
    assert task_bid not in filtered_ids, "Task bucket should be masked"


@pytest.mark.asyncio
async def test_mask_task_buckets_all_task(bucket_mgr):
    """_mask_task_buckets should return empty list when all buckets are tasks."""
    await bucket_mgr.create(content="任务1", task_flag=True, domain=["工作"])
    await bucket_mgr.create(content="任务2", task_flag=True, domain=["工作"])

    all_buckets = await bucket_mgr.list_all(include_archive=False)
    filtered = bucket_mgr._mask_task_buckets(all_buckets)
    assert len(filtered) == 0


@pytest.mark.asyncio
async def test_mask_task_buckets_no_task(bucket_mgr):
    """_mask_task_buckets should return all when no task_flag buckets."""
    await bucket_mgr.create(content="记忆1", domain=["生活"])
    await bucket_mgr.create(content="记忆2", domain=["工作"])

    all_buckets = await bucket_mgr.list_all(include_archive=False)
    filtered = bucket_mgr._mask_task_buckets(all_buckets)
    assert len(filtered) == len(all_buckets)


# -----------------------------------------------------------
# 4. Three-step pipeline: mask_tasks propagation / 三步检索屏蔽
# -----------------------------------------------------------

@pytest.mark.asyncio
async def test_retrieve_strong_anchors_masks_tasks(bucket_mgr):
    """retrieve_strong_anchors(mask_tasks=True) should filter task_flag pinned buckets."""
    # Pinned task bucket (rare, but possible)
    await bucket_mgr.create(
        content="必须完成的KPI任务",
        pinned=True,
        task_flag=True,
        domain=["工作"],
    )
    # Pinned normal bucket
    normal_pinned = await bucket_mgr.create(
        content="核心准则：永远陪伴顾尘",
        pinned=True,
        domain=["关系"],
    )

    # Without mask: should return both pinned buckets
    result_no_mask = await bucket_mgr.retrieve_strong_anchors(query="")
    ids_no_mask = {b["id"] for b in result_no_mask}
    assert normal_pinned in ids_no_mask

    # With mask: should filter out the task pinned bucket
    result_masked = await bucket_mgr.retrieve_strong_anchors(query="", mask_tasks=True)
    ids_masked = {b["id"] for b in result_masked}
    assert normal_pinned in ids_masked, "Normal pinned should survive masking"
    assert all(
        not b["metadata"].get("task_flag", False) for b in result_masked
    ), "No task_flag bucket should be in masked result"


@pytest.mark.asyncio
async def test_search_masks_tasks(bucket_mgr):
    """search(mask_tasks=True) should filter task_flag buckets."""
    await bucket_mgr.create(
        content="项目报告需要完成",
        task_flag=True,
        domain=["工作"],
        tags=["项目", "报告"],
    )
    normal_bid = await bucket_mgr.create(
        content="项目讨论很有趣",
        task_flag=False,
        domain=["工作"],
        tags=["项目", "讨论"],
    )

    # Without mask: should find both
    result_no_mask = await bucket_mgr.search("项目", limit=10)
    assert len(result_no_mask) >= 1

    # With mask: task bucket should be gone
    result_masked = await bucket_mgr.search("项目", limit=10, mask_tasks=True)
    masked_ids = {b["id"] for b in result_masked}
    assert normal_bid in masked_ids, "Normal bucket should be in masked results"
    assert all(
        not b["metadata"].get("task_flag", False) for b in result_masked
    ), "No task_flag bucket should be in masked results"


@pytest.mark.asyncio
async def test_retrieve_hybrid_buckets_masks_tasks(bucket_mgr):
    """retrieve_hybrid_buckets(mask_tasks=True) should filter task_flag buckets."""
    await bucket_mgr.create(
        content="完成项目报告的任务",
        task_flag=True,
        domain=["工作"],
        tags=["项目"],
    )
    normal_bid = await bucket_mgr.create(
        content="关于项目的讨论",
        task_flag=False,
        domain=["工作"],
        tags=["项目"],
    )

    full, summary = await bucket_mgr.retrieve_hybrid_buckets(
        "项目", top_n=10, mask_tasks=True
    )
    all_returned = full + summary
    all_ids = {b["id"] for b in all_returned}

    assert normal_bid in all_ids, "Normal bucket should be returned"
    assert all(
        not b["metadata"].get("task_flag", False) for b in all_returned
    ), "No task_flag bucket should be returned when mask_tasks=True"


# -----------------------------------------------------------
# 5. End-to-end: vulnerable state + task masking / 端到端
# -----------------------------------------------------------

def test_end_to_end_vulnerable_state_masks_tasks():
    """
    End-to-end: a sick/tired/emotional user message should
    trigger vulnerable state, which in breath() would set mask_tasks=True.
    """
    sick_message = "我发烧了，全身酸痛，能别催我做任务了吗"
    tired_message = "心力交瘁，累死了"
    emotional_message = "难过到想哭"

    for msg in (sick_message, tired_message, emotional_message):
        state = detect_vulnerable_state(msg)
        assert state["is_vulnerable"] is True, f"Should detect vulnerability: {msg}"
        # In breath(), this would set mask_tasks=True, filtering task buckets
        mask_tasks = state["is_vulnerable"]
        assert mask_tasks is True


def test_normal_message_does_not_trigger_masking():
    """A normal message should NOT trigger task masking."""
    normal_messages = [
        "今天的天气真好",
        "我们聊聊项目进度吧",
        "刚看完一本很有意思的书",
    ]
    for msg in normal_messages:
        state = detect_vulnerable_state(msg)
        assert state["is_vulnerable"] is False, (
            f"Normal message should not trigger masking: {msg}"
        )
