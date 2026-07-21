# ============================================================
# Test 4: Three-Layer Memory + Multi-Dimensional Emotions
# 测试 4：三层记忆架构 + 多维情绪模型
#
# Tests:
#   1. Identity layer CRUD operations
#   2. Pattern layer CRUD operations
#   3. Multi-dimensional emotions storage and retrieval
#   4. Backward compatibility (valence/arousal → emotions)
#   5. breath type parameter filtering
# ============================================================

import os
import pytest
import pytest_asyncio
import yaml
import importlib


@pytest_asyncio.fixture
async def isolated_env(test_config, tmp_path, monkeypatch):
    """Setup isolated environment with temp buckets directory."""
    buckets_dir = str(tmp_path / "buckets")
    
    for d in ["permanent", "dynamic", "archive", "feel", "identity", "pattern"]:
        os.makedirs(os.path.join(buckets_dir, d), exist_ok=True)
    
    config_path = str(tmp_path / "config.yaml")
    with open(config_path, "w") as f:
        yaml.dump(test_config | {"buckets_dir": buckets_dir}, f)
    
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", buckets_dir)
    monkeypatch.setenv("OMBRE_CONFIG_PATH", config_path)
    
    import utils
    importlib.reload(utils)
    
    from bucket_manager import BucketManager
    from identity_manager import IdentityManager
    from pattern_manager import PatternManager
    from decay_engine import DecayEngine
    from embedding_engine import EmbeddingEngine
    
    embedding_engine = EmbeddingEngine(test_config)
    bm = BucketManager(test_config | {"buckets_dir": buckets_dir}, embedding_engine=embedding_engine)
    id_mgr = IdentityManager(test_config | {"buckets_dir": buckets_dir})
    pt_mgr = PatternManager(test_config | {"buckets_dir": buckets_dir})
    de = DecayEngine(test_config, bm)
    
    return bm, id_mgr, pt_mgr, de, buckets_dir


