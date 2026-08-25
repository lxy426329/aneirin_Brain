# ============================================================
# Test: Event Chain Graph & Journal Idempotency
# 测试：事件链实体图谱交叉索引 + 日记幂等更新
#
# Upgrade 1 — EventChain 图谱关联：
#   1. to_dict/from_dict 往返保持 entities / related_chain_ids
#   2. 文本实体提取（人物正则）
#   3. 记忆桶实体提取（标签 + wikilink）
#   4. _rebuild_chain_graph 构建双向关联（共享实体、去重、无交集不关联）
#   5. _append_temporary_node 自动更新链实体
#   6. _daily_chain_update 触发图谱重建
#
# Upgrade 2 — 日记接口：
#   7. complete/create_entry 幂等合并（不覆盖为空、不冲突）
#   8. emotion_tags 归一化（list/中文逗号/顿号/去重）
#   9. date 归一化（非法日期降级为当前 UTC 日期、替代格式解析）
#  10. journals/ 与 buckets/ 完全隔离（不污染 breath/list_all）
# ============================================================

import os
import json
import pytest
from datetime import datetime, timezone

import frontmatter


# ---------------------------------------------------------
# Local fixtures
# ---------------------------------------------------------
@pytest.fixture
def identity_mgr(test_config):
    from identity_manager import IdentityManager
    return IdentityManager(test_config)


@pytest.fixture
def housekeeper(test_config, bucket_mgr, identity_mgr):
    from housekeeper import Housekeeper
    return Housekeeper(test_config, bucket_mgr, None, identity_mgr=identity_mgr)


@pytest.fixture
def journal_mgr(test_config):
    from journal_manager import JournalManager
    # Mirror production: journals live as sibling of buckets/ (strict isolation)
    # 与生产一致：journals 为 buckets/ 的兄弟目录（严格隔离）
    return JournalManager(os.path.dirname(test_config["buckets_dir"]))


# ---------------------------------------------------------
# 1. EventChain 序列化往返
# ---------------------------------------------------------
def test_event_chain_roundtrip_preserves_graph_fields():
    from housekeeper import EventChain
    chain = EventChain("c1", "项目开发")
    chain.entities = ["张三", "项目A"]
    chain.related_chain_ids = ["c2", "c3"]

    data = chain.to_dict()
    restored = EventChain.from_dict(data)

    assert restored.entities == ["张三", "项目A"]
    assert restored.related_chain_ids == ["c2", "c3"]

    # 旧数据（无新字段）也不报错
    legacy = EventChain.from_dict({"chain_id": "c9", "topic": "旧链"})
    assert legacy.entities == []
    assert legacy.related_chain_ids == []


# ---------------------------------------------------------
# 2. 文本实体提取
# ---------------------------------------------------------
def test_extract_entities_from_text(housekeeper):
    entities = housekeeper._extract_entities_from_text(
        "昨天跟小明一起吃饭，小红告诉我项目的事"
    )
    assert "小明" in entities
    assert "小红" in entities
    # 人称代词不提取
    for stopword in ("我", "你", "他", "她们"):
        assert stopword not in entities


# ---------------------------------------------------------
# 3. 记忆桶实体提取（标签 + wikilink）
# ---------------------------------------------------------
def test_extract_entities_from_bucket(housekeeper):
    bucket = {
        "content": "跟小红一起讨论[[神经网络]]的架构",
        "metadata": {"tags": ["project", "AI"]},
    }
    entities = housekeeper._extract_entities_from_bucket(bucket)
    assert "小红" in entities
    assert "project" in entities
    assert "AI" in entities
    assert "神经网络" in entities


# ---------------------------------------------------------
# 4. 图谱重建：共享实体双向关联 + 去重 + 无交集不关联
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_rebuild_chain_graph(housekeeper):
    from housekeeper import EventChain

    c1 = EventChain("g1", "项目A开发")
    c1.entities = ["张三", "项目A"]
    await housekeeper._save_event_chain(c1)

    c2 = EventChain("g2", "项目A会议")
    c2.entities = ["项目A", "李四"]
    await housekeeper._save_event_chain(c2)

    c3 = EventChain("g3", "健身计划")
    c3.entities = ["跑步", "健身"]
    await housekeeper._save_event_chain(c3)

    result = await housekeeper._rebuild_chain_graph()
    assert result["links"] == 1

    chain1 = await housekeeper.get_event_chain("g1")
    chain2 = await housekeeper.get_event_chain("g2")
    chain3 = await housekeeper.get_event_chain("g3")
    # 双向关联
    assert "g2" in chain1.related_chain_ids
    assert "g1" in chain2.related_chain_ids
    # 无交集不关联
    assert chain3.related_chain_ids == []

    # 幂等：重复重建不产生重复关联
    await housekeeper._rebuild_chain_graph()
    chain1 = await housekeeper.get_event_chain("g1")
    assert chain1.related_chain_ids.count("g2") == 1


