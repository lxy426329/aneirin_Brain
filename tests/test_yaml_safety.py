# ============================================================
# Test: YAML Safety - Content with YAML-sensitive characters
# 测试：YAML 安全 - 包含 YAML 敏感字符的内容
#
# Verifies:
#   1. Content with colons, quotes, and --- doesn't break frontmatter
#   2. Metadata values with special characters are properly escaped
#   3. round-trip (create → read) preserves content correctly
# ============================================================

import pytest
import frontmatter


class TestYamlSafety:
    """Test YAML safety with special characters."""

    @pytest.mark.asyncio
    async def test_content_with_yaml_special_chars(self, bucket_mgr):
        """
        测试正文包含 YAML 敏感字符。
        content="今天学了 YAML: 格式---很奇妙 'quoted'"
        """
        content = "今天学了 YAML: 格式---很奇妙 'quoted'"
        
        bucket_id = await bucket_mgr.create(
            content=content,
            domain=["学习"],
        )
        
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket is not None
        assert bucket["content"] == content

    @pytest.mark.asyncio
    async def test_content_with_triple_dash(self, bucket_mgr):
        """
        测试正文包含 --- 分隔符。
        """
        content = """这是第一部分---这是第二部分---这是第三部分"""
        
        bucket_id = await bucket_mgr.create(
            content=content,
            domain=["测试"],
        )
        
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket is not None
        assert "---" in bucket["content"]

    @pytest.mark.asyncio
    async def test_content_with_colons(self, bucket_mgr):
        """
        测试正文包含多个冒号。
        """
        content = "HTTP: GET /api/v1/users?id=1&name=test"
        
        bucket_id = await bucket_mgr.create(
            content=content,
            domain=["技术"],
        )
        
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket is not None
        assert bucket["content"] == content

    @pytest.mark.asyncio
    async def test_content_with_quotes(self, bucket_mgr):
        """
        测试正文包含单双引号。
        """
        content = 'He said "Hello" and she replied \'Hi\''
        
        bucket_id = await bucket_mgr.create(
            content=content,
            domain=["对话"],
        )
        
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket is not None
        assert bucket["content"] == content

    @pytest.mark.asyncio
    async def test_content_with_newlines(self, bucket_mgr):
        """
        测试正文包含换行符。
        """
        content = """第一行
第二行
第三行"""
        
        bucket_id = await bucket_mgr.create(
            content=content,
            domain=["测试"],
        )
        
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket is not None
        assert "\n" in bucket["content"]

    @pytest.mark.asyncio
    async def test_metadata_with_special_chars(self, bucket_mgr):
        """
        测试元数据值包含特殊字符。
        """
        content = "普通内容"
        
        bucket_id = await bucket_mgr.create(
            content=content,
            domain=["测试"],
            name="名称: 包含:冒号的名称",
        )
        
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket is not None
        assert "名称: 包含:冒号的名称" in bucket["metadata"].get("name", "")

    @pytest.mark.asyncio
    async def test_content_starts_with_dash(self, bucket_mgr):
        """
        测试正文以 - 开头（YAML 列表项）。
        """
        content = "- 项目一\n- 项目二\n- 项目三"
        
        bucket_id = await bucket_mgr.create(
            content=content,
            domain=["测试"],
        )
        
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket is not None
        assert bucket["content"].startswith("- ")

    @pytest.mark.asyncio
    async def test_content_starts_with_hash(self, bucket_mgr):
        """
        测试正文以 # 开头（YAML 注释）。
        """
        content = "# 这是一个标题\n正文内容"
        
        bucket_id = await bucket_mgr.create(
            content=content,
            domain=["测试"],
        )
        
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket is not None
        assert bucket["content"].startswith("# ")

    @pytest.mark.asyncio
    async def test_content_with_yaml_list(self, bucket_mgr):
        """
        测试正文包含 YAML 列表格式。
        """
        content = """购物清单：
- 牛奶
- 面包
- 鸡蛋"""
        
        bucket_id = await bucket_mgr.create(
            content=content,
            domain=["生活"],
        )
        
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket is not None
        assert "- 牛奶" in bucket["content"]

    @pytest.mark.asyncio
    async def test_roundtrip_complex_content(self, bucket_mgr):
        """
        测试复杂内容的往返（创建 → 读取）。
        """
        content = """今天学了 YAML: 格式---很奇妙 'quoted'
        
列表项：
- 项目一
- 项目二

URL: http://example.com/path?a=1&b=2

多行文本：
第一行
第二行
第三行"""
        
        bucket_id = await bucket_mgr.create(
            content=content,
            domain=["学习"],
        )
        
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket is not None
        
        # Verify all special parts are preserved
        assert "YAML:" in bucket["content"]
        assert "---" in bucket["content"]
        assert "'quoted'" in bucket["content"]
        assert "- 项目一" in bucket["content"]
        assert "http://example.com" in bucket["content"]
        assert "第一行" in bucket["content"]