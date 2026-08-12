# ============================================================
# Test: Daily Review — 主 AI 触发的结构化日终整理
#
# Verifies:
#   1. run_daily_review 返回"一包结果"结构（mode/review_date/
#      summary_guidance/candidates/execution_instructions/health/scan_summary）
#   2. 过期任务候选：task_flag + 正文含已过日期（简单可解释规则）
#   3. 长期低价值候选：30天未访问 + 低重要度 + 低激活
#   4. 重复记忆候选：内容相似度 ≥88（保守，仅提示合并方向）
#   5. 冲突候选：复用现有规则，不自动提交回音壁提案
#   6. 回音壁记录本次日终扫描报告（daily_review digest）
#   7. 管家默认不写日记草稿（今日总结由主 AI 写出）
#   8. auto_schedule 默认关闭：后台调度不自动跑日终
#   9. run_daily_review(run_pipeline=True) 兼容旧管线副作用
# ============================================================

import os
import json
import pytest
from datetime import datetime, timedelta, timezone

import frontmatter


@pytest.fixture
def identity_mgr(test_config):
    from identity_manager import IdentityManager
    return IdentityManager(test_config)


@pytest.fixture
def housekeeper(test_config, bucket_mgr, identity_mgr):
    from housekeeper import Housekeeper
    return Housekeeper(test_config, bucket_mgr, None, identity_mgr=identity_mgr)


async def _patch_created(bucket_mgr, bucket_id, days_ago):
    """Patch a bucket's created/last_accessed timestamp to simulate age."""
    fpath = bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(fpath)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    post["created"] = old_ts
    post["last_accessed"] = old_ts
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))