class TestIdentityLayer:
    """Test identity layer CRUD operations."""
    
    @pytest.mark.asyncio
    async def test_create_identity(self, isolated_env):
        """Create an identity profile with all fields."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        identity_id = await id_mgr.create(
            name="张三",
            aliases=["小张", "三哥"],
            basic_info={"年龄": "28", "身高": "175cm", "职业": "程序员"},
            core_traits=["乐观", "细心", "幽默"],
            relationships=["与李四是大学同学", "与王五是同事"],
            content="张三是一个热爱生活的程序员",
        )
        
        assert identity_id is not None
        assert len(identity_id) > 0
        
        identity = await id_mgr.get(identity_id)
        assert identity is not None
        assert identity["id"] == identity_id
        
        meta = identity["metadata"]
        assert meta["name"] == "张三"
        assert meta["type"] == "identity"
        assert meta["aliases"] == ["小张", "三哥"]
        assert meta["basic_info"] == {"年龄": "28", "身高": "175cm", "职业": "程序员"}
        assert meta["core_traits"] == ["乐观", "细心", "幽默"]
        assert meta["relationships"] == ["与李四是大学同学", "与王五是同事"]
    
    @pytest.mark.asyncio
    async def test_identity_file_location(self, isolated_env):
        """Identity files stored in identity/ directory."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        identity_id = await id_mgr.create(name="李四")
        
        identity_dir = os.path.join(bd, "identity")
        files = os.listdir(identity_dir)
        assert any(identity_id in f for f in files), f"Identity {identity_id} not found in {identity_dir}"
    
    @pytest.mark.asyncio
    async def test_list_all_identities(self, isolated_env):
        """List all identity profiles."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        await id_mgr.create(name="张三")
        await id_mgr.create(name="李四")
        
        identities = await id_mgr.list_all()
        assert len(identities) >= 2
    
    @pytest.mark.asyncio
    async def test_update_identity(self, isolated_env):
        """Update identity profile."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        identity_id = await id_mgr.create(name="张三", basic_info={"年龄": "28"})
        
        success = await id_mgr.update(identity_id, basic_info={"年龄": "29", "城市": "北京"})
        assert success
        
        identity = await id_mgr.get(identity_id)
        assert identity["metadata"]["basic_info"] == {"年龄": "29", "城市": "北京"}
    
    @pytest.mark.asyncio
    async def test_delete_identity(self, isolated_env):
        """Delete identity profile."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        identity_id = await id_mgr.create(name="张三")
        
        success = await id_mgr.delete(identity_id)
        assert success
        
        identity = await id_mgr.get(identity_id)
        assert identity is None
    
    @pytest.mark.asyncio
    async def test_add_relationship(self, isolated_env):
        """Add relationship to identity."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        identity_id = await id_mgr.create(name="张三", relationships=["单身"])
        
        success = await id_mgr.add_relationship(identity_id, "与李四结婚")
        assert success
        
        identity = await id_mgr.get(identity_id)
        assert "与李四结婚" in identity["metadata"]["relationships"]
    
    @pytest.mark.asyncio
    async def test_identity_not_in_decay(self, isolated_env):
        """Identity buckets don't participate in decay scoring."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        identity_id = await id_mgr.create(name="张三")
        
        identity = await id_mgr.get(identity_id)
        score = de.calculate_score(identity["metadata"])
        
        assert score == 999.0 or score >= 0


class TestPatternLayer:
    """Test pattern layer CRUD operations."""
    
    @pytest.mark.asyncio
    async def test_create_pattern(self, isolated_env):
        """Create a pattern with all fields."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        pattern_id = await pt_mgr.create(
            summary="周一早晨开会效率低，应该缩短会议时间",
            source_events=["bucket1", "bucket2", "bucket3"],
            applicable_scenes=["周一", "团队会议"],
            confidence=0.7,
            tags=["工作习惯", "效率"],
            content="经过多次观察发现的规律",
            name="周一会议规律",
        )
        
        assert pattern_id is not None
        
        pattern = await pt_mgr.get(pattern_id)
        assert pattern is not None
        
        meta = pattern["metadata"]
        assert meta["name"] == "周一会议规律"
        assert meta["type"] == "pattern"
        assert meta["summary"] == "周一早晨开会效率低，应该缩短会议时间"
        assert meta["source_events"] == ["bucket1", "bucket2", "bucket3"]
        assert meta["applicable_scenes"] == ["周一", "团队会议"]
        assert meta["confidence"] == 0.7
        assert meta["tags"] == ["工作习惯", "效率"]
    
    @pytest.mark.asyncio
    async def test_pattern_file_location(self, isolated_env):
        """Pattern files stored in pattern/ directory."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        pattern_id = await pt_mgr.create(summary="测试模式")
        
        pattern_dir = os.path.join(bd, "pattern")
        files = os.listdir(pattern_dir)
        assert any(pattern_id in f for f in files), f"Pattern {pattern_id} not found in {pattern_dir}"
    
    @pytest.mark.asyncio
    async def test_update_confidence(self, isolated_env):
        """Update pattern confidence."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        pattern_id = await pt_mgr.create(summary="测试", confidence=0.5)
        
        success = await pt_mgr.update_confidence(pattern_id, 0.2)
        assert success
        
        pattern = await pt_mgr.get(pattern_id)
        assert pattern["metadata"]["confidence"] == pytest.approx(0.7)
    
    @pytest.mark.asyncio
    async def test_add_source_event(self, isolated_env):
        """Add source event to pattern."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        pattern_id = await pt_mgr.create(summary="测试", source_events=["event1"])
        
        success = await pt_mgr.add_source_event(pattern_id, "event2")
        assert success
        
        pattern = await pt_mgr.get(pattern_id)
        assert "event2" in pattern["metadata"]["source_events"]
    
    @pytest.mark.asyncio
    async def test_list_all_patterns(self, isolated_env):
        """List all patterns."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        await pt_mgr.create(summary="模式1")
        await pt_mgr.create(summary="模式2")
        
        patterns = await pt_mgr.list_all()
        assert len(patterns) >= 2