# ---------------------------------------------------------
# 5. _append_temporary_node 自动更新链实体
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_append_temporary_node_updates_entities(housekeeper):
    from housekeeper import EventChain

    chain = EventChain("t1", "测试链")
    await housekeeper._save_event_chain(chain)

    bucket = {"id": "b1", "content": "跟小王一起加班到很晚", "metadata": {"created": "2026-08-10T10:00:00"}}
    await housekeeper._append_temporary_node(chain, bucket)

    loaded = await housekeeper.get_event_chain("t1")
    assert "小王" in loaded.entities
    assert len(loaded.timeline) == 1


# ---------------------------------------------------------
# 6. _daily_chain_update 触发图谱重建
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_chain_update_runs_graph(housekeeper, bucket_mgr):
    # 两条都含 小明 的长效事件（备考），应生成/关联事件链
    await bucket_mgr.create(content="跟小明一起备考英语考试，背了单词", importance=6)
    await bucket_mgr.create(content="跟小明一起复习数学，做了模拟题", importance=6)

    result = await housekeeper._daily_chain_update()
    assert "chains_updated" in result
    assert "links" in result  # 图谱重建已并入日任务结果

    chains = await housekeeper.get_event_chains()
    if len(chains) >= 2:
        assert any("小明" in (c.entities or []) for c in chains)


# ---------------------------------------------------------
# 7. 日记幂等合并更新
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_journal_idempotent_merge(journal_mgr):
    # 管家先写事件摘要
    await journal_mgr.create_entry(
        date="2026-08-01", event_summary="今天做了三件事：晨跑、开会、写代码",
    )
    # 主AI补情绪（第二次调用，携带空摘要不覆盖）
    entry = await journal_mgr.create_entry(
        date="2026-08-01",
        event_summary="",
        mood_comment="忙碌但充实",
        emotion_tags=["充实", "略疲惫"],
    )
    assert entry["event_summary"] == "今天做了三件事：晨跑、开会、写代码"  # 未覆盖
    assert entry["mood_comment"] == "忙碌但充实"
    assert entry["emotion_tags"] == "充实,略疲惫"

    # 再次更新情绪（保留已有情绪，不冲突）
    entry2 = await journal_mgr.create_entry(
        date="2026-08-01",
        event_summary="",
        mood_comment="晚上放松了",
        emotion_tags=[],
    )
    assert entry2["mood_comment"] == "晚上放松了"
    assert entry2["event_summary"] == "今天做了三件事：晨跑、开会、写代码"


# ---------------------------------------------------------
# 8. emotion_tags 归一化
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_journal_emotion_tags_normalization(journal_mgr):
    # list 输入
    e1 = await journal_mgr.create_entry(date="2026-08-02", event_summary="s", emotion_tags=["焦虑", "紧张", "焦虑"])
    assert e1["emotion_tags"] == "焦虑,紧张"  # 去重

    # 中文逗号 + 顿号混合字符串输入
    e2 = await journal_mgr.create_entry(date="2026-08-03", event_summary="s", emotion_tags="开心、期待，兴奋")
    assert e2["emotion_tags"] == "开心,期待,兴奋"

    # None / 空输入
    e3 = await journal_mgr.create_entry(date="2026-08-04", event_summary="s", emotion_tags=None)
    assert e3["emotion_tags"] == ""


# ---------------------------------------------------------
# 9. date 归一化（非法 → 当前 UTC；替代格式解析）
# ---------------------------------------------------------
def test_journal_date_normalization(journal_mgr):
    # 合法日期原样保留
    assert journal_mgr._normalize_date("2026-08-01") == "2026-08-01"
    # 替代格式
    assert journal_mgr._normalize_date("2026/8/1") == "2026-08-01"
    # 非法/空 → 当前 UTC 日期
    assert journal_mgr._normalize_date("not-a-date") == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert journal_mgr._normalize_date("") == datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_journal_bad_date_graceful(journal_mgr):
    # 非法日期调用 create_entry 不抛异常，落盘到当前 UTC 日期
    entry = await journal_mgr.create_entry(date="2026.13.99", event_summary="脏数据测试")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert entry["date"] == today
    assert journal_mgr.get_entry(today) is not None


# ---------------------------------------------------------
# 10. journals/ 与 buckets/ 完全隔离
# ---------------------------------------------------------
@pytest.mark.asyncio
async def test_journal_isolation_from_buckets(test_config, bucket_mgr, journal_mgr):
    # 写入一条日记
    await journal_mgr.create_entry(date="2026-08-05", event_summary="秘密日记内容不应进记忆库")

    # 日志目录与记忆桶目录物理隔离
    journals_dir = os.path.abspath(journal_mgr.journals_dir)
    buckets_dir = os.path.abspath(test_config["buckets_dir"])
    assert not journals_dir.startswith(buckets_dir + os.sep)
    assert not buckets_dir.startswith(journals_dir + os.sep)

    # bucket_mgr 列表不包含日记文件
    all_buckets = await bucket_mgr.list_all(include_archive=False)
    journal_content = [b["content"] for b in all_buckets]
    assert not any("秘密日记内容" in c for c in journal_content)

    # 日记仍可独立查询
    entry = journal_mgr.get_entry("2026-08-05")
    assert entry and "秘密日记内容" in entry["event_summary"]


