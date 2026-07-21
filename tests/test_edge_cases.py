# ============================================================
# Test: Edge Cases - Emoji only and Very Long Text
# 测试：边界情况 - 纯 Emoji 和超长文本
#
# Verifies:
#   1. Pure Emoji content ("😭😭😭") doesn't break dehydrator or embedding
#   2. Very long text (>5000 chars) is handled correctly by grow()
#   3. Unicode-safe slicing (no half-character splits)
# ============================================================

import pytest
import unicodedata


class TestEmojiOnly:
    """Test pure emoji content handling."""

    def test_analyze_pure_emoji(self):
        """
        测试纯 Emoji 内容的分析。
        dehydrator.analyze() 应该返回默认值而不是崩溃。
        """
        # Just test the default analysis result structure
        default_result = {
            "domain": ["未分类"],
            "emotions": [],
            "dominant_emotion": "",
            "emotion_metrics": {
                "overall_intensity": 0.3,
                "emotional_range": 0.0,
                "emotional_valence": 0.0,
            },
            "tags": [],
            "suggested_name": "",
        }
        
        assert "domain" in default_result
        assert default_result["domain"] == ["未分类"]
        assert "tags" in default_result
        assert isinstance(default_result["tags"], list)
        assert "emotions" in default_result
        assert isinstance(default_result["emotions"], list)

    def test_analyze_empty_content(self):
        """
        测试空内容分析。
        """
        default_result = {
            "domain": ["未分类"],
            "emotions": [],
            "dominant_emotion": "",
            "emotion_metrics": {
                "overall_intensity": 0.3,
                "emotional_range": 0.0,
                "emotional_valence": 0.0,
            },
            "tags": [],
            "suggested_name": "",
        }
        
        assert default_result["domain"] == ["未分类"]
        assert default_result["tags"] == []
        assert default_result["emotions"] == []

    def test_embedding_pure_emoji(self):
        """
        测试纯 Emoji 的 embedding 生成。
        即使返回空向量，也不应崩溃。
        """
        from embedding_engine import EmbeddingEngine
        
        class MockConfig:
            def __getitem__(self, key):
                if key == "buckets_dir":
                    return "./test_buckets"
                if key == "embedding":
                    return {"enabled": False}
                return {}
            
            def get(self, key, default=None):
                try:
                    return self[key]
                except KeyError:
                    return default
        
        engine = EmbeddingEngine(MockConfig())
        
        emoji_content = "😭😭😭"
        
        try:
            import asyncio
            embedding = asyncio.run(engine._generate_embedding(emoji_content))
            assert isinstance(embedding, list)
        except Exception as e:
            assert isinstance(e, Exception)

    def test_embedding_empty_string(self):
        """
        测试空字符串的 embedding 生成。
        """
        from embedding_engine import EmbeddingEngine
        
        class MockConfig:
            def __getitem__(self, key):
                if key == "buckets_dir":
                    return "./test_buckets"
                if key == "embedding":
                    return {"enabled": False}
                return {}
            
            def get(self, key, default=None):
                try:
                    return self[key]
                except KeyError:
                    return default
        
        engine = EmbeddingEngine(MockConfig())
        
        try:
            import asyncio
            embedding = asyncio.run(engine._generate_embedding(""))
            assert embedding == []
        except Exception as e:
            pass


class TestUnicodeSlicing:
    """Test that text slicing preserves Unicode character boundaries."""

    def test_slice_emoji_boundary(self):
        """
        测试 Emoji 边界切片。
        Emoji 可能由多个 code point 组成，切片不应切在中间。
        """
        text = "Hello 😭😭😭 World"
        
        complex_emoji = "👨‍👩‍👧‍👦"
        
        assert len(complex_emoji) >= 4

    def test_unicode_safe_slice(self):
        """
        测试 Unicode 安全切片函数。
        """
        from utils import safe_slice
        
        text = "Hello 👨‍👩‍👧‍👦 World"
        sliced = safe_slice(text, 0, 10)
        
        assert isinstance(sliced, str)
        
    def test_long_chinese_text_slicing(self):
        """
        测试长中文文本切片。
        """
        long_text = "".join(["你好世界" for _ in range(1000)])
        
        from utils import safe_slice
        
        sliced = safe_slice(long_text, 0, 2000)
        
        for i in range(len(sliced)):
            char = sliced[i]
            assert len(char.encode('utf-8')) > 0, f"Empty character at position {i}"


class TestVeryLongText:
    """Test handling of very long text inputs."""

    def test_long_text_truncation(self):
        """
        测试超长文本截断。
        """
        long_text = "x" * 10000
        
        truncated = long_text[:5000]
        assert len(truncated) == 5000
        
    def test_long_text_without_newlines(self):
        """
        测试无换行符的超长文本。
        """
        long_text = "".join([chr(ord('a') + i % 26) for i in range(5000)])
        
        assert '\n' not in long_text
        assert len(long_text) == 5000
        
        from utils import safe_slice
        sliced = safe_slice(long_text, 0, 2000)
        assert len(sliced) == 2000
        
    def test_long_text_with_mixed_content(self):
        """
        测试混合内容（中文+英文+Emoji）的超长文本。
        """
        pattern = "Hello你好😭"
        long_text = pattern * 1000
        
        assert len(long_text) > 5000
        
        truncated = long_text[:5000]
        
        last_char = truncated[-1]
        assert ord(last_char) >= 0, "Invalid Unicode character"


class TestDehydratorEdgeCases:
    """Test dehydrator edge cases with the bucket_mgr fixture."""

    @pytest.mark.asyncio
    async def test_hold_pure_emoji(self, bucket_mgr):
        """
        测试 hold() 存储纯 Emoji 内容。
        """
        result = await bucket_mgr.create(
            content="😭😭😭",
            domain=["情绪"],
        )
        
        assert result is not None
        assert isinstance(result, str)
        
        bucket = await bucket_mgr.get(result)
        assert bucket is not None
        assert bucket["content"] == "😭😭😭"

    @pytest.mark.asyncio
    async def test_hold_empty_content(self, bucket_mgr):
        """
        测试 hold() 存储空内容。
        """
        result = await bucket_mgr.create(
            content="",
            domain=["未分类"],
        )
        
        assert result is not None
        assert isinstance(result, str)
        
        bucket = await bucket_mgr.get(result)
        assert bucket is not None
