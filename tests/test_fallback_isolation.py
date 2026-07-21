# ============================================================
# Test: Fallback Isolation — 极端不相关查询的保底机制测试
# 
# 测试场景：
# 1. 数据库中有日常记忆，但查询一个完全不相关的专业术语
# 2. 验证余弦相似度全部接近 0.0
# 3. 验证能够精准触发 Fallback 保底机制
# ============================================================

import pytest
from datetime import datetime, timedelta


class TestFallbackIsolation:
    
    @pytest.mark.asyncio
    async def test_isolated_query_no_vector_overlap(self, bucket_mgr):
        """
        Test that a completely unrelated query triggers fallback.
        测试完全不相关的查询能够触发保底机制。
        """
        await bucket_mgr.create(
            content="今天和女朋友去吃了火锅，味道很不错，聊了很多开心的话题",
            name="火锅约会",
            domain=["关系"],
            valence=0.8,
            arousal=0.6,
        )
        
        await bucket_mgr.create(
            content="周末去公园跑步，天气很好，心情舒畅",
            name="公园跑步",
            domain=["健康"],
            valence=0.7,
            arousal=0.5,
        )
        
        await bucket_mgr.create(
            content="今天工作完成了一个重要的项目，感觉很有成就感",
            name="项目完成",
            domain=["工作"],
            valence=0.9,
            arousal=0.7,
        )
        
        query = "量子力学波函数塌缩薛定谔方程"
        
        matches = await bucket_mgr.search(query, limit=20)
        
        if matches:
            scores = [b.get("score", 0) for b in matches]
            print(f"Match scores: {scores}")
            
            for score in scores:
                assert score < 0.4, f"Expected score < 0.4, got {score}"
            
            for b in matches:
                dims = b.get("dimensions", {})
                vector_sim = dims.get("vector_similarity", 0)
                assert vector_sim == 0.0, f"Expected vector similarity = 0, got {vector_sim}"
        
    @pytest.mark.asyncio
    async def test_fallback_triggers_on_no_relevant_results(self, bucket_mgr):
        """
        Test that fallback is triggered when all results have low scores.
        测试当所有结果得分都很低时，能够触发保底机制。
        """
        await bucket_mgr.create(
            content="今天和女朋友去吃了火锅，味道很不错，聊了很多开心的话题",
            name="火锅约会",
            domain=["关系"],
            valence=0.8,
            arousal=0.6,
        )
        
        await bucket_mgr.create(
            content="周末去公园跑步，天气很好，心情舒畅",
            name="公园跑步",
            domain=["健康"],
            valence=0.7,
            arousal=0.5,
        )
        
        query = "量子力学波函数塌缩"
        
        matches = await bucket_mgr.search(query, limit=20)
        
        all_low_score = True
        if matches:
            for bucket in matches:
                score = bucket.get("score", 0.0)
                if score >= 0.4:
                    all_low_score = False
                    break
        
        assert all_low_score, "Expected all scores to be below 0.4 for unrelated query"
        
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()
        recent_unresolved = [
            b for b in all_buckets
            if not b["metadata"].get("resolved", False)
            and b["metadata"].get("last_active", "") >= seven_days_ago
        ]
        
        assert len(recent_unresolved) >= 2, "Expected at least 2 recent unresolved buckets for fallback"
    
    @pytest.mark.asyncio
    async def test_no_buckets_returns_empty_search(self, bucket_mgr):
        """
        Test that search returns empty when no buckets exist.
        测试当数据库为空时，搜索返回空列表。
        """
        query = "量子力学"
        matches = await bucket_mgr.search(query, limit=20)
        assert matches == [], "Expected empty result when no buckets exist"
    
    @pytest.mark.asyncio
    async def test_fallback_condition_check(self, bucket_mgr):
        """
        Test the exact fallback condition logic.
        测试保底机制的精确条件判断。
        """
        await bucket_mgr.create(
            content="今天和女朋友去吃了火锅，味道很不错，聊了很多开心的话题",
            name="火锅约会",
            domain=["关系"],
            valence=0.8,
            arousal=0.6,
        )
        
        query = "量子力学波函数塌缩"
        
        matches = await bucket_mgr.search(query, limit=20)
        
        if matches:
            all_low_score = True
            for bucket in matches:
                final_score = bucket.get("score", 0.0) / 100.0 if bucket.get("score", 0) > 1 else bucket.get("score", 0.0)
                if final_score >= 0.4:
                    all_low_score = False
                    break
        else:
            all_low_score = True
        
        assert all_low_score, "Fallback should be triggered for unrelated query"
    
    @pytest.mark.asyncio
    async def test_topic_score_dominates_over_irrelevant_content(self, bucket_mgr):
        """
        Test that topic score doesn't artificially inflate for unrelated content.
        测试主题分数不会对不相关内容产生虚假高分。
        """
        await bucket_mgr.create(
            content="今天和女朋友去吃了火锅，味道很不错，聊了很多开心的话题",
            name="火锅约会",
            domain=["关系"],
            valence=0.8,
            arousal=0.6,
        )
        
        query = "量子力学波函数塌缩"
        
        matches = await bucket_mgr.search(query, limit=20)
        
        if matches:
            for b in matches:
                dims = b.get("dimensions", {})
                topic_score = dims.get("topic_relevance", 0)
                
                assert topic_score < 0.3, f"Expected topic score < 0.3, got {topic_score}"
                
                total_score = b.get("score", 0)
                assert total_score < 0.4, f"Expected total score < 0.4, got {total_score}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