# ---------------------------------------------------------
# 1. 一包结果结构
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_review_package_structure(housekeeper):
    result = await housekeeper.run_daily_review(record_report=False)

    assert result["mode"] == "daily_review"
    assert result["review_date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert isinstance(result["candidates"], list)
    assert isinstance(result["summary_guidance"], dict)
    assert "message" in result["summary_guidance"]
    assert "suggested_fields" in result["summary_guidance"]
    assert "hold_template" in result["summary_guidance"]
    assert "journal_status" in result["summary_guidance"]
    assert isinstance(result["execution_instructions"], str)
    assert "trace" in result["execution_instructions"]  # 主AI决策指引
    assert set(result["health"]) == {"errors", "warnings"}
    assert result["health"]["errors"] == []
    assert result["health"]["warnings"] == []  # 回归保护：journal_status 读取不得产生警告
    assert "scanned_buckets" in result["scan_summary"]
    assert "candidates_by_category" in result["scan_summary"]


# ---------------------------------------------------------
# 2. 过期任务候选
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_review_expired_task(housekeeper, bucket_mgr):
    past = (datetime.now(timezone.utc) - timedelta(days=10)).date()
    future = (datetime.now(timezone.utc) + timedelta(days=5)).date()

    expired_id = await bucket_mgr.create(
        content=f"{past.isoformat()} 去银行办卡",
        task_flag=True,
    )
    future_id = await bucket_mgr.create(
        content=f"{future.isoformat()} 交房租",
        task_flag=True,
    )
    # 非任务型记忆即使含过去日期也不应被标记
    normal_id = await bucket_mgr.create(
        content=f"{past.isoformat()} 那天去爬山了",
        task_flag=False,
    )

    result = await housekeeper.run_daily_review(record_report=False)
    expired = [c for c in result["candidates"] if c["category"] == "expired_task"]

    assert len(expired) == 1
    assert expired[0]["bucket_id"] == expired_id
    assert "已过去" in expired[0]["reason"]
    assert "mark_resolved" in expired[0]["options"]
    assert expired[0]["default_suggestion"] == "mark_resolved"

    flagged_ids = {c["bucket_id"] for c in expired}
    assert future_id not in flagged_ids
    assert normal_id not in flagged_ids


# ---------------------------------------------------------
# 3. 长期低价值候选
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_review_stale(housekeeper, bucket_mgr):
    stale_id = await bucket_mgr.create(content="很久以前的琐碎小事", importance=2)
    await _patch_created(bucket_mgr, stale_id, days_ago=40)

    fresh_id = await bucket_mgr.create(content="最近的重要记忆", importance=8)
    await _patch_created(bucket_mgr, fresh_id, days_ago=2)

    result = await housekeeper.run_daily_review(record_report=False)
    stale = [c for c in result["candidates"] if c["category"] == "stale"]

    assert len(stale) == 1
    assert stale[0]["bucket_id"] == stale_id
    assert "天未访问" in stale[0]["reason"]
    assert "raise_importance" in stale[0]["options"]
    assert stale[0]["default_suggestion"] in ("keep", "archive_or_sink")
    assert fresh_id not in {c["bucket_id"] for c in stale}


# ---------------------------------------------------------
# 4. 重复记忆候选（保守：相似度≥88）
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_review_duplicate(housekeeper, bucket_mgr):
    old_id = await bucket_mgr.create(content="我特别喜欢喝全糖奶茶，每天一杯")
    await _patch_created(bucket_mgr, old_id, days_ago=5)
    new_id = await bucket_mgr.create(content="我特别喜欢喝全糖奶茶，每天一杯")

    result = await housekeeper.run_daily_review(record_report=False)
    dup = [c for c in result["candidates"] if c["category"] == "duplicate"]

    assert len(dup) >= 1
    c = dup[0]
    assert c["bucket_id"] == new_id
    assert c["detail"]["duplicate_of"] == old_id
    assert c["detail"]["similarity"] >= 88
    assert f"merge_with:{old_id}" in c["options"]
    assert c["default_suggestion"] == f"merge_with:{old_id}"


# ---------------------------------------------------------
# 5. 冲突候选（不自动提交回音壁提案）
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_review_conflict_no_auto_proposal(housekeeper, bucket_mgr):
    old_id = await bucket_mgr.create(content="我以前不喜欢吃辣", importance=5)
    await _patch_created(bucket_mgr, old_id, days_ago=3)
    new_id = await bucket_mgr.create(content="我今天特别想吃辣", importance=5)

    result = await housekeeper.run_daily_review(record_report=False)
    conflicts = [c for c in result["candidates"] if c["category"] == "conflict"]

    assert len(conflicts) >= 1
    c = conflicts[0]
    assert c["detail"]["old_bucket_id"] == old_id
    assert c["detail"]["new_bucket_id"] == new_id
    assert c["detail"]["conflict_type"]
    assert "mark_old_resolved" in c["options"]

    # 不自动向回音壁提交 conflict 提案（最终决策权在主 AI）
    actions = await housekeeper.echo_chamber.get_pending_actions(action_type="conflict")
    assert len(actions) == 0


# ---------------------------------------------------------
# 6. 回音壁记录日终扫描报告
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_review_writes_echo_report(housekeeper):
    result = await housekeeper.run_daily_review(record_report=True)

    digests = await housekeeper.echo_chamber.get_pending_digests(digest_type="daily_review")
    assert len(digests) == 1
    d = digests[0]
    assert d["digest_type"] == "daily_review"
    assert d["metadata"]["review_date"] == result["review_date"]
    assert d["metadata"]["candidates_total"] == result["scan_summary"]["candidates_total"]

    # record_report=False 时不写报告
    await housekeeper.run_daily_review(record_report=False)
    assert len(await housekeeper.echo_chamber.get_pending_digests(digest_type="daily_review")) == 1


# ---------------------------------------------------------
# 7. 管家默认不写日记草稿（今日总结由主 AI 写出）
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_review_no_auto_journal_draft(housekeeper, bucket_mgr):
    await bucket_mgr.create(content="今天测试日记草稿不自动生成")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    result = await housekeeper.run_daily_review(record_report=False)
    assert result["summary_guidance"]["journal_status"]["exists"] is False
    assert housekeeper.journal_mgr.get_entry(today) is None

    # _daily_summary 同样不写草稿（auto_journal_draft 默认 False）
    await housekeeper._daily_summary()
    assert housekeeper.journal_mgr.get_entry(today) is None


# ---------------------------------------------------------
# 8. auto_schedule 默认关闭：后台调度不自动跑日终
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_auto_schedule_default_off(housekeeper):
    assert housekeeper.auto_schedule is False
    # _check_schedule 在 auto_schedule=False 时不做任何事（不触发日终）
    await housekeeper._check_schedule()
    assert housekeeper._last_daily_run is None


# ---------------------------------------------------------
# 9. run_pipeline=True 兼容旧管线副作用
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_review_with_pipeline(housekeeper, bucket_mgr):
    await bucket_mgr.create(content="今天发生了一件小事：去公园散步")

    result = await housekeeper.run_daily_review(record_report=False, run_pipeline=True)
    assert "pipeline" in result
    for task in ("daily_summary", "chain_updates", "conflicts", "identity_detection"):
        assert task in result["pipeline"]