# ---------------------------------------------------------
# 11. 日记情绪标签经 emotion_manager 同义词归并
# ---------------------------------------------------------
class _FakeEmotionMgr:
    """Minimal emotion_mgr stub with async merge_tags (no real API calls).
    最小情绪管理器桩：async merge_tags，不发起真实 API 调用。"""

    def __init__(self, merged=None, exc=None):
        self.merged = merged
        self.exc = exc
        self.calls = []

    async def merge_tags(self, tags):
        self.calls.append(list(tags))
        if self.exc:
            raise self.exc
        return list(self.merged) if self.merged is not None else list(tags)


@pytest.mark.asyncio
async def test_journal_emotion_tags_merged_via_emotion_manager(test_config):
    from journal_manager import JournalManager
    em = _FakeEmotionMgr(merged=["开心"])
    jm = JournalManager(os.path.dirname(test_config["buckets_dir"]), emotion_mgr=em)

    entry = await jm.create_entry(date="2026-08-06", event_summary="s", emotion_tags="开心,喜悦,快乐")

    # 同义词归并：三个标签归一为"开心"
    assert entry["emotion_tags"] == "开心"
    # 确认 emotion_manager 收到的是归一化后的标签列表
    assert em.calls == [["开心", "喜悦", "快乐"]]


@pytest.mark.asyncio
async def test_journal_emotion_merge_failure_keeps_original(test_config):
    from journal_manager import JournalManager
    em = _FakeEmotionMgr(exc=RuntimeError("api down"))
    jm = JournalManager(os.path.dirname(test_config["buckets_dir"]), emotion_mgr=em)

    # 归并失败时非破坏性保留原值（不因 emotion_manager 故障丢数据）
    entry = await jm.create_entry(date="2026-08-07", event_summary="s", emotion_tags="开心,喜悦")
    assert entry["emotion_tags"] == "开心,喜悦"


@pytest.mark.asyncio
async def test_journal_without_emotion_mgr_unchanged(journal_mgr):
    # 无 emotion_mgr（旧式初始化）时行为不变：仅去重
    entry = await journal_mgr.create_entry(date="2026-08-08", event_summary="s", emotion_tags="开心,开心,期待")
    assert entry["emotion_tags"] == "开心,期待"


# ---------------------------------------------------------
# 12. 标签归一化查询端同义归一化
# ---------------------------------------------------------
def _write_tag_synonyms(config, mapping):
    """Write the tag synonym map to the location both tag_normalizer and
    bucket_manager expect (buckets_dir/tag_synonyms.json).
    写入同义词映射到 tag_normalizer 与 bucket_manager 约定的位置。"""
    path = os.path.join(config["buckets_dir"], "tag_synonyms.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False)
    return path


def test_tag_normalizer_expand_query(test_config, bucket_mgr):
    from tag_normalizer import TagNormalizer
    tn = TagNormalizer(test_config, bucket_mgr)
    _write_tag_synonyms(test_config, {"慢跑": "运动", "健走": "运动", "跑步": "运动"})

    expanded = tn.expand_query("慢跑 今天")
    assert "跑步" in expanded  # 同义词扩展
    assert "健走" in expanded
    assert "运动" in expanded  # 泛化标签扩展
    assert "今天" in expanded  # 无映射词原样保留

    # 无映射查询原样返回
    assert tn.expand_query("随便聊聊") == "随便聊聊"
    # 空查询不崩溃
    assert tn.expand_query("") == ""
    assert tn.expand_query(None) is None


def test_bucket_manager_expand_query_synonyms(test_config, bucket_mgr):
    _write_tag_synonyms(test_config, {"慢跑": "运动", "健走": "运动", "跑步": "运动"})

    expanded = bucket_mgr._expand_query_synonyms("慢跑 今天")
    assert "跑步" in expanded
    assert "健走" in expanded
    assert "运动" in expanded

    # 未写入映射时原样返回（不崩溃）
    bucket_mgr._tag_synonyms = {}  # 重置进程内缓存
    assert bucket_mgr._expand_query_synonyms("普通查询") == "普通查询"


@pytest.mark.asyncio
async def test_bucket_search_hits_synonym(test_config, bucket_mgr):
    # 记忆用"慢跑"标签，用户搜"跑步"也应命中
    await bucket_mgr.create(content="今天慢跑五公里，出了一身汗", importance=5, tags=["慢跑"])
    _write_tag_synonyms(test_config, {"慢跑": "运动", "跑步": "运动"})

    results = await bucket_mgr.search("跑步")
    assert results
    assert any("慢跑" in r["content"] for r in results)