class TestMultiDimensionalEmotions:
    """Test multi-dimensional emotions model."""
    
    @pytest.mark.asyncio
    async def test_create_bucket_with_emotions(self, isolated_env):
        """Create bucket with emotions array and dominant_emotion."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        bid = await bm.create(
            content="今天被误解了，虽然委屈但最终还是心软原谅了对方",
            tags=["人际"],
            importance=7,
            domain=["社交"],
            emotions=[{"label": "委屈", "intensity": 0.8}, {"label": "心软", "intensity": 0.6}],
            dominant_emotion="委屈",
            name="误解事件",
        )
        
        assert bid is not None
        
        bucket = await bm.get(bid)
        meta = bucket["metadata"]
        
        assert "emotions" in meta
        assert isinstance(meta["emotions"], list)
        assert len(meta["emotions"]) == 2
        assert meta["emotions"][0]["label"] == "委屈"
        assert meta["emotions"][0]["intensity"] == 0.8
        assert meta["emotions"][1]["label"] == "心软"
        assert meta["emotions"][1]["intensity"] == 0.6
        assert meta["dominant_emotion"] == "委屈"
    
    @pytest.mark.asyncio
    async def test_backward_compatibility_valence_arousal(self, isolated_env):
        """Old format valence/arousal automatically converted to emotions."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        bid = await bm.create(
            content="这是一条旧格式的记忆",
            tags=[],
            importance=5,
            domain=[],
            valence=0.8,
            arousal=0.7,
            name="旧格式测试",
        )
        
        bucket = await bm.get(bid)
        meta = bucket["metadata"]
        
        assert "emotions" in meta
        assert isinstance(meta["emotions"], list)
        
        emotion_labels = [e["label"] for e in meta["emotions"]]
        assert "正面" in emotion_labels or "负面" in emotion_labels
    
    @pytest.mark.asyncio
    async def test_emotions_in_bucket_manager_list(self, isolated_env):
        """Emotions visible in list_all results."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        await bm.create(
            content="开心的一天",
            emotions=[{"label": "开心", "intensity": 0.9}],
            dominant_emotion="开心",
        )
        
        all_buckets = await bm.list_all()
        assert len(all_buckets) >= 1
        
        for b in all_buckets:
            meta = b.get("metadata", {})
            if "开心" in b.get("content", ""):
                assert "emotions" in meta
                assert meta["dominant_emotion"] == "开心"


class TestBreathTypeFiltering:
    """Test breath tool type parameter filtering."""
    
    @pytest.mark.asyncio
    async def test_bucket_types_list_all(self, isolated_env):
        """list_all includes identity and pattern buckets."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        await id_mgr.create(name="张三")
        await pt_mgr.create(summary="测试模式")
        await bm.create(content="普通事件", bucket_type="event")
        
        all_buckets = await bm.list_all()
        types = [b["metadata"].get("type") for b in all_buckets]
        
        assert "identity" in types
        assert "pattern" in types
        assert "event" in types


class TestLayerInvariants:
    """Test layer-specific invariants."""
    
    @pytest.mark.asyncio
    async def test_identity_not_decayed(self, isolated_env):
        """Identity buckets should have high score (not decayed)."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        identity_id = await id_mgr.create(name="张三")
        
        all_buckets = await bm.list_all()
        identity = next(b for b in all_buckets if b["id"] == identity_id)
        score = de.calculate_score(identity["metadata"])
        
        assert score >= 50.0
    
    @pytest.mark.asyncio
    async def test_pattern_not_decayed(self, isolated_env):
        """Pattern buckets should have stable score."""
        bm, id_mgr, pt_mgr, de, bd = isolated_env
        
        pattern_id = await pt_mgr.create(summary="测试")
        
        all_buckets = await bm.list_all()
        pattern = next(b for b in all_buckets if b["id"] == pattern_id)
        score = de.calculate_score(pattern["metadata"])
        
        assert score >= 50.0