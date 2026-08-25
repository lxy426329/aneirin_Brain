# ============================================================
# Module: Memory Bucket Manager (bucket_manager.py)
# 模块：记忆桶管理器
#
# CRUD operations, multi-dimensional index search, activation updates
# for memory buckets.
# 记忆桶的增删改查、多维索引搜索、激活更新。
#
# Core design:
# 核心逻辑：
#   - Each bucket = one Markdown file (YAML frontmatter + body)
#     每个记忆桶 = 一个 Markdown 文件
#   - Storage by type: permanent / dynamic / archive
#     存储按类型分目录
#   - Multi-dimensional soft index: domain + valence/arousal + fuzzy text
#     多维软索引：主题域 + 情感坐标 + 文本模糊匹配
#   - Search strategy: domain pre-filter → weighted multi-dim ranking
#     搜索策略：主题域预筛 → 多维加权精排
#   - Emotion coordinates based on Russell circumplex model:
#     情感坐标基于环形情感模型（Russell circumplex）：
#       valence (0~1): 0=negative → 1=positive
#       arousal (0~1): 0=calm → 1=excited
#
# Depended on by: server.py, decay_engine.py
# 被谁依赖：server.py, decay_engine.py
# ============================================================

import os
import re
import json
import math
import logging
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import frontmatter
from rapidfuzz import fuzz

from utils import (
    generate_bucket_id,
    sanitize_name,
    sanitize_filename,
    safe_path,
    now_iso,
    safe_int,
    safe_float,
    safe_bool,
    as_list,
)

logger = logging.getLogger("ombre_brain.bucket")

try:
    from hybrid_search import HybridSearchEngine
    HAS_HYBRID_SEARCH = True
except ImportError:
    HAS_HYBRID_SEARCH = False
    logger.warning("Hybrid search module not found, using legacy search")

# ---------------------------------------------------------
# File lock for safe concurrent Markdown read/write
# 文件锁，保证 Markdown 读写操作的并发安全
# ---------------------------------------------------------
_file_lock = threading.Lock()


class BucketManager:
    """
    Memory bucket manager — entry point for all bucket CRUD operations.
    Buckets are stored as Markdown files with YAML frontmatter for metadata
    and body for content. Natively compatible with Obsidian browsing/editing.
    记忆桶管理器 —— 所有桶的 CRUD 操作入口。
    桶以 Markdown 文件存储，YAML frontmatter 存元数据，正文存内容。
    天然兼容 Obsidian 直接浏览和编辑。
    """

    def __init__(self, config: dict, embedding_engine=None):
        # --- Read storage paths from config / 从配置中读取存储路径 ---
        self.base_dir = config["buckets_dir"]
        self.permanent_dir = os.path.join(self.base_dir, "permanent")
        self.dynamic_dir = os.path.join(self.base_dir, "dynamic")
        self.archive_dir = os.path.join(self.base_dir, "archive")
        self.feel_dir = os.path.join(self.base_dir, "feel")
        self.identity_dir = os.path.join(self.base_dir, "identity")
        # --- Ring layer (年轮经验层): replaced legacy pattern/ dir, with auto-migration ---
        # --- ring 层：取代旧 pattern/ 目录；旧数据自动迁移，pattern_dir 变量继续指向 ring/ ---
        self.ring_dir = os.path.join(self.base_dir, "ring")
        self._migrate_legacy_dir("pattern", "ring", "pattern_dir", self.ring_dir)
        # --- Milestone layer (重要时刻/纪念日): high emotional intensity, never decay ---
        # --- milestone 层：高情绪浓度重要时刻（纪念日/重要事件），永不衰减，检索与 permanent 分离 ---
        self.milestone_dir = os.path.join(self.base_dir, "milestone")
        os.makedirs(self.milestone_dir, exist_ok=True)
        # --- Voice layer (说话习惯/称呼/相处方式): always injected by breath(), no scoring/decay ---
        # --- voice 层：说话习惯、称呼、相处方式；按需检索，不走评分、不走衰减 ---
        self.voice_dir = os.path.join(self.base_dir, "voice")
        os.makedirs(self.voice_dir, exist_ok=True)
        # --- Boundary layer (认知共识/逻辑底线/原则): activated on severe cognitive distortion ---
        # --- boundary 层：双方确立的认知共识、逻辑底线与原则；消极言论/认知偏差时高优先级激活，永不衰减 ---
        self.boundary_dir = os.path.join(self.base_dir, "boundary")
        os.makedirs(self.boundary_dir, exist_ok=True)
        # --- Faded memory (模糊印象标签): decayed dynamic memories compressed to light tags ---
        # --- faded 层：久远普通记忆衰减后降维为轻量模糊印象标签，不物理删除 ---
        self.faded_dir = os.path.join(self.base_dir, "faded")
        os.makedirs(self.faded_dir, exist_ok=True)
        # --- Ephemeral layer (短期绝密暂存区): 24h half-life vent-only content, evaporated in nightly dream() ---
        # --- ephemeral 层：半衰期 24 小时的临时吐槽暂存区；未被再次引用的内容在 nightly dream() 中自然蒸发 ---
        self.ephemeral_dir = os.path.join(self.base_dir, "ephemeral")
        os.makedirs(self.ephemeral_dir, exist_ok=True)
        # --- Dream sandbox (临时梦境沙盒): sleep-time intermediate associations & drafts ONLY live here,
        # --- never as permanent memory; physically cleared at day-end regardless of approval outcome.
        # --- 梦境沙盒：睡眠做梦的中间联想与草稿只允许写入临时文件，绝不直接落库；日终物理清空 ---
        self.temp_dreams_dir = os.path.join(self.base_dir, "temp_dreams")
        os.makedirs(self.temp_dreams_dir, exist_ok=True)
        self.fuzzy_threshold = config.get("matching", {}).get("fuzzy_threshold", 30)
        self.max_results = config.get("matching", {}).get("max_results", 5)

        # --- Wikilink config / 双链配置 ---
        wikilink_cfg = config.get("wikilink", {})
        self.wikilink_enabled = wikilink_cfg.get("enabled", True)
        self.wikilink_use_tags = wikilink_cfg.get("use_tags", False)
        self.wikilink_use_domain = wikilink_cfg.get("use_domain", True)
        self.wikilink_use_auto_keywords = wikilink_cfg.get("use_auto_keywords", True)
        self.wikilink_auto_top_k = wikilink_cfg.get("auto_top_k", 8)
        self.wikilink_min_len = wikilink_cfg.get("min_keyword_len", 2)
        self.wikilink_exclude_keywords = set(wikilink_cfg.get("exclude_keywords", []))
        self.wikilink_stopwords = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
            "都", "一个", "上", "也", "很", "到", "说", "要", "去",
            "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
            "我们", "你们", "他们", "然后", "今天", "昨天", "明天", "一下",
            "the", "and", "for", "are", "but", "not", "you", "all", "can",
            "had", "her", "was", "one", "our", "out", "has", "have", "with",
            "this", "that", "from", "they", "been", "said", "will", "each",
        }
        self.wikilink_stopwords |= {w.lower() for w in self.wikilink_exclude_keywords}

        # --- Search scoring weights / 检索权重配置 ---
        scoring = config.get("scoring_weights", {})
        # New multi-dimensional continuous scoring system:
        # Final_Score = (W1 * Emotion_Arousal) + (W2 * Explicit_Priority) + (W3 * Vector_Similarity) + (W4 * Topic_Relevance) + (W5 * Time_Proximity)
        # Tuned to reduce context noise: topic & vector weights lowered, explicit priority raised.
        # 权重已微调以降低上下文噪音：降低主题/向量权重，提高显式优先级权重。
        self.w_emotion_arousal = scoring.get("emotion_arousal", 3.0)    # 情绪唤醒度权重
        self.w_explicit_priority = scoring.get("explicit_priority", 4.0)  # 显式优先级权重（钉选/保护，提高）
        self.w_vector_similarity = scoring.get("vector_similarity", 3.0)  # 向量相似度权重（降低）
        self.w_topic = scoring.get("topic_relevance", 2.0)               # 主题相关性权重（降低）
        self.w_time = scoring.get("time_proximity", 1.5)
        self.content_weight = scoring.get("content_weight", 1.0)

        # --- Optional embedding engine for pre-filtering / 可选 embedding 引擎，用于预筛候选集 ---
        self.embedding_engine = embedding_engine

        # --- Hybrid search engine (BM25 + Vector + Rerank) ---
        # --- 混合检索引擎（BM25关键词 + 向量语义 + Rerank重排序）---
        self.hybrid_search = None
        if HAS_HYBRID_SEARCH:
            hybrid_cfg = config.get("hybrid_search", {})
            self.hybrid_search = HybridSearchEngine(hybrid_cfg, embedding_engine)

        # --- Anchor storage / 锚点存储目录 ---
        self.anchor_dir = os.path.join(self.base_dir, "anchor")
        os.makedirs(self.anchor_dir, exist_ok=True)

        # --- Timeline storage / 时间链存储目录 ---
        self.timeline_dir = os.path.join(self.base_dir, "timeline")
        os.makedirs(self.timeline_dir, exist_ok=True)
        
        # --- Candlestick storage / 烛台存储目录 ---
        self.candlestick_dir = os.path.join(self.base_dir, "candlestick")
        os.makedirs(self.candlestick_dir, exist_ok=True)

        # --- Memory cache for list_all / list_all 内存缓存 ---
        self._buckets_cache = []
        self._cache_timestamp = 0
        self._cache_validity = 5  # 5 seconds cache validity

        # --- Query-side tag synonym map (persisted by tag_normalizer) ---
        # --- 查询端标签同义词映射（由 tag_normalizer 持久化），懒加载 ---
        self._tag_synonyms = None
        self._tag_synonyms_file = os.path.join(self.base_dir, "tag_synonyms.json")

        # --- Cooldown state for pattern/experience injection ---
        # --- 年轮经验注入冷却状态（内存级，不持久化）---
        # Records {bucket_id: last_injected_timestamp} for cooldown tracking
        activation_cfg = config.get("activation", {})
        self.cooldown_seconds = activation_cfg.get("cooldown_seconds", 300)  # 5 min default
        self.similarity_threshold = activation_cfg.get("similarity_threshold", 0.75)
        self.cooldown_decay_factor = activation_cfg.get("cooldown_decay_factor", 0.5)
        self._injection_history: dict[str, float] = {}  # {bucket_id: timestamp}

    def _migrate_legacy_dir(self, legacy_name: str, new_name: str, dir_attr: str, target_dir: str):
        """
        Migrate a legacy directory into the new-named directory (rename if target
        absent, otherwise merge missing files). The class attribute `dir_attr` keeps
        pointing at `target_dir`, so all existing callers pick up the new location.
        把旧目录迁移到新目录：目标不存在则整体改名，已存在则合并缺失文件。
        `dir_attr` 属性始终指向新目录，现有调用方无需改动。
        """
        legacy_dir = os.path.join(self.base_dir, legacy_name)
        setattr(self, dir_attr, target_dir)
        if os.path.isdir(legacy_dir):
            try:
                if not os.path.isdir(target_dir):
                    os.rename(legacy_dir, target_dir)
                    logger.info(
                        f"Migrated legacy directory / 目录迁移：{legacy_name}/ → {new_name}/"
                    )
                else:
                    for entry in os.listdir(legacy_dir):
                        src = os.path.join(legacy_dir, entry)
                        dst = os.path.join(target_dir, entry)
                        if os.path.isfile(src) and not os.path.exists(dst):
                            os.rename(src, dst)
                    try:
                        if not os.listdir(legacy_dir):
                            os.rmdir(legacy_dir)
                    except OSError:
                        pass
            except OSError as e:
                logger.warning(
                    f"Legacy directory migration failed, using target directly / "
                    f"旧目录迁移失败，直接使用新目录: {e}"
                )
        os.makedirs(target_dir, exist_ok=True)

    # ---------------------------------------------------------
    # Timeline operations / 时间链操作
    # ---------------------------------------------------------
    async def save_timeline(self, query: str, timeline_data: dict):
        """
        Save a generated timeline.
        Forcibly converts all phase times to ISO-8601 (YYYY-MM-DD).

        保存生成的时间链。
        强制将所有阶段时间转换为 ISO-8601 格式（YYYY-MM-DD）。
        """
        from utils import normalize_to_iso_date

        # --- Forcibly normalize all phase times to ISO-8601 ---
        # --- 强制将所有阶段时间归一化为 ISO-8601 ---
        normalized_phases = []
        for phase in timeline_data.get("phases", []):
            if isinstance(phase, dict):
                raw_time = phase.get("time", "")
                iso_time = normalize_to_iso_date(raw_time)
                normalized_phase = dict(phase)
                normalized_phase["time"] = iso_time
                # Preserve original time as display_original for reference
                if raw_time and raw_time != iso_time:
                    normalized_phase["time_original"] = raw_time
                normalized_phases.append(normalized_phase)
            else:
                normalized_phases.append(phase)

        timeline = {
            "id": generate_bucket_id(),
            "query": query,
            "title": timeline_data.get("title", ""),
            "summary": timeline_data.get("summary", ""),
            "phases": normalized_phases,
            "created": now_iso(),
            "updated": now_iso(),
            "decayed": False,
        }

        file_path = os.path.join(self.timeline_dir, f"{timeline['id']}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            import json
            json.dump(timeline, f, ensure_ascii=False, indent=2)

        logger.info(f"Timeline saved (times normalized to ISO-8601) / 时间链已保存（时间已归一化为 ISO-8601）: query={query}")
        return timeline

    async def get_timelines(self):
        """Get all saved timelines."""
        timelines = []
        import json
        
        for filename in os.listdir(self.timeline_dir):
            if filename.endswith(".json"):
                file_path = os.path.join(self.timeline_dir, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        timeline = json.load(f)
                        timelines.append(timeline)
                except Exception as e:
                    logger.warning(f"Failed to load timeline file / 加载时间链文件失败: {file_path}: {e}")
        
        timelines.sort(key=lambda t: t.get("updated", ""), reverse=True)
        return timelines

    async def get_timeline(self, timeline_id: str):
        """Get a specific timeline by ID."""
        file_path = os.path.join(self.timeline_dir, f"{timeline_id}.json")
        if os.path.exists(file_path):
            import json
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load timeline / 加载时间链失败: {file_path}: {e}")
        return None

    async def decay_timeline(self, timeline_id: str):
        """
        Decay a timeline: replace full content with a summary.
        衰减时间链：用一句话总结替换完整内容。
        """
        file_path = os.path.join(self.timeline_dir, f"{timeline_id}.json")
        if not os.path.exists(file_path):
            return False
        
        import json
        with open(file_path, "r", encoding="utf-8") as f:
            timeline = json.load(f)
        
        if timeline.get("decayed"):
            return False
        
        original_summary = timeline.get("summary", "")
        original_title = timeline.get("title", "")
        created = timeline.get("created", "")
        
        decayed_summary = f"{created}: {original_title} - {original_summary[:100]}..."
        
        timeline["phases"] = []
        timeline["summary"] = decayed_summary
        timeline["decayed"] = True
        timeline["updated"] = now_iso()
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(timeline, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Timeline decayed / 时间链已衰减: {timeline_id}")
        return True

    async def delete_timeline(self, timeline_id: str):
        """Delete a timeline by ID."""
        file_path = os.path.join(self.timeline_dir, f"{timeline_id}.json")
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Timeline deleted / 删除时间链: {timeline_id}")
            return True
        return False

    # ---------------------------------------------------------
    # Candlestick operations / 烛台操作
    # ---------------------------------------------------------
    async def save_candlestick(self, content: str, bucket_id: str = None, title: str = ""):
        """
        Save a candlestick (reflection/thought).
        保存烛台（智能体的感想/反思）。

        Args:
            content: The reflection content, can include bucket references or specific events
            bucket_id: Optional reference to a memory bucket
            title: Optional title for the candlestick
        """
        # Auto-generate title from content if not provided
        if not title or not title.strip():
            title = content.strip().split("\n")[0][:20] if content.strip() else "无标题"

        candlestick = {
            "id": generate_bucket_id(),
            "title": title,
            "content": content,
            "bucket_id": bucket_id,
            "created": now_iso(),
            "updated": now_iso(),
            "decayed": False,
        }
        
        file_path = os.path.join(self.candlestick_dir, f"{candlestick['id']}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            import json
            json.dump(candlestick, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Candlestick saved / 烛台已保存: id={candlestick['id']}, bucket={bucket_id}")
        return candlestick

    async def get_candlesticks(self):
        """Get all saved candlesticks, sorted by creation time (newest first)."""
        candlesticks = []
        import json

        for filename in os.listdir(self.candlestick_dir):
            if filename.endswith(".json"):
                file_path = os.path.join(self.candlestick_dir, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        candlestick = json.load(f)
                        # --- Auto-fix empty titles in old data ---
                        # --- 自动修复旧数据中为空的标题 ---
                        if not candlestick.get("title", "").strip():
                            content = candlestick.get("content", "")
                            candlestick["title"] = content.strip().split("\n")[0][:20] if content.strip() else "无标题"
                        candlesticks.append(candlestick)
                except Exception as e:
                    logger.warning(f"Failed to load candlestick file / 加载烛台文件失败: {file_path}: {e}")

        candlesticks.sort(key=lambda c: c.get("created", ""), reverse=True)
        return candlesticks

    async def get_candlestick(self, candlestick_id: str):
        """Get a specific candlestick by ID."""
        file_path = os.path.join(self.candlestick_dir, f"{candlestick_id}.json")
        if os.path.exists(file_path):
            import json
            with open(file_path, "r", encoding="utf-8") as f:
                candlestick = json.load(f)
                # --- Auto-fix empty titles in old data ---
                # --- 自动修复旧数据中为空的标题 ---
                if not candlestick.get("title", "").strip():
                    content = candlestick.get("content", "")
                    candlestick["title"] = content.strip().split("\n")[0][:20] if content.strip() else "无标题"
                return candlestick
        return None

    async def delete_candlestick(self, candlestick_id: str):
        """Delete a candlestick by ID."""
        file_path = os.path.join(self.candlestick_dir, f"{candlestick_id}.json")
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Candlestick deleted / 删除烛台: {candlestick_id}")
            return True
        return False

    async def retrieve_candlesticks_for_flavor(
        self,
        query: str = "",
        max_count: int = 2,
        random_probability: float = 0.3,
    ) -> list[dict]:
        """
        Retrieve candlesticks for "flavor" injection — low-priority, casual chat seasoning.
        
        低优先级烛台检索 —— 作为闲聊时的语气调味料，不参与强制行为规则约束。
        
        Rules:
        - Only inject when chatting casually and no strong anchors triggered
        - Low weight random sampling or semantic matching
        - Returns at most max_count candlesticks
        - random_probability: probability of including any candlestick (0.0 ~ 1.0)
        
        规则：
        - 仅在闲聊且没有强锚点触发时注入
        - 低权重随机采样或语义匹配
        - 返回最多 max_count 条
        - random_probability: 采样概率（0.0~1.0），越低越谨慎
        
        Args:
            query: Optional query for semantic matching
            max_count: Maximum number of candlesticks to return
            random_probability: Probability of including candlesticks
        
        Returns:
            List of candlestick dicts (lightweight format)
        """
        import random

        candlesticks = await self.get_candlesticks()
        if not candlesticks:
            return []

        # --- Apply random probability gate ---
        # --- 随机概率门：只有概率命中才注入 ---
        if random.random() > random_probability:
            logger.debug(
                f"Candlestick flavor injection skipped (probability gate) / "
                f"烛台调味料注入跳过（概率门）: prob={random_probability}"
            )
            return []

        # --- Semantic matching if query provided ---
        # --- 如果有 query，进行语义匹配 ---
        if query and query.strip():
            scored_candles = []
            for candle in candlesticks:
                content = candle.get("content", "") + candle.get("title", "")
                if not content:
                    continue
                # Simple fuzzy match for flavor matching
                relevance = fuzz.partial_ratio(query.lower(), content.lower())
                if relevance >= 30:  # Low threshold for flavor matching
                    scored_candles.append((candle, relevance))
            
            # Sort by relevance and take top
            scored_candles.sort(key=lambda x: x[1], reverse=True)
            selected = [c for c, _ in scored_candles[:max_count]]
        else:
            # --- Random sampling without query ---
            # --- 无 query 时随机采样 ---
            shuffled = list(candlesticks)
            random.shuffle(shuffled)
            selected = shuffled[:max_count]

        logger.info(
            f"Candlestick flavor injection / 烛台调味料注入: "
            f"selected={len(selected)}/{len(candlesticks)}, query={query[:30] if query else ''}"
        )

        return selected

    # ---------------------------------------------------------
    # Pattern operations / 行为模式操作
    # ---------------------------------------------------------
    async def save_pattern(self, name: str, description: str, triggers: str = ""):
        """
        Save a behavior pattern.
        保存行为模式。
        
        Args:
            name: Pattern name
            description: Pattern description/summary
            triggers: Optional trigger conditions
        """
        from pattern_manager import PatternManager
        
        pattern_mgr = PatternManager({"buckets_dir": self.base_dir})
        pattern_id = await pattern_mgr.create(
            name=name,
            summary=description,
            content=triggers,
        )
        
        logger.info(f"Pattern saved / 行为模式已保存: id={pattern_id}, name={name}")
        return {"id": pattern_id}
    
    async def get_patterns(self):
        """Get all behavior patterns, excluding superseded ones."""
        from pattern_manager import PatternManager
        
        pattern_mgr = PatternManager({"buckets_dir": self.base_dir})
        all_patterns = await pattern_mgr.list_all()
        
        active_patterns = [
            p for p in all_patterns 
            if p["metadata"].get("superseded_by") is None
        ]
        
        return active_patterns
    
    async def get_pattern(self, pattern_id: str):
        """Get a specific pattern by ID."""
        from pattern_manager import PatternManager
        
        pattern_mgr = PatternManager({"buckets_dir": self.base_dir})
        return await pattern_mgr.get(pattern_id)
    
    async def delete_pattern(self, pattern_id: str):
        """Delete a pattern by ID."""
        from pattern_manager import PatternManager
        
        pattern_mgr = PatternManager({"buckets_dir": self.base_dir})
        return await pattern_mgr.delete(pattern_id)

    # ---------------------------------------------------------
    # Anchor operations / 锚点操作
    # ---------------------------------------------------------
    async def add_anchor(
        self,
        triggers: list = None,
        emotional_baseline: list = None,
        boundaries: list = None,
        related_bucket_ids: list = None,
        anchor_type: str = "dynamic",
        ttl_hours: float = None,
        name: str = "",
        # Legacy fields (backward compat)
        bucket_id: str = None,
        emotion_intensity: float = 0.0,
        summary: str = "",
        coordinates: dict = None,
        emotion_tags: list = None,
    ):
        """
        Add a behavioral & emotional pivot anchor.
        添加行为与情绪锚点。

        Anchors store RULES and GUIDANCE, not event details.
        Event details are referenced via related_bucket_ids (pointers only).

        锚点只存规则和指导，具体事件细节只留 bucket_id 指针。

        Args:
            triggers: 触发条件列表（触发词/场景）
            emotional_baseline: 情绪基调列表（情绪指导指令）
            boundaries: 行为禁忌列表
            related_bucket_ids: 关联记忆桶 IDs（仅保留指针，不存文本）
            anchor_type: "static" (核心锚点，极少变动) | "dynamic" (即时锚点，随情绪衰减)
            ttl_hours: 动态锚点的半衰期（小时），static 锚点忽略此字段
            name: 锚点名称（简短标识）

        Legacy args (backward compat):
            bucket_id, emotion_intensity, summary, coordinates, emotion_tags
        """
        # --- Build new-format anchor ---
        # --- 兼容旧格式：如果传了 bucket_id/summary，自动转换为新格式 ---
        if triggers is None and summary:
            # Legacy mode: convert summary to triggers
            triggers = [summary[:60]] if summary else []
        if related_bucket_ids is None and bucket_id:
            related_bucket_ids = [bucket_id]
        if emotion_tags and not emotional_baseline:
            emotional_baseline = emotion_tags

        anchor = {
            "id": generate_bucket_id(),
            "name": name or (summary[:30] if summary else f"anchor_{anchor_type}"),
            "anchor_type": anchor_type,  # "static" | "dynamic"
            "is_active": True,
            "triggers": triggers or [],
            "emotional_baseline": emotional_baseline or [],
            "boundaries": boundaries or [],
            "related_bucket_ids": related_bucket_ids or [],
            "created": now_iso(),
            "updated": now_iso(),
            "deactivated_at": None,
        }

        # --- TTL for dynamic anchors ---
        if anchor_type == "dynamic":
            anchor["ttl_hours"] = ttl_hours if ttl_hours is not None else 48.0  # default 48h
            anchor["expires_at"] = (
                datetime.now() + timedelta(hours=anchor["ttl_hours"])
            ).isoformat(timespec="seconds")
        else:
            # Static anchors never expire
            anchor["ttl_hours"] = None
            anchor["expires_at"] = None

        # --- Legacy fields (for backward compat) ---
        if emotion_intensity:
            anchor["emotion_intensity"] = emotion_intensity
        if summary:
            anchor["summary"] = summary
        if coordinates:
            anchor["coordinates"] = coordinates
        if emotion_tags:
            anchor["emotion_tags"] = emotion_tags

        file_path = os.path.join(self.anchor_dir, f"{anchor['id']}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            import json
            json.dump(anchor, f, ensure_ascii=False, indent=2)

        logger.info(
            f"Anchor added / 添加锚点: type={anchor_type}, active={anchor['is_active']}, "
            f"triggers={len(triggers or [])}, boundaries={len(boundaries or [])}"
        )
        return anchor

    async def get_anchors(
        self,
        bucket_id: str = None,
        active_only: bool = False,
        anchor_type: str = None,
    ):
        """
        Get anchors, optionally filtered.
        获取锚点，可选过滤。

        Args:
            bucket_id: Filter by related bucket ID
            active_only: If True, only return active anchors (and auto-deactivate expired ones)
            anchor_type: Filter by "static" or "dynamic"
        """
        anchors = []
        import json

        for filename in os.listdir(self.anchor_dir):
            if filename.endswith(".json"):
                file_path = os.path.join(self.anchor_dir, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        anchor = json.load(f)

                        # --- Filter by bucket_id ---
                        if bucket_id:
                            related = anchor.get("related_bucket_ids", [])
                            legacy_bid = anchor.get("bucket_id", "")
                            if bucket_id not in related and bucket_id != legacy_bid:
                                continue

                        # --- Filter by anchor_type ---
                        if anchor_type and anchor.get("anchor_type") != anchor_type:
                            continue

                        # --- Auto-deactivate expired dynamic anchors ---
                        if active_only:
                            if self._check_and_deactivate_if_expired(anchor, file_path):
                                continue  # Skip newly-deactivated anchors

                        # --- Filter by is_active ---
                        if active_only and not anchor.get("is_active", True):
                            continue

                        anchors.append(anchor)
                except Exception as e:
                    logger.warning(f"Failed to load anchor file / 加载锚点文件失败: {file_path}: {e}")

        # --- Sort: active first, then by created desc ---
        anchors.sort(
            key=lambda a: (
                not a.get("is_active", True),  # active first
                a.get("created", ""),  # newer first
            ),
            reverse=False,
        )
        # Re-sort: active=True should come before active=False
        anchors.sort(key=lambda a: not a.get("is_active", True))
        return anchors

    def _check_and_deactivate_if_expired(self, anchor: dict, file_path: str) -> bool:
        """
        Check if a dynamic anchor has expired and deactivate it.
        Returns True if the anchor was just deactivated.

        检查动态锚点是否已过期并失效。返回 True 表示刚刚被失效。
        """
        if anchor.get("anchor_type") != "dynamic":
            return False
        if not anchor.get("is_active", True):
            return False  # Already inactive

        expires_at = anchor.get("expires_at")
        if not expires_at:
            return False

        try:
            expiry = datetime.fromisoformat(expires_at)
            if datetime.now() < expiry:
                return False  # Not expired yet

            # --- Expired: deactivate ---
            anchor["is_active"] = False
            anchor["deactivated_at"] = now_iso()
            anchor["updated"] = now_iso()

            import json
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(anchor, f, ensure_ascii=False, indent=2)

            logger.info(f"Anchor auto-deactivated (expired) / 锚点自动失效（过期）: {anchor.get('id')}")
            return True
        except (ValueError, TypeError):
            return False

    async def activate_anchor(self, anchor_id: str) -> bool:
        """
        Activate an anchor (set is_active=True).
        激活锚点。
        """
        import json
        file_path = os.path.join(self.anchor_dir, f"{anchor_id}.json")
        if not os.path.exists(file_path):
            return False

        with open(file_path, "r", encoding="utf-8") as f:
            anchor = json.load(f)

        anchor["is_active"] = True
        anchor["deactivated_at"] = None
        # Reset expiry if dynamic
        if anchor.get("anchor_type") == "dynamic":
            ttl = anchor.get("ttl_hours", 48.0)
            anchor["expires_at"] = (
                datetime.now() + timedelta(hours=ttl)
            ).isoformat(timespec="seconds")
        anchor["updated"] = now_iso()

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(anchor, f, ensure_ascii=False, indent=2)

        logger.info(f"Anchor activated / 锚点已激活: {anchor_id}")
        return True

    async def deactivate_anchor(self, anchor_id: str) -> bool:
        """
        Deactivate an anchor (set is_active=False).
        Dynamic anchors sink back to regular memory buckets.
        失效锚点。动态锚点沉淀回普通记忆桶。
        """
        import json
        file_path = os.path.join(self.anchor_dir, f"{anchor_id}.json")
        if not os.path.exists(file_path):
            return False

        with open(file_path, "r", encoding="utf-8") as f:
            anchor = json.load(f)

        anchor["is_active"] = False
        anchor["deactivated_at"] = now_iso()
        anchor["updated"] = now_iso()

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(anchor, f, ensure_ascii=False, indent=2)

        logger.info(f"Anchor deactivated / 锚点已失效: {anchor_id}")
        return True

    async def get_anchor_count(self):
        """Get the total number of anchors."""
        count = 0
        for filename in os.listdir(self.anchor_dir):
            if filename.endswith(".json"):
                count += 1
        return count

    async def delete_anchor(self, anchor_id: str):
        """Delete an anchor by ID."""
        file_path = os.path.join(self.anchor_dir, f"{anchor_id}.json")
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Anchor deleted / 删除锚点: {anchor_id}")
            return True
        return False

    # ---------------------------------------------------------
    # Create a new bucket
    # 创建新桶
    # Write content and metadata into a .md file
    # 将内容和元数据写入一个 .md 文件
    # ---------------------------------------------------------
    async def create(
        self,
        content: str,
        tags: list[str] = None,
        importance: int = 5,
        domain: list[str] = None,
        emotions: list[dict] = None,
        dominant_emotion: str = "",
        emotion_metrics: dict = None,
        valence: float = None,
        arousal: float = None,
        bucket_type: str = "dynamic",
        name: str = None,
        pinned: bool = False,
        protected: bool = False,
        task_flag: bool = False,
        dehydrator=None,
        context_metadata: dict = None,
        ttl: int = None,
        status_key: str = None,
        is_private: bool = False,
        privacy_password: str = None,
    ) -> str:
        """
        Create a new memory bucket, return bucket ID.
        创建一个新的记忆桶，返回桶 ID。

        pinned/protected=True: bucket won't be merged, decayed, or have importance changed.
        Importance is locked to 10 for pinned/protected buckets.
        pinned/protected 桶不参与合并与衰减，importance 强制锁定为 10。

        task_flag=True: marks this bucket as a "task/todo" memory.
        When the user is in an emotional/sick/exhausted state, task_flag=True
        buckets are auto-masked to prevent the model from acting like a cold KPI machine.
        task_flag=True：标记为任务类记忆。
        当用户处于情绪化/生病/疲惫状态时，自动屏蔽所有 task_flag=True 的桶，
        防止模型像个冰冷的 KPI 机器一样跑来催任务。

        emotions: list of {"label": str, "intensity": float} dicts
        dominant_emotion: the primary emotion label
        valence/arousal: legacy parameters for backward compatibility
        """
        bucket_id = generate_bucket_id()
        
        if name:
            bucket_name = sanitize_name(name)
        elif dehydrator:
            try:
                analysis = await dehydrator.analyze(content)
                bucket_name = sanitize_name(analysis.get("suggested_name", "")) or self._extract_name_from_content(content)
            except Exception:
                bucket_name = self._extract_name_from_content(content)
        else:
            bucket_name = self._extract_name_from_content(content)
        
        bucket_name = await self._ensure_unique_name(bucket_name, domain, bucket_type)
        
        if bucket_type == "feel":
            domain = domain if domain is not None else []
        else:
            domain = domain or ["未分类"]
        tags = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
        linked_content = content

        if pinned or protected:
            importance = 10

        emotions = emotions or []
        if not emotions and (valence is not None or arousal is not None):
            # Only generate generic labels when NO explicit emotion tags provided.
            # Explicit emotions (from analysis) take priority — otherwise an angry
            # memory would be downgraded to a generic "平静" label.
            # 仅当没有显式情绪标签时才从 valence/arousal 生成通用标签；
            # 显式情绪（分析所得）优先保留，否则"愤怒"会被降级成通用"平静"。
            emotions = self._valence_arousal_to_emotions(
                valence if valence is not None else 0.5,
                arousal if arousal is not None else 0.3,
            )
        
        if not dominant_emotion and emotions:
            dominant_emotion = max(emotions, key=lambda e: e["intensity"])["label"]

        metadata = {
            "id": bucket_id,
            "name": bucket_name,
            "tags": tags,
            "domain": domain,
            "emotions": emotions,
            "dominant_emotion": dominant_emotion,
            "emotion_metrics": emotion_metrics or {},
            "importance": max(1, min(10, safe_int(importance, 5))),
            "importance_details": {
                "impact": 0,
                "duration": 0,
                "emotional_intensity": 0,
                "recurrence": 0,
                "interconnectedness": 0,
            },
            "type": bucket_type,
            "created": now_iso(),
            "last_active": now_iso(),
            "last_accessed": now_iso(),
            "activation_count": 0,
            "related_buckets": [],
            "parent_bucket": None,
            "child_buckets": [],
            "event_sequence": [],
            "valence": valence if valence is not None else 0.5,
            "arousal": arousal if arousal is not None else 0.3,
            "one_line_summary": "",
            "dehydrated_summary": "",
            "task_flag": bool(task_flag),
            "previous_event_id": None,
            "next_event_id": None,
            "context_metadata": context_metadata or {},
            "ttl": ttl,
            "cold_memory": False,
            "status_key": status_key,
            "is_private": bool(is_private),
            "privacy_password": privacy_password or "",
        }
        if pinned:
            metadata["pinned"] = True
        if protected:
            metadata["protected"] = True

        # --- Assemble Markdown file (frontmatter + body) ---
        # --- 组装 Markdown 文件 ---
        post = frontmatter.Post(linked_content, **metadata)

        # --- Choose directory by type + primary domain ---
        # --- 按类型 + 主题域选择存储目录 ---
        if bucket_type == "permanent" or pinned:
            type_dir = self.permanent_dir
            if pinned and bucket_type != "permanent":
                metadata["type"] = "permanent"
        elif bucket_type == "feel":
            type_dir = self.feel_dir
        elif bucket_type == "milestone":
            # Milestone: high-emotional-intensity moments (anniversaries / important events),
            # never decayed, retrieved separately from permanent.
            # milestone：高情绪浓度重要时刻（纪念日/重要事件），永不衰减，检索与 permanent 分离。
            type_dir = self.milestone_dir
        elif bucket_type == "voice":
            # Voice: speech habits / nicknames / interaction style, retrieved on demand.
            # voice：说话习惯、称呼、相处方式；按需检索，不走评分/衰减。
            type_dir = self.voice_dir
        elif bucket_type == "boundary":
            # Boundary: cognitive consensus / logical bottom lines / principles, never decayed,
            # activated at high priority when severe cognitive distortion is detected.
            # boundary：认知共识、逻辑底线与原则；永不衰减，检测到认知偏差时高优先级激活。
            type_dir = self.boundary_dir
        elif bucket_type == "ephemeral":
            # Ephemeral: short-term vent-only stash with a 24h half-life.
            # ephemeral：短期绝密暂存区，半衰期 24 小时；未被再次引用的纯发泄内容在 dream() 中自然蒸发。
            type_dir = self.ephemeral_dir
        elif bucket_type == "pattern":
            # Ring layer (year-ring experiences) lives in ring/ dir.
            # ring 层（年轮经验）存放于 ring/ 目录。
            type_dir = self.ring_dir
        else:
            type_dir = self.dynamic_dir
        if bucket_type == "feel":
            primary_domain = "沉淀物"  # feel subfolder name
        else:
            primary_domain = sanitize_name(domain[0]) if domain else "未分类"
        target_dir = os.path.join(type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)

        # --- Filename: readable_name_bucketID.md (Obsidian friendly) ---
        # --- 文件名：可读名称_桶ID.md ---
        # Note: bucket_name is used for metadata (can contain colons),
        # but filename must be sanitized for file system (no colons on Windows)
        if bucket_name and bucket_name != bucket_id:
            safe_filename = sanitize_filename(bucket_name)
            filename = f"{safe_filename}_{bucket_id}.md"
        else:
            filename = f"{bucket_id}.md"
        file_path = safe_path(target_dir, filename)

        # --- Thread-safe atomic file write / 线程安全的原子文件写入 ---
        with _file_lock:
            try:
                self._atomic_write(file_path, frontmatter.dumps(post))
            except OSError as e:
                logger.error(f"Failed to write bucket file / 写入桶文件失败: {file_path}: {e}")
                raise

        logger.info(
            f"Created bucket / 创建记忆桶: {bucket_id} ({bucket_name}) → {primary_domain}/"
            + (" [PINNED]" if pinned else "") + (" [PROTECTED]" if protected else "")
        )
        
        self._invalidate_cache()
        return bucket_id

    # ---------------------------------------------------------
    # Read bucket content
    # 读取桶内容
    # Returns {"id", "metadata", "content", "path"} or None
    # ---------------------------------------------------------
    async def get(self, bucket_id: str) -> Optional[dict]:
        """
        Read a single bucket by ID.
        根据 ID 读取单个桶。
        """
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None
        return self._load_bucket(file_path)

    # ---------------------------------------------------------
    # Move bucket between directories
    # 在目录间移动桶文件
    # ---------------------------------------------------------
    def _move_bucket(self, file_path: str, target_type_dir: str, domain: list[str] = None) -> str:
        """
        Move a bucket file to a new type directory, preserving domain subfolder.
        Returns new file path.
        """
        primary_domain = sanitize_name(domain[0]) if domain else "未分类"
        target_dir = os.path.join(target_type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)
        filename = os.path.basename(file_path)
        new_path = safe_path(target_dir, filename)
        if os.path.normpath(file_path) != os.path.normpath(new_path):
            os.rename(file_path, new_path)
            logger.info(f"Moved bucket / 移动记忆桶: {filename} → {target_dir}/")
        return new_path

    # ---------------------------------------------------------
    # Atomic file write / 原子化文件写入
    # 所有写桶/改桶操作统一走这里：先写同目录 .tmp 临时文件，
    # 再 os.replace() 原子替换，防止管家后台扫描与前台实时写入
    # 并发产生读写冲突或半截坏文件。
    # ---------------------------------------------------------
    @staticmethod
    def _atomic_write(file_path: str, content: str) -> None:
        """
        Atomic write: dump content to a .tmp file in the same directory,
        then os.replace() it onto the target path.
        原子写入：将内容先写入同目录 .tmp 临时文件，再 os.replace 原子替换目标文件。
        中途失败不会留下半截目标文件；残留临时文件会被清理。
        """
        directory = os.path.dirname(file_path) or "."
        tmp_path = os.path.join(directory, f".{os.path.basename(file_path)}.{os.getpid()}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            # --- Atomic replace: readers see either the old file or the new one ---
            # --- 原子替换：任何读者看到的要么是旧文件、要么是新文件，绝不可能是半截文件 ---
            os.replace(tmp_path, file_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ---------------------------------------------------------
    # Update bucket
    # 更新桶
    # Supports: content, tags, importance, emotions, dominant_emotion, valence, arousal, name, resolved
    # ---------------------------------------------------------
    async def update(self, bucket_id: str, **kwargs) -> bool:
        """
        Update bucket content or metadata fields.
        更新桶的内容或元数据字段。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        # --- Thread-safe read-modify-write cycle / 线程安全的读-改-写循环 ---
        with _file_lock:
            try:
                post = frontmatter.load(file_path)
            except Exception as e:
                logger.warning(f"Failed to load bucket for update / 加载桶失败: {file_path}: {e}")
                return False

            is_pinned = post.get("pinned", False) or post.get("protected", False)
            if is_pinned:
                kwargs.pop("importance", None)

            # --- Full metadata replacement support (read-modify-write flows) ---
            # --- 全量元数据替换支持（读-改-写流程）---
            # Callers pass the whole metadata dict (e.g. manage_record / PUT /api/experiences);
            # every key is written back so nothing is silently dropped.
            if "metadata" in kwargs and isinstance(kwargs["metadata"], dict):
                for mk, mv in kwargs["metadata"].items():
                    if mk in ("id", "content", "metadata"):
                        continue
                    post[mk] = mv

            if "content" in kwargs:
                post.content = kwargs["content"]
            if "tags" in kwargs:
                t = kwargs["tags"]
                post["tags"] = [x for x in t if isinstance(x, str)] if isinstance(t, list) else []
            if "importance" in kwargs:
                post["importance"] = max(1, min(10, safe_int(kwargs["importance"], 5)))
            if "domain" in kwargs:
                post["domain"] = kwargs["domain"]
            if "emotions" in kwargs:
                post["emotions"] = kwargs["emotions"]
            if "dominant_emotion" in kwargs:
                post["dominant_emotion"] = kwargs["dominant_emotion"]
            if "emotion_metrics" in kwargs:
                post["emotion_metrics"] = kwargs["emotion_metrics"]
            if "valence" in kwargs or "arousal" in kwargs:
                v = safe_float(kwargs.get("valence", post.get("valence")), 0.5)
                a = safe_float(kwargs.get("arousal", post.get("arousal")), 0.3)
                # 仅在没有显式情绪标签时才从 valence/arousal 生成通用标签；
                # 显式情绪（分析所得）优先保留，否则"愤怒"会被降级成通用"平静"。
                if "emotions" not in kwargs or not kwargs.get("emotions"):
                    emotions = self._valence_arousal_to_emotions(v, a)
                    post["emotions"] = emotions
                    if not post.get("dominant_emotion") and emotions:
                        post["dominant_emotion"] = max(emotions, key=lambda e: e["intensity"])["label"]
                post["valence"] = v
                post["arousal"] = a
            if "name" in kwargs:
                post["name"] = sanitize_name(kwargs["name"])
            if "resolved" in kwargs:
                # --- Task bucket protection: prevent auto-resolving from dream() ---
                # --- 任务桶保护：防止 dream() 自动解决任务 ---
                # If task_flag=True, require force_resolved=True to set resolved=True
                # 只有显式指定 force_resolved=True 才能解决 task_flag=True 的桶
                task_flag = post.get("task_flag", False)
                new_resolved = safe_bool(kwargs["resolved"], False)
                
                if task_flag and new_resolved and not kwargs.get("force_resolved", False):
                    logger.warning(
                        f"Cannot resolve task bucket without force_resolved / "
                        f"无法在没有 force_resolved 的情况下解决任务桶: {bucket_id}"
                    )
                else:
                    post["resolved"] = new_resolved
                
                # Remove force_resolved to prevent it from being stored as metadata
                # 删除 force_resolved，防止它被存储为元数据
                kwargs.pop("force_resolved", None)
            if "pinned" in kwargs:
                post["pinned"] = safe_bool(kwargs["pinned"], False)
                if kwargs["pinned"]:
                    post["importance"] = 10
            if "digested" in kwargs:
                post["digested"] = safe_bool(kwargs["digested"], False)
            if "task_flag" in kwargs:
                post["task_flag"] = safe_bool(kwargs["task_flag"], False)
            if "decay_stage" in kwargs:
                post["decay_stage"] = safe_int(kwargs["decay_stage"], 0)
            if "model_valence" in kwargs:
                post["model_valence"] = max(0.0, min(1.0, safe_float(kwargs["model_valence"], 0.5)))
            
            for key in ("exp_type", "source", "apply_count", "last_applied", "title", "one_line_summary", "source_bucket_ids", "hit_count", "last_hit", "dehydrated_summary", "previous_event_id", "next_event_id", "superseded_by", "superseded_at", "status", "resolved_reason", "faded", "cold_memory", "efficacy_score", "efficacy_reports", "primary_tags", "sub_tags", "type", "people"):
                if key in kwargs:
                    post[key] = kwargs[key]

            if "is_private" in kwargs:
                post["is_private"] = safe_bool(kwargs["is_private"], False)
            if "privacy_password" in kwargs:
                post["privacy_password"] = kwargs["privacy_password"] or ""

            # --- Auto-refresh activation time / 自动刷新激活时间 ---
            post["last_active"] = now_iso()

            try:
                self._atomic_write(file_path, frontmatter.dumps(post))
            except OSError as e:
                logger.error(f"Failed to write bucket update / 写入桶更新失败: {file_path}: {e}")
                return False

            # --- Auto-move: pinned → permanent/ ---
            # --- 自动移动：钉选 → permanent/ ---
            # NOTE: resolved buckets are NOT auto-archived here.
            # They stay in dynamic/ and decay naturally until score < threshold.
            # 注意：resolved 桶不在此自动归档，留在 dynamic/ 随衰减引擎自然归档。
            domain = post.get("domain", ["未分类"])
            if kwargs.get("pinned") and post.get("type") != "permanent":
                post["type"] = "permanent"
                self._atomic_write(file_path, frontmatter.dumps(post))
                self._move_bucket(file_path, self.permanent_dir, domain)

        logger.info(f"Updated bucket / 更新记忆桶: {bucket_id}")
        
        self._invalidate_cache()
        return True

    # ---------------------------------------------------------
    # Causal chain operations / 因果链操作
    # ---------------------------------------------------------
    async def link_events(self, prev_id: str, next_id: str) -> bool:
        """
        Link two events as cause-effect (bidirectional pointers).
        
        建立两个事件之间的因果关系（双向指针）。
        
        Sets:
          - prev_id.next_event_id = next_id
          - next_id.previous_event_id = prev_id
        
        Also handles existing chains:
          - If prev_id already has a next_event_id (A), that A's previous_event_id
            will be set to None (removed from chain)
          - If next_id already has a previous_event_id (B), that B's next_event_id
            will be set to None (removed from chain)
        
        Args:
            prev_id: The cause event ID (previous in time/sequence)
            next_id: The effect event ID (next in time/sequence)
        
        Returns:
            True if successful, False if either bucket doesn't exist
        """
        # --- Validate both buckets exist ---
        # --- 验证两个桶都存在 ---
        prev_bucket = await self.get(prev_id)
        next_bucket = await self.get(next_id)
        
        if not prev_bucket or not next_bucket:
            logger.warning(
                f"link_events failed: bucket not found / 因果链链接失败：桶不存在: "
                f"prev_id={prev_id} exists={bool(prev_bucket)}, "
                f"next_id={next_id} exists={bool(next_bucket)}"
            )
            return False

        # --- Handle existing chains ---
        # --- 处理已有链条 ---
        # If prev_id already has a next_event_id, break that link
        old_next_id = prev_bucket["metadata"].get("next_event_id")
        if old_next_id and old_next_id != next_id:
            await self.update(old_next_id, previous_event_id=None)
            logger.info(
                f"link_events: broke existing link / 因果链链接：断开原有链接: "
                f"{prev_id} -> {old_next_id} removed"
            )

        # If next_id already has a previous_event_id, break that link
        old_prev_id = next_bucket["metadata"].get("previous_event_id")
        if old_prev_id and old_prev_id != prev_id:
            await self.update(old_prev_id, next_event_id=None)
            logger.info(
                f"link_events: broke existing link / 因果链链接：断开原有链接: "
                f"{old_prev_id} -> {next_id} removed"
            )

        # --- Set bidirectional pointers ---
        # --- 设置双向指针 ---
        await self.update(prev_id, next_event_id=next_id)
        await self.update(next_id, previous_event_id=prev_id)

        logger.info(
            f"link_events: created causal chain / 因果链链接：创建因果关系: "
            f"{prev_id} -> {next_id}"
        )
        return True

    async def get_event_chain(
        self,
        bucket_id: str,
        direction: str = "both",
        max_depth: int = 3,
    ) -> dict:
        """
        Traverse causal chain pointers from a given bucket.
        
        从给定桶遍历因果链指针。
        
        Args:
            bucket_id: The starting bucket ID
            direction: "previous" (forward in time/cause), "next" (backward/effect), or "both"
            max_depth: Maximum number of hops (default: 3)
        
        Returns:
            {
                "current": {"id": str, "name": str, "one_line_summary": str, "created": str},
                "previous": [list of lightweight summaries],
                "next": [list of lightweight summaries],
            }
        
        Note: Skips dangling pointers (buckets that no longer exist) silently.
        注意：静默跳过悬空指针（已删除的桶）。
        """
        bucket = await self.get(bucket_id)
        if not bucket:
            return {"current": None, "previous": [], "next": []}

        current_meta = bucket["metadata"]
        result = {
            "current": {
                "id": bucket_id,
                "name": current_meta.get("name", ""),
                "one_line_summary": current_meta.get("one_line_summary", "") or current_meta.get("dehydrated_summary", "")[:80],
                "created": current_meta.get("created", "")[:10] if current_meta.get("created") else "",
            },
            "previous": [],
            "next": [],
        }

        # --- Traverse previous (cause/earlier events) ---
        # --- 遍历前因（更早的事件）---
        if direction in ("previous", "both"):
            current_id = current_meta.get("previous_event_id")
            depth = 0
            while current_id and depth < max_depth:
                prev_bucket = await self.get(current_id)
                if not prev_bucket:
                    break  # Dangling pointer, stop traversal
                
                prev_meta = prev_bucket["metadata"]
                result["previous"].append({
                    "id": current_id,
                    "name": prev_meta.get("name", ""),
                    "one_line_summary": prev_meta.get("one_line_summary", "") or prev_meta.get("dehydrated_summary", "")[:80],
                    "created": prev_meta.get("created", "")[:10] if prev_meta.get("created") else "",
                })
                
                current_id = prev_meta.get("previous_event_id")
                depth += 1

        # --- Traverse next (effect/later events) ---
        # --- 遍历后果（更晚的事件）---
        if direction in ("next", "both"):
            current_id = current_meta.get("next_event_id")
            depth = 0
            while current_id and depth < max_depth:
                next_bucket = await self.get(current_id)
                if not next_bucket:
                    break  # Dangling pointer, stop traversal
                
                next_meta = next_bucket["metadata"]
                result["next"].append({
                    "id": current_id,
                    "name": next_meta.get("name", ""),
                    "one_line_summary": next_meta.get("one_line_summary", "") or next_meta.get("dehydrated_summary", "")[:80],
                    "created": next_meta.get("created", "")[:10] if next_meta.get("created") else "",
                })
                
                current_id = next_meta.get("next_event_id")
                depth += 1

        return result

    async def add_related_bucket(self, bucket_id: str, related_id: str) -> bool:
        """Add a bidirectional relationship between two buckets."""
        bucket = await self.get(bucket_id)
        related = await self.get(related_id)
        if not bucket or not related:
            return False
        
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        
        # --- Lock the whole bidirectional write (no await inside) ---
        # --- 持锁完成双向写入（临界区无 await）---
        with _file_lock:
            post = frontmatter.load(file_path)
            related_buckets = post.get("related_buckets", [])
            if related_id not in related_buckets:
                related_buckets.append(related_id)
                post["related_buckets"] = related_buckets
            
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
            except OSError as e:
                logger.error(f"Failed to add related bucket: {e}")
                return False
            
            related_path = self._find_bucket_file(related_id)
            if related_path:
                related_post = frontmatter.load(related_path)
                rel_buckets = related_post.get("related_buckets", [])
                if bucket_id not in rel_buckets:
                    rel_buckets.append(bucket_id)
                    related_post["related_buckets"] = rel_buckets
                    with open(related_path, "w", encoding="utf-8") as f:
                        f.write(frontmatter.dumps(related_post))
        
        self._invalidate_cache()
        return True

    async def remove_related_bucket(self, bucket_id: str, related_id: str) -> bool:
        """Remove a bidirectional relationship between two buckets."""
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        
        with _file_lock:
            post = frontmatter.load(file_path)
            related_buckets = post.get("related_buckets", [])
            if related_id in related_buckets:
                related_buckets.remove(related_id)
                post["related_buckets"] = related_buckets
                try:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(frontmatter.dumps(post))
                except OSError as e:
                    logger.error(f"Failed to remove related bucket: {e}")
                    return False
            
            related_path = self._find_bucket_file(related_id)
            if related_path:
                related_post = frontmatter.load(related_path)
                rel_buckets = related_post.get("related_buckets", [])
                if bucket_id in rel_buckets:
                    rel_buckets.remove(bucket_id)
                    related_post["related_buckets"] = rel_buckets
                    with open(related_path, "w", encoding="utf-8") as f:
                        f.write(frontmatter.dumps(related_post))
        
        self._invalidate_cache()
        return True

    async def set_parent_bucket(self, child_id: str, parent_id: str) -> bool:
        """Set a parent-child relationship."""
        child = await self.get(child_id)
        parent = await self.get(parent_id)
        if not child or not parent:
            return False
        
        child_path = self._find_bucket_file(child_id)
        if not child_path:
            return False
        
        with _file_lock:
            post = frontmatter.load(child_path)
            post["parent_bucket"] = parent_id
            child_buckets = post.get("child_buckets", [])
            
            try:
                with open(child_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
            except OSError as e:
                logger.error(f"Failed to set parent bucket: {e}")
                return False
            
            parent_path = self._find_bucket_file(parent_id)
            if parent_path:
                parent_post = frontmatter.load(parent_path)
                parent_children = parent_post.get("child_buckets", [])
                if child_id not in parent_children:
                    parent_children.append(child_id)
                    parent_post["child_buckets"] = parent_children
                    with open(parent_path, "w", encoding="utf-8") as f:
                        f.write(frontmatter.dumps(parent_post))
        
        self._invalidate_cache()
        return True

    async def add_event_sequence(self, bucket_id: str, event_id: str, position: int = None) -> bool:
        """Add an event to the sequence chain."""
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        
        with _file_lock:
            post = frontmatter.load(file_path)
            sequence = post.get("event_sequence", [])
            if event_id not in sequence:
                if position is not None and 0 <= position <= len(sequence):
                    sequence.insert(position, event_id)
                else:
                    sequence.append(event_id)
                post["event_sequence"] = sequence
            
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
            except OSError as e:
                logger.error(f"Failed to add event sequence: {e}")
                return False
        
        self._invalidate_cache()
        return True

    async def update_importance_details(self, bucket_id: str, details: dict) -> bool:
        """Update importance details with multi-dimensional evaluation."""
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        
        post = frontmatter.load(file_path)
        importance_details = post.get("importance_details", {
            "impact": 0, "duration": 0, "emotional_intensity": 0, "recurrence": 0, "interconnectedness": 0
        })
        
        for key in ["impact", "duration", "emotional_intensity", "recurrence", "interconnectedness"]:
            if key in details:
                importance_details[key] = max(0, min(10, safe_int(details[key], 0)))
        
        post["importance_details"] = importance_details
        
        total = sum(importance_details.values())
        average = total / 5 if total > 0 else safe_int(post.get("importance"), 5)
        post["importance"] = max(1, min(10, round(average)))
        
        try:
            self._atomic_write(file_path, frontmatter.dumps(post))
        except OSError as e:
            logger.error(f"Failed to update importance details: {e}")
            return False
        
        self._invalidate_cache()
        return True

    # ---------------------------------------------------------
    # Extract name from content (fallback when LLM doesn't provide one)
    # 从内容中提取名称（当 LLM 未提供名称时的回退）
    # ---------------------------------------------------------
    async def _ensure_unique_name(self, name: str, domain: list, bucket_type: str) -> str:
        """
        Ensure bucket name is globally unique across all buckets.
        If duplicate, append numeric suffix like "名称-2", "名称-3".
        
        确保桶名在全局范围内唯一。如果重复，添加数字后缀如"名称-2"、"名称-3"。
        """
        if not name:
            return name
        
        all_buckets = await self.list_all(include_archive=False)
        
        existing_names = [b.get("metadata", {}).get("name", "") for b in all_buckets]
        
        if name not in existing_names:
            return name
        
        suffix = 2
        while True:
            new_name = f"{name}-{suffix}"
            if new_name not in existing_names:
                return new_name
            suffix += 1
            if suffix > 99:
                return f"{name}-{suffix}"

    def _extract_name_from_content(self, content: str) -> str:
        """
        Extract a meaningful name from content when no name is provided.
        当未提供名称时，从内容中提取有意义的名称。
        
        Priority:
        1. First sentence (max 10 chars)
        2. First line (max 10 chars)
        3. First few characters (max 10 chars)
        
        Returns sanitized name safe for filenames.
        """
        if not content or not content.strip():
            return "未命名"
        
        text = content.strip()
        
        first_sentence = re.match(r'^[^。！？.!?\n]+', text)
        if first_sentence:
            name = first_sentence.group(0).strip()[:10]
            if name:
                return sanitize_name(name)
        
        first_line = text.split('\n')[0].strip()[:10]
        if first_line:
            return sanitize_name(first_line)
        
        return sanitize_name(text[:10]) or "未命名"

    # ---------------------------------------------------------
    # Wikilink injection — DISABLED
    # 自动添加 Obsidian 双链 — 已禁用
    # Now handled by LLM prompts (Gemini adds [[]] for proper nouns)
    # 现在由 LLM prompt 处理（Gemini 对人名/地名/专有名词加 [[]]）
    # ---------------------------------------------------------
    # def _apply_wikilinks(self, content, tags, domain, name): ...
    # def _collect_wikilink_keywords(self, content, tags, domain, name): ...
    # def _normalize_keywords(self, keywords): ...
    # def _extract_auto_keywords(self, content): ...

    # ---------------------------------------------------------
    # Delete bucket
    # 删除桶
    # ---------------------------------------------------------
    async def delete(self, bucket_id: str) -> bool:
        """
        Delete a memory bucket file (move to .trash/ recycle bin, restorable).
        删除指定的记忆桶文件（移入 .trash/ 回收站，可恢复）。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        # --- Lock move + metadata write so touch/update can't resurrect the file ---
        # --- 持锁完成移动+元数据写入，防止 touch/update 并发"复活"已删除文件 ---
        with _file_lock:
            try:
                # --- 移入 .trash/ 回收站，而非直接永久删除 ---
                trash_dir = self._ensure_trash_dir()
                original_name = os.path.basename(file_path)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                trash_filename = f"{timestamp}_{original_name}"
                trash_path = os.path.join(trash_dir, trash_filename)

                shutil.move(file_path, trash_path)

                # --- 保存元数据 JSON：原始路径、删除时间、桶 ID ---
                meta = {
                    "original_path": file_path,
                    "deleted_at": now_iso(),
                    "bucket_id": bucket_id,
                }
                meta_path = os.path.join(trash_dir, os.path.splitext(trash_filename)[0] + ".json")
                self._atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
            except Exception as e:
                logger.error(f"Failed to delete bucket file / 删除桶文件失败: {file_path}: {e}")
                return False

        logger.info(f"Deleted bucket (moved to trash) / 删除记忆桶（已移入回收站）: {bucket_id}")

        self._invalidate_cache()
        return True

    # ---------------------------------------------------------
    # Recycle bin (trash) operations / 回收站操作
    # ---------------------------------------------------------
    def _trash_dir_path(self) -> str:
        """
        Return the .trash/ directory path (sibling of buckets_dir).
        返回 .trash/ 回收站目录路径（与 buckets_dir 同级）。
        """
        parent = os.path.dirname(self.base_dir) or "."
        return os.path.join(parent, ".trash")

    def _ensure_trash_dir(self) -> str:
        """Ensure .trash/ exists, return its path. 确保回收站目录存在并返回路径。"""
        trash_dir = self._trash_dir_path()
        os.makedirs(trash_dir, exist_ok=True)
        return trash_dir

    async def restore(self, trash_filename: str) -> bool:
        """
        Restore a bucket file from .trash/ back to its original location.
        从 .trash/ 回收站恢复桶文件到原始位置。
        """
        trash_dir = self._trash_dir_path()
        if not os.path.exists(trash_dir):
            return False

        # --- 清理文件名，防止路径穿越 ---
        safe_name = os.path.basename(trash_filename)
        trash_path = os.path.join(trash_dir, safe_name)
        if not os.path.exists(trash_path):
            return False

        # --- 读取元数据，获取原始路径与桶 ID ---
        meta_path = os.path.join(trash_dir, os.path.splitext(safe_name)[0] + ".json")
        original_path = None
        bucket_id = None
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                original_path = meta.get("original_path")
                bucket_id = meta.get("bucket_id")
            except Exception as e:
                logger.warning(f"Failed to read trash metadata / 读取回收站元数据失败: {safe_name}: {e}")

        # --- 确定恢复目标；原目录缺失时回退到 permanent 目录 ---
        if original_path and os.path.isdir(os.path.dirname(original_path)):
            restore_path = original_path
        else:
            restore_path = os.path.join(self.permanent_dir, safe_name)

        # --- 避免文件名冲突 ---
        if os.path.exists(restore_path):
            base, ext = os.path.splitext(restore_path)
            restore_path = f"{base}_restored{ext}"

        # --- Lock the move so concurrent touch can't write to the old path ---
        # --- 持锁移动，防止并发 touch 向旧路径写文件 ---
        with _file_lock:
            try:
                shutil.move(trash_path, restore_path)
                # --- 恢复成功后删除元数据 JSON ---
                if os.path.exists(meta_path):
                    os.remove(meta_path)
            except Exception as e:
                logger.error(f"Failed to restore bucket / 恢复桶失败: {safe_name}: {e}")
                return False

        logger.info(f"Restored bucket / 恢复记忆桶: {bucket_id or safe_name}")
        self._invalidate_cache()
        return True

    def list_trash(self) -> list:
        """
        List all trashed bucket files with their deletion metadata.
        列出回收站中所有桶文件及其删除元数据。
        """
        trash_dir = self._trash_dir_path()
        if not os.path.exists(trash_dir):
            return []

        result = []
        for fname in sorted(os.listdir(trash_dir)):
            if not fname.endswith(".md"):
                continue
            meta_path = os.path.join(trash_dir, os.path.splitext(fname)[0] + ".json")

            deleted_at = None
            bucket_id = None
            original_name = None
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    deleted_at = meta.get("deleted_at")
                    bucket_id = meta.get("bucket_id")
                    orig = meta.get("original_path")
                    if orig:
                        original_name = os.path.basename(orig)
                except Exception as e:
                    logger.warning(f"Failed to read trash metadata / 读取回收站元数据失败: {meta_path}: {e}")

            # --- 回退：从文件名时间戳前缀解析删除时间 ---
            if not deleted_at:
                try:
                    ts = fname[:15]  # YYYYMMDD_HHMMSS
                    if len(ts) == 15 and ts[:8].isdigit():
                        deleted_at = datetime.strptime(ts, "%Y%m%d_%H%M%S").isoformat()
                except Exception:
                    deleted_at = None

            # --- 回退：去掉时间戳前缀得到原始文件名 ---
            if not original_name:
                if len(fname) > 16 and fname[:8].isdigit() and fname[8] == "_":
                    original_name = fname[16:]
                else:
                    original_name = fname

            result.append({
                "filename": fname,
                "deleted_at": deleted_at,
                "bucket_id": bucket_id,
                "original_name": original_name,
            })

        return result

    def cleanup_trash(self, max_age_hours: int = 24) -> int:
        """
        Permanently delete trash files older than max_age_hours.
        永久删除超过 max_age_hours 的回收站文件，返回已删除数量。
        """
        trash_dir = self._trash_dir_path()
        if not os.path.exists(trash_dir):
            return 0

        now = datetime.now()
        cutoff = now - timedelta(hours=max_age_hours)
        deleted_count = 0

        for fname in list(os.listdir(trash_dir)):
            fpath = os.path.join(trash_dir, fname)

            # --- 确定删除时间：优先用文件名时间戳前缀，回退到 mtime ---
            delete_time = None
            try:
                ts = fname[:15]  # YYYYMMDD_HHMMSS
                if len(ts) == 15 and ts[:8].isdigit():
                    delete_time = datetime.strptime(ts, "%Y%m%d_%H%M%S")
            except Exception:
                delete_time = None

            if delete_time is None:
                try:
                    delete_time = datetime.fromtimestamp(os.path.getmtime(fpath))
                except OSError:
                    continue

            if delete_time < cutoff:
                try:
                    os.remove(fpath)
                    deleted_count += 1
                except OSError as e:
                    logger.warning(f"Failed to cleanup trash file / 清理回收站文件失败: {fname}: {e}")

        logger.info(
            f"Cleaned up {deleted_count} trash files older than {max_age_hours}h / "
            f"清理了 {deleted_count} 个超过 {max_age_hours} 小时的回收站文件"
        )
        return deleted_count

    # ---------------------------------------------------------
    # Touch bucket (refresh activation time + increment count)
    # 触碰桶（刷新激活时间 + 累加激活次数）
    # Called on every recall hit; affects decay score.
    # 每次检索命中时调用，影响衰减得分。
    # ---------------------------------------------------------
    async def touch(self, bucket_id: str) -> None:
        """
        Update a bucket's last activation time and count.
        Also triggers time ripple: nearby memories get a slight activation boost.
        更新桶的最后激活时间和激活次数。
        同时触发时间涟漪：时间上相邻的记忆轻微唤醒。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return

        # --- Hold lock for the read-modify-write cycle ---
        # --- 持锁完成读-改-写循环，防止与 update() 并发导致 lost update ---
        created_str = ""
        with _file_lock:
            try:
                post = frontmatter.load(file_path)
                post["last_active"] = now_iso()
                post["last_accessed"] = now_iso()
                post["activation_count"] = safe_int(post.get("activation_count"), 0) + 1
                post["cold_memory"] = False
                created_str = str(post.get("created", post.get("last_active", "")))

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
            except Exception as e:
                logger.warning(f"Failed to touch bucket / 触碰桶失败: {bucket_id}: {e}")
                return

        # --- Time ripple: boost nearby memories within ±48h (lock released) ---
        # --- 时间涟漪：±48小时内的记忆轻微唤醒（锁已释放）---
        try:
            current_time = datetime.fromisoformat(created_str)
            await self._time_ripple(bucket_id, current_time)
        except (ValueError, TypeError):
            pass

        self._invalidate_cache()

    # ---------------------------------------------------------
    # Cooldown management for pattern/experience injection
    # 年轮经验注入冷却管理
    # ---------------------------------------------------------
    def check_cooldown(self, bucket_id: str) -> tuple[bool, float]:
        """
        Check if a bucket is in cooldown and return decay factor.
        检查桶是否在冷却期内，返回 (是否在冷却期内, 降权系数)。

        Returns:
            (in_cooldown, weight_factor)
            - in_cooldown: True if within cooldown period
            - weight_factor: 1.0 if not in cooldown, decay_factor if in cooldown
        """
        import time
        last_injected = self._injection_history.get(bucket_id)
        if last_injected is None:
            return False, 1.0

        elapsed = time.time() - last_injected
        if elapsed >= self.cooldown_seconds:
            return False, 1.0

        # In cooldown: calculate proportional decay
        # 冷却期内：按时间比例计算降权系数
        remaining_ratio = 1.0 - (elapsed / self.cooldown_seconds)
        weight_factor = 1.0 - remaining_ratio * (1.0 - self.cooldown_decay_factor)
        return True, weight_factor

    def record_injection(self, bucket_id: str) -> None:
        """Record that a bucket was injected into prompt (updates cooldown timer).
        记录桶被注入到 Prompt（更新冷却计时器）。"""
        import time
        self._injection_history[bucket_id] = time.time()

    def check_similarity_threshold(self, bucket_id: str, query: str, vector_sim_map: dict = None) -> bool:
        """
        Check if a bucket meets the similarity threshold for injection.
        检查桶是否达到注入的语义相似度阈值。

        Args:
            bucket_id: target bucket
            query: current prompt query
            vector_sim_map: optional pre-computed {bucket_id: similarity} map

        Returns:
            True if similarity >= threshold (or no query / no embedding available)
        """
        if not query or not query.strip():
            return True  # No query = float mode, no threshold check

        if vector_sim_map is not None:
            sim = vector_sim_map.get(bucket_id, 0.0)
            return sim >= self.similarity_threshold

        return True  # No vector map available, allow injection

    async def _time_ripple(self, source_id: str, reference_time: datetime, hours: float = 48.0) -> None:
        """
        Slightly boost activation_count of buckets created/activated near the reference time.
        轻微提升时间相邻桶的激活次数（+0.3），不改 last_active 避免递归唤醒。
        Max 5 buckets rippled per touch to bound I/O.
        """
        try:
            all_buckets = await self.list_all(include_archive=False)
        except Exception as e:
            logger.warning(f"Time ripple: list_all failed / 时间涟漪列出桶失败: {e}")
            return

        rippled = 0
        max_ripple = 5
        for bucket in all_buckets:
            if rippled >= max_ripple:
                break
            if bucket["id"] == source_id:
                continue
            meta = bucket.get("metadata", {})
            # Skip pinned/permanent/feel
            if meta.get("pinned") or meta.get("protected") or meta.get("type") in ("permanent", "feel"):
                continue

            created_str = meta.get("created", meta.get("last_active", ""))
            try:
                created = datetime.fromisoformat(str(created_str))
                delta_hours = abs((reference_time - created).total_seconds()) / 3600
            except (ValueError, TypeError):
                continue

            if delta_hours <= hours:
                # Boost activation_count by 0.3 (fractional), don't change last_active
                file_path = self._find_bucket_file(bucket["id"])
                if not file_path:
                    continue
                try:
                    # --- Lock each ripple write (small read-modify-write) ---
                    # --- 每次涟漪写入都持锁（短读-改-写）---
                    with _file_lock:
                        post = frontmatter.load(file_path)
                        current_count = safe_float(post.get("activation_count"), 1.0)
                        # Store as float for fractional increments; calculate_score handles it
                        post["activation_count"] = round(current_count + 0.3, 1)
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(frontmatter.dumps(post))
                    rippled += 1
                except Exception as e:
                    logger.warning(f"Time ripple write failed / 时间涟漪写入失败: {bucket['id']}: {e}")
                    continue

        self._invalidate_cache()

    # ---------------------------------------------------------
    # Multi-dimensional search (core feature)
    # 多维搜索（核心功能）
    #
    # Strategy: domain pre-filter → weighted multi-dim ranking
    # 策略：主题域预筛 → 多维加权精排
    #
    # Ranking formula:
    #   total = topic(×w_topic) + emotion(×w_emotion)
    #           + time(×w_time)
    #
    # Multi-dimensional continuous scoring system:
    #   Final_Score = (W1 * Emotion_Arousal) + (W2 * Explicit_Priority) + (W3 * Vector_Similarity) + (W4 * Topic_Relevance) + (W5 * Time_Proximity)
    #
    # Where:
    #   Emotion_Arousal     = 情绪唤醒度 (0.0~1.0 continuous)
    #   Explicit_Priority   = 显式优先级 (0 or 1, 钉选/保护)
    #   Vector_Similarity   = 向量语义相似度 (0.0~1.0 continuous)
    #   Topic_Relevance     = 主题相关性 (0.0~1.0 continuous)
    #   Time_Proximity      = 时间亲近度 (0.0~1.0 continuous)
    # ---------------------------------------------------------
    # ---------------------------------------------------------
    # Query-side tag synonym normalization / 查询端标签同义归一化
    # Uses the synonym map persisted by tag_normalizer (tag_synonyms.json)
    # to expand the query so synonyms match (e.g. 跑步 ↔ 慢跑 ↔ 健走).
    # 复用 tag_normalizer 持久化的同义词映射（tag_synonyms.json）扩展查询，
    # 使同义词可互相命中（如 跑步 ↔ 慢跑 ↔ 健走）。
    # ---------------------------------------------------------
    def _load_tag_synonyms(self) -> dict:
        """Lazily load the persisted tag synonym map (cached per process).
        懒加载持久化的标签同义词映射（进程内缓存）。"""
        if self._tag_synonyms is not None:
            return self._tag_synonyms
        try:
            if os.path.exists(self._tag_synonyms_file):
                with open(self._tag_synonyms_file, "r", encoding="utf-8") as f:
                    self._tag_synonyms = json.load(f)
            else:
                self._tag_synonyms = {}
        except Exception as e:
            logger.warning(f"Failed to load tag synonyms / 读取标签同义词失败: {e}")
            self._tag_synonyms = {}
        return self._tag_synonyms

    def _expand_query_synonyms(self, query: str) -> str:
        """
        Expand the query with tag synonyms. For each tag-like token found in the
        synonym map, append its canonical tag and all synonyms sharing that
        canonical tag. Non-matching tokens are kept as-is.
        用同义词扩展查询词：对命中映射的标签词，追加其泛化标签及所有共享
        该泛化标签的同义词；无映射的词原样保留。
        """
        if not query or not query.strip():
            return query
        syn_map = self._load_tag_synonyms()
        if not syn_map:
            return query

        tokens = [t for t in re.split(r"[\s,，、]+", query.strip()) if t]
        if not tokens:
            return query

        expanded = list(tokens)
        for tok in tokens:
            canonical = syn_map.get(tok)
            if not canonical or canonical == tok:
                continue
            if canonical not in expanded:
                expanded.append(canonical)
            for k, v in syn_map.items():
                if v == canonical and k not in expanded:
                    expanded.append(k)

        if len(expanded) == len(tokens):
            return query
        return " ".join(expanded)

    async def search(
        self,
        query: str,
        limit: int = None,
        domain_filter: list[str] = None,
        query_valence: float = None,
        query_arousal: float = None,
        mask_tasks: bool = False,
        force_keyword: bool = False,
    ) -> list[dict]:
        """
        Multi-dimensional indexed search for memory buckets.
        多维索引搜索记忆桶。

        domain_filter: pre-filter by domain (None = search all)
        query_valence/arousal: emotion coordinates for resonance scoring
        mask_tasks: If True, filter out task_flag=True buckets
                    (used when user is in a vulnerable state).
                    当用户处于脆弱状态时设为 True，屏蔽任务桶。
        force_keyword: If True, force exact keyword matching mode
        """
        if not query or not query.strip():
            return []

        # --- Query-side tag synonym normalization / 查询端标签同义归一化 ---
        # Searching "跑步" also matches memories tagged "慢跑"/"健走" (synonyms
        # persisted by tag_normalizer that share the same canonical tag).
        # 搜"跑步"也能命中"慢跑"/"健走"等映射到同一泛化标签的同义词记忆。
        query = self._expand_query_synonyms(query)

        limit = limit or self.max_results
        all_buckets = await self.list_all(include_archive=False)

        if not all_buckets:
            return []

        # --- Mask task_flag buckets if requested ---
        # --- 屏蔽任务类桶（如用户生病/疲惫/情绪化）---
        if mask_tasks:
            all_buckets = self._mask_task_buckets(all_buckets)

        # --- Layer 0: exclude superseded / expired buckets ---
        # --- 第0层：排除已被取代（版本控制 status=superseded / superseded_by）的失效旧桶 ---
        # Voice buckets are retrievable on demand (no longer force-injected).
        # voice 桶可被按需检索（已取消开局强制注入）。
        all_buckets = [
            b for b in all_buckets
            if b["metadata"].get("superseded_by") is None
            and b["metadata"].get("status") != "superseded"
        ]

        # --- Layer 1: domain pre-filter (fast scope reduction) ---
        # --- 第一层：主题域预筛（快速缩小范围）---
        candidates = all_buckets
        if domain_filter:
            filter_set = {d.lower() for d in domain_filter}
            candidates = [
                b for b in all_buckets
                if {d.lower() for d in as_list(b["metadata"].get("domain"))} & filter_set
            ]
            # Fall back to full search if pre-filter yields nothing
            # 预筛为空则回退全量搜索
            if not candidates:
                candidates = all_buckets
        else:
            candidates = all_buckets

        # --- Hybrid Search Strategy (BM25 + Vector + Rerank) ---
        # --- 混合检索策略（BM25关键词 + 向量语义 + Rerank重排序）---
        if self.hybrid_search and self.hybrid_search.enabled:
            try:
                results = await self.hybrid_search.search(query, candidates, limit=limit, force_keyword=force_keyword)
                return results
            except Exception as e:
                logger.warning(f"Hybrid search failed, falling back to legacy search: {e}")
        
        # --- Legacy search fallback ---
        # --- 传统搜索降级方案 ---
        is_exact_query = force_keyword or self._is_exact_match_query(query)
        
        # --- Layer 1.5: embedding pre-filter (optional, reduces multi-dim ranking set) ---
        # --- 第1.5层：embedding 预筛（可选，缩小精排候选集）---
        # For exact queries, skip embedding pre-filter to avoid missing exact matches
        vector_similarity_map = {}
        if self.embedding_engine and self.embedding_engine.enabled and not is_exact_query:
            try:
                vector_results = await self.embedding_engine.search_similar(query, top_k=50)
                if vector_results:
                    vector_similarity_map = {bid: score for bid, score in vector_results}
                    vector_ids = {bid for bid, _ in vector_results}
                    emb_candidates = [b for b in candidates if b["id"] in vector_ids]
                    if emb_candidates:
                        candidates = emb_candidates
            except Exception as e:
                logger.warning(f"Embedding pre-filter failed, using fuzzy only / embedding 预筛失败: {e}")

        # --- Layer 2: Multi-dimensional continuous scoring system ---
        # --- 第二层：多维连续评分系统 ---
        # Hybrid Search: For exact queries, use exact keyword matching with higher weight
        #                For semantic queries, use fuzzy + vector similarity
        scored = []
        for bucket in candidates:
            meta = bucket.get("metadata", {})

            try:
                # Dim 1: Emotion Arousal (0.0~1.0 continuous)
                # 情绪唤醒度：越高越优先
                emotion_arousal = self._calc_emotion_arousal_score(meta)

                # Dim 2: Explicit Priority (0 or 1)
                # 显式优先级：钉选/保护为1，否则为0
                explicit_priority = 1.0 if (safe_bool(meta.get("pinned")) or safe_bool(meta.get("protected"))) else 0.0

                # Dim 3: Vector Similarity (0.0~1.0 continuous)
                # 向量语义相似度
                vector_similarity = vector_similarity_map.get(bucket["id"], 0.0)

                # Dim 4: Topic Relevance / Exact Keyword Match (0.0~1.0 continuous)
                # 主题相关性 / 精确关键字匹配
                if is_exact_query:
                    topic_score = self._exact_keyword_match(query, bucket)
                else:
                    topic_score = self._calc_topic_score(query, bucket)

                # Dim 5: Time Proximity (0.0~1.0 continuous)
                # 时间亲近度：越近时间的记忆优先
                time_score = self._calc_time_score(meta)

                # --- Final weighted score calculation ---
                # --- 最终加权得分计算 ---
                # Hybrid Search weights:
                #   Exact query mode: reduce vector weight, increase keyword weight
                #   Semantic query mode: use normal weights
                if is_exact_query:
                    w_emotion = self.w_emotion_arousal
                    w_priority = self.w_explicit_priority
                    w_vector = self.w_vector_similarity * 0.2
                    w_topic = self.w_topic * 2.0
                    w_time = self.w_time
                else:
                    w_emotion = self.w_emotion_arousal
                    w_priority = self.w_explicit_priority
                    w_vector = self.w_vector_similarity
                    w_topic = self.w_topic
                    w_time = self.w_time
                
                total_weight = w_emotion + w_priority + w_vector + w_topic + w_time
                
                raw_score = (
                    emotion_arousal * w_emotion
                    + explicit_priority * w_priority
                    + vector_similarity * w_vector
                    + topic_score * w_topic
                    + time_score * w_time
                )

                # --- Topic-relevance priority: low Topic/Vector match must not be
                # --- hard-pushed to the top by high Priority weight ---
                # --- 主题相关度优先：匹配度极低时，高 Priority 不能硬性推送 Top ---
                if topic_score < 0.05 and vector_similarity < 0.1:
                    raw_score *= 0.2
                
                # Normalize to [0, 1] range
                # 归一化到 [0, 1] 区间
                final_score = raw_score / total_weight if total_weight > 0 else 0.0

                # Store individual dimension scores for analysis
                bucket["dimensions"] = {
                    "emotion_arousal": round(emotion_arousal, 3),
                    "explicit_priority": explicit_priority,
                    "vector_similarity": round(vector_similarity, 3),
                    "topic_relevance": round(topic_score, 3),
                    "time_proximity": round(time_score, 3),
                    "search_mode": "exact" if is_exact_query else "semantic",
                }

                # Threshold check uses normalized score so resolved buckets
                # remain reachable by keyword
                # 使用归一化后的得分进行阈值检查
                # For exact queries, use higher threshold to filter noise
                normalized_threshold = 0.2 if is_exact_query else 0.1
                if final_score >= normalized_threshold:
                    if meta.get("resolved", False):
                        final_score *= 0.3
                    bucket["score"] = round(final_score, 4)
                    scored.append(bucket)
            except Exception as e:
                logger.warning(
                    f"Scoring failed for bucket {bucket.get('id', '?')} / "
                    f"桶评分失败: {e}"
                )
                continue

        # Sort by final_score (continuous, almost no collisions)
        scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        return scored[:limit]

    # ---------------------------------------------------------
    # Topic relevance sub-score:
    # name(×3) + domain(×2.5) + tags(×2) + body(×1)
    # 文本相关性子分：桶名(×3) + 主题域(×2.5) + 标签(×2) + 正文(×1)
    # ---------------------------------------------------------
    def _calc_topic_score(self, query: str, bucket: dict) -> float:
        """
        Calculate text dimension relevance score (0~1).
        计算文本维度的相关性得分。
        """
        meta = bucket.get("metadata", {})

        name_score = fuzz.partial_ratio(query, meta.get("name", "")) * 3
        domain_score = (
            max(
                (fuzz.partial_ratio(query, d) for d in as_list(meta.get("domain"))),
                default=0,
            )
            * 2.5
        )
        tag_score = (
            max(
                (fuzz.partial_ratio(query, tag) for tag in as_list(meta.get("tags"))),
                default=0,
            )
            * 2
        )
        # --- Primary tag gets extra boost: closed vocabulary, high precision ---
        # --- 主标签额外加分：封闭词表、精确度高 ---
        primary_tag_score = (
            max(
                (fuzz.partial_ratio(query, t) for t in as_list(meta.get("primary_tags"))),
                default=0,
            )
            * 1.5
        )
        content_score = fuzz.partial_ratio(query, bucket.get("content", "")[:1000]) * self.content_weight

        return (name_score + domain_score + tag_score + primary_tag_score + content_score) / (100 * (3 + 2.5 + 2 + 1.5 + self.content_weight))

    def _is_exact_match_query(self, query: str) -> bool:
        """
        Detect if query requires exact keyword matching (vs semantic matching).
        判断查询是否需要精确关键字匹配（而非语义匹配）。
        
        Triggers for exact matching:
        - Short queries (1-2 chars): e.g., "烛台", "名册", "0426"
        - Numeric patterns: dates "2026-03", IDs "abc123", codes "04261221"
        - Special keywords: "烛台", "名册", "年轮", "锚点", "时间链", "模式", "经验", "感受"
        - Wikilink patterns: [[...]]
        """
        q = query.strip()
        
        if len(q) <= 2:
            return True
        
        if re.match(r'^\d{4}-\d{2}(-\d{2})?$', q):
            return True
        
        if re.match(r'^[\da-fA-F]{4,}$', q):
            return True
        
        exact_keywords = {"烛台", "名册", "年轮", "锚点", "时间链", "模式", "经验", "感受", "记忆", "事件"}
        if q in exact_keywords:
            return True
        
        if q.startswith("[[") and q.endswith("]]"):
            return True
        
        return False

    def _exact_keyword_match(self, query: str, bucket: dict) -> float:
        """
        Exact keyword matching score (0~1).
        精确关键字匹配得分（0~1）。
        
        Checks: name, domain, tags, content for exact substring matches.
        检查：名称、主题域、标签、正文中的精确子串匹配。
        """
        q = query.lower().strip()
        if not q:
            return 0.0

        meta = bucket.get("metadata", {})
        score = 0.0
        matches = 0

        name = meta.get("name", "").lower()
        if q in name:
            score += 3.0
            matches += 1

        for d in as_list(meta.get("domain")):
            if q in d.lower():
                score += 2.5
                matches += 1

        for tag in as_list(meta.get("tags")):
            if q in tag.lower():
                score += 2.0
                matches += 1

        content = bucket.get("content", "").lower()[:2000]
        if q in content:
            score += 1.5
            matches += 1

        if matches == 0:
            return 0.0
        return min(1.0, score / 3.0)

    # ---------------------------------------------------------
    # Rerank + Top-N truncation (Step 3 context injection gate)
    # 精排 + Top-N 截断（Step 3 上下文注入闸门）
    # ---------------------------------------------------------
    def rerank_top_n(self, candidates: list, query: str, top_n: int = 3) -> list:
        """
        Lightweight rerank + hard Top-N truncation for Step 3 search results.
        No external CrossEncoder model is loaded (avoids OOM in small containers);
        the rerank signal blends the five-dimension Final_Score with a re-computed
        lexical/keyword relevance, then hard-caps the result at Top-N.

        轻量级精排 + 硬性 Top-N 截断（Step 3 检索结果注入前使用）。
        不加载外部跨编码器模型（避免小内存容器 OOM）；
        精排得分 = 五维 Final_Score 与重新计算的词面/关键词相关性信号混合，
        随后按精排得分从高到低硬性截断为 Top-N，宁缺毋滥。

        Args:
            candidates: scored buckets from search() (each has 'score' + 'metadata').
            query: the search query / 检索查询词。
            top_n: max number of buckets to keep (default 3) / 保留条数上限（默认 3）。

        Returns:
            Sorted, truncated list of buckets. Buckets are mutated in place with
            'rerank_score'. 返回按精排得分降序截断后的列表，并在原桶写入 rerank_score。
        """
        if not candidates:
            return []
        top_n = max(1, int(top_n))
        q = (query or "").strip()
        if not q:
            # No query → keep existing order, just truncate
            # 无查询词时保持原顺序，仅做截断
            return sorted(
                candidates, key=lambda b: b.get("score", 0.0), reverse=True
            )[:top_n]

        reranked = []
        for bucket in candidates:
            raw = bucket.get("score", 0.0)
            # Normalize legacy 0~100 scores to 0~1
            # 兼容旧式 0~100 分制，统一归一化到 0~1
            base_score = (raw / 100.0) if raw > 1 else raw

            # Lexical relevance signal (0~1): exact keyword match first, topic as fallback
            # 词面相关信号（0~1）：优先精确关键词匹配，无匹配则回退主题相关度
            lexical = self._exact_keyword_match(q, bucket)
            if lexical <= 0.0:
                lexical = self._calc_topic_score(q, bucket)

            # Blend: 70% five-dim score + 30% lexical signal (pure deterministic calc)
            # 混合：70% 五维得分 + 30% 词面相关信号（纯确定性计算，不依赖模型）
            rerank_score = 0.7 * base_score + 0.3 * lexical
            bucket["rerank_score"] = round(rerank_score, 4)
            reranked.append(bucket)

        reranked.sort(key=lambda b: b.get("rerank_score", 0.0), reverse=True)
        return reranked[:top_n]

    # ---------------------------------------------------------
    # Emotion intensity score:
    # Measures emotion fluctuation degree (0~1)
    # 情感强度子分：衡量情绪波动程度
    # Uses new emotion_metrics if available, otherwise falls back to arousal/valence
    # ---------------------------------------------------------
    def _calc_emotion_intensity_score(self, meta: dict) -> float:
        """
        Calculate emotion intensity/fluctuation score (0~1, higher = more intense).
        计算情绪波动强度得分（0~1，越高波动越强）。
        优先使用 emotion_metrics 中的 overall_intensity，否则回退到 arousal/valence 计算。
        """
        try:
            emotion_metrics = meta.get("emotion_metrics", {})
            if isinstance(emotion_metrics, dict) and "overall_intensity" in emotion_metrics:
                return float(emotion_metrics.get("overall_intensity", 0.3))
            
            if "emotions" in meta and meta["emotions"]:
                emotions = meta["emotions"]
                total_intensity = sum(float(e.get("intensity", 0.0)) for e in emotions)
                return min(1.0, total_intensity / len(emotions))
            
            arousal = float(meta.get("arousal", 0.3))
            valence = float(meta.get("valence", 0.5))
            intensity = arousal * (1.0 + abs(valence - 0.5))
            return min(1.0, intensity)
        except (ValueError, TypeError):
            return 0.3

    def _calc_emotion_arousal_score(self, meta: dict) -> float:
        """
        Calculate emotion arousal score (0.0~1.0 continuous).
        计算情绪唤醒度得分，越高表示情绪越强烈/激动。
        优先使用 emotion_metrics 中的 arousal，否则使用 emotions 数组的最大强度，最后回退到 arousal 字段。
        """
        try:
            emotion_metrics = meta.get("emotion_metrics", {})
            if isinstance(emotion_metrics, dict) and "arousal" in emotion_metrics:
                return max(0.0, min(1.0, float(emotion_metrics.get("arousal", 0.3))))

            if "emotions" in meta and meta["emotions"]:
                emotions = [e for e in meta["emotions"] if isinstance(e, dict)]
                if emotions:
                    max_intensity = max(safe_float(e.get("intensity"), 0.0) for e in emotions)
                    return max(0.0, min(1.0, max_intensity))

            arousal = safe_float(meta.get("arousal"), 0.3)
            return max(0.0, min(1.0, arousal))
        except Exception:
            return 0.3

    def get_emotion_arousal(self, meta: dict) -> float:
        """Public API for emotion arousal score (0.0~1.0)."""
        return self._calc_emotion_arousal_score(meta)

    # ---------------------------------------------------------
    # Three-step retrieval pipeline / 三步检索管线
    # ---------------------------------------------------------
    async def retrieve_strong_anchors(self, query: str = "", mask_tasks: bool = False) -> list[dict]:
        """
        Step 1: Strong anchor retrieval.
        Returns pinned/protected memories ONLY (static rules).
        
        步骤1：强锚点检索。
        仅返回钉选/保护记忆（静态规则）。
        优先级最高，原样注入（不脱水）。
        
        注意：不使用唤醒度 > 0.8 的动态判断，避免在检索第一步就触发计算阻塞。
        强锚点必须是人工显式指定的最高权重记忆。

        Args:
            mask_tasks: If True, filter out task_flag=True buckets.
        """
        all_buckets = await self.list_all(include_archive=False)

        if mask_tasks:
            all_buckets = self._mask_task_buckets(all_buckets)

        strong_anchors = []
        anchor_ids = set()

        for b in all_buckets:
            meta = b["metadata"]
            if safe_bool(meta.get("pinned")) or safe_bool(meta.get("protected")):
                strong_anchors.append(b)
                anchor_ids.add(b["id"])

        return strong_anchors

    async def retrieve_top_experiences(self, query: str, top_n: int = 3, mask_tasks: bool = False) -> list[dict]:
        """
        Step 2: Experience extraction.
        Returns top-N experiences semantically related to the query.

        步骤2：年轮经验提取。
        返回与当前 Prompt 语义相关的 TOP-N 经验。
        高权重，提炼后注入。

        Args:
            mask_tasks: If True, filter out task_flag=True experiences.
                        当用户处于脆弱状态时设置为 True。
        """
        all_buckets = await self.list_all(include_archive=False)

        # --- Mask task_flag buckets if requested ---
        # --- 屏蔽任务类桶（如用户生病/疲惫/情绪化）---
        if mask_tasks:
            all_buckets = self._mask_task_buckets(all_buckets)

        experiences = []
        for b in all_buckets:
            meta = b["metadata"]
            if meta.get("type") in ("experience", "pattern"):
                experiences.append(b)
            elif "经验" in (meta.get("domain") or []):
                experiences.append(b)

        if not experiences:
            return []

        if not query or not query.strip():
            experiences.sort(key=lambda e: e["metadata"].get("created", ""), reverse=True)
            return experiences[:top_n]

        scored = []
        for exp in experiences:
            topic_score = self._calc_topic_score(query, exp)
            in_cooldown, weight_factor = self.check_cooldown(exp["id"])
            if in_cooldown and weight_factor < 0.3:
                continue
            final_score = topic_score * weight_factor
            scored.append((final_score, exp))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [exp for _, exp in scored[:top_n]]

    async def retrieve_hybrid_buckets(
        self,
        query: str,
        top_n: int = 10,
        exclude_ids: set = None,
        domain_filter: list = None,
        mask_tasks: bool = False,
    ) -> tuple[list[dict], list[dict]]:
        """
        Step 3: Hybrid memory bucket retrieval.
        Returns (full_content_buckets, summary_only_buckets).

        步骤3：记忆桶混合检索。
        返回 (完整内容桶列表, 仅摘要桶列表)。
        TOP-N 桶返回完整内容，其余仅返回 one_line_summary。

        Args:
            mask_tasks: If True, filter out task_flag=True buckets.
                        当用户处于脆弱状态时设置为 True。
        """
        exclude_ids = exclude_ids or set()

        if query and query.strip():
            matches = await self.search(
                query,
                limit=max(top_n * 3, 30),
                domain_filter=domain_filter,
                mask_tasks=mask_tasks,
            )
        else:
            all_buckets = await self.list_all(include_archive=False)
            if mask_tasks:
                all_buckets = self._mask_task_buckets(all_buckets)
            matches = [
                b for b in all_buckets
                if not b["metadata"].get("resolved")
                and b["metadata"].get("type") not in ("feel", "identity", "pattern", "experience", "candlestick")
                and not b["metadata"].get("pinned")
                and not b["metadata"].get("protected")
            ]
            matches.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)

        matches = [b for b in matches if b["id"] not in exclude_ids]

        if domain_filter:
            matches = [
                b for b in matches
                if any(d in (b["metadata"].get("domain") or []) for d in domain_filter)
            ]

        full_buckets = matches[:top_n]
        summary_buckets = matches[top_n:]

        return full_buckets, summary_buckets

    # ---------------------------------------------------------
    # Task bucket masking / 任务桶屏蔽
    # ---------------------------------------------------------
    def _mask_task_buckets(self, buckets: list[dict]) -> list[dict]:
        """
        Filter out task_flag=True buckets.
        Used when user is in a vulnerable state (sick / tired / emotional)
        to prevent the model from acting like a cold KPI machine.

        屏蔽所有 task_flag=True 的记忆桶。
        在用户生病/疲惫/情绪化时调用，防止模型像个冰冷的 KPI 机器一样催任务。
        """
        before = len(buckets)
        filtered = [b for b in buckets if not b.get("metadata", {}).get("task_flag", False)]
        masked = before - len(filtered)
        if masked > 0:
            logger.info(
                f"Task buckets masked (vulnerable state) / "
                f"任务桶已屏蔽（脆弱状态）: {masked}/{before}"
            )
        return filtered

    # ---------------------------------------------------------
    # Emotion resonance sub-score:
    # Based on Russell circumplex Euclidean distance
    # 情感共鸣子分：基于环形情感模型的欧氏距离
    # No emotion in query → neutral 0.5 (doesn't affect ranking)
    # ---------------------------------------------------------
    def _calc_emotion_score(
        self, q_valence: float, q_arousal: float, meta: dict
    ) -> float:
        """
        Calculate emotion resonance score (0~1, closer = higher).
        计算情感共鸣度（0~1，越近越高）。
        """
        if q_valence is None or q_arousal is None:
            return 0.5

        try:
            if "emotions" in meta and meta["emotions"]:
                return self._calc_emotion_score_new(q_valence, q_arousal, meta)
            else:
                b_valence = float(meta.get("valence", 0.5))
                b_arousal = float(meta.get("arousal", 0.3))
                dist = math.sqrt((q_valence - b_valence) ** 2 + (q_arousal - b_arousal) ** 2)
                return max(0.0, 1.0 - dist / 1.414)
        except (ValueError, TypeError):
            return 0.5

    def _calc_emotion_score_new(
        self, q_valence: float, q_arousal: float, meta: dict
    ) -> float:
        """
        Calculate emotion resonance score using new emotions array format.
        使用新的 emotions 数组格式计算情感共鸣度。
        """
        emotions = meta.get("emotions", [])
        if not emotions:
            return 0.5

        q_label = "正面" if q_valence > 0.5 else "负面"
        q_intensity = abs(q_valence - 0.5) * 2
        
        scores = []
        for e in emotions:
            label = e.get("label", "")
            intensity = float(e.get("intensity", 0.0))
            
            label_match = 1.0 if label == q_label else 0.5
            intensity_diff = 1.0 - abs(intensity - q_intensity)
            
            scores.append(label_match * intensity_diff)
        
        return max(scores) if scores else 0.5

    # ---------------------------------------------------------
    # Time proximity sub-score:
    # More recent activation → higher score
    # 时间亲近子分：距上次激活越近分越高
    # ---------------------------------------------------------
    def _calc_time_score(self, meta: dict) -> float:
        """
        Calculate time proximity score (0~1, more recent = higher).
        计算时间亲近度。
        
        Uses timezone-aware comparison to prevent issues in cross-region
        deployments (UTC server vs local client).
        """
        last_active_str = meta.get("last_active", meta.get("created", ""))
        try:
            last_active = datetime.fromisoformat(str(last_active_str))
            # Use UTC for "now" to ensure consistent timezone comparison
            now = datetime.now(timezone.utc)
            # If last_active is timezone-naive, treat it as UTC
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
            days = max(0.0, (now - last_active).total_seconds() / 86400)
        except (ValueError, TypeError):
            days = 30
        return math.exp(-0.02 * days)

    # ---------------------------------------------------------
    # List all buckets
    # 列出所有桶
    # ---------------------------------------------------------
    async def list_all(self, include_archive: bool = False) -> list[dict]:
        """
        Recursively walk directories (including domain subdirs), list all buckets.
        Uses memory cache to avoid repeated disk I/O.
        递归遍历目录（含域子目录），列出所有记忆桶。使用内存缓存避免重复磁盘IO。
        """
        import time
        now = time.time()
        
        if self._cache_timestamp > 0 and (now - self._cache_timestamp) < self._cache_validity:
            if include_archive:
                return self._buckets_cache
            else:
                return [b for b in self._buckets_cache if b.get("metadata", {}).get("type") != "archive"]
        
        buckets = []

        dirs = [self.permanent_dir, self.dynamic_dir, self.feel_dir, self.identity_dir, self.pattern_dir,
                self.milestone_dir, self.voice_dir, self.boundary_dir, self.ephemeral_dir]
        if include_archive:
            dirs.append(self.archive_dir)

        for dir_path in dirs:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for filename in files:
                    if not filename.endswith(".md"):
                        continue
                    file_path = os.path.join(root, filename)
                    bucket = self._load_bucket(file_path)
                    if bucket:
                        buckets.append(bucket)

        self._buckets_cache = buckets
        self._cache_timestamp = now
        
        return buckets

    def _invalidate_cache(self):
        """Invalidate the buckets cache when data changes."""
        self._cache_timestamp = 0

    def invalidate_index(self):
        """
        Invalidate both the bucket file cache and the in-memory BM25 index.
        Called by the housekeeper after bulk mutations (superseded / deleted /
        purged) so retrieval reflects the on-disk state immediately.
        失效内存索引（桶列表缓存 + BM25 检索索引）：
        管家在批量变更（superseded 标记/删除/清空）后调用，确保检索不查空。
        """
        self._invalidate_cache()
        if self.hybrid_search is not None:
            try:
                self.hybrid_search.invalidate_index()
            except Exception as e:
                logger.warning(f"Failed to invalidate hybrid index / 混合索引失效失败: {e}")

    async def find_by_domain(self, domain: str, include_archive: bool = False) -> list[dict]:
        """
        Find all buckets with a specific domain.
        按域查找所有记忆桶。
        
        Args:
            domain: 域名称（如"经验"）
            include_archive: 是否包含归档的桶
        
        Returns:
            匹配的记忆桶列表
        """
        all_buckets = await self.list_all(include_archive=include_archive)
        
        domain_lower = domain.lower()
        return [
            b for b in all_buckets
            if any(d.lower() == domain_lower for d in b.get("metadata", {}).get("domain", []))
        ]

    # ---------------------------------------------------------
    # Statistics (counts per category + total size)
    # 统计信息（各分类桶数量 + 总体积）
    # ---------------------------------------------------------
    async def get_stats(self) -> dict:
        """
        Return memory bucket statistics (including domain subdirs).
        返回记忆桶的统计数据。
        """
        stats = {
            "permanent_count": 0,
            "dynamic_count": 0,
            "archive_count": 0,
            "feel_count": 0,
            "milestone_count": 0,
            "voice_count": 0,
            "boundary_count": 0,
            "faded_count": 0,
            "ephemeral_count": 0,
            "total_size_kb": 0.0,
            "domains": {},
        }

        for subdir, key in [
            (self.permanent_dir, "permanent_count"),
            (self.dynamic_dir, "dynamic_count"),
            (self.archive_dir, "archive_count"),
            (self.feel_dir, "feel_count"),
            (self.milestone_dir, "milestone_count"),
            (self.voice_dir, "voice_count"),
            (self.boundary_dir, "boundary_count"),
            (self.faded_dir, "faded_count"),
            (self.ephemeral_dir, "ephemeral_count"),
        ]:
            if not os.path.exists(subdir):
                continue
            for root, _, files in os.walk(subdir):
                for f in files:
                    if f.endswith(".md"):
                        stats[key] += 1
                        fpath = os.path.join(root, f)
                        try:
                            stats["total_size_kb"] += os.path.getsize(fpath) / 1024
                        except OSError as e:
                            logger.debug(f"Failed to get file size: {fpath}: {e}")
                        # Per-domain counts / 每个域的桶数量
                        domain_name = os.path.basename(root)
                        if domain_name != os.path.basename(subdir):
                            stats["domains"][domain_name] = stats["domains"].get(domain_name, 0) + 1

        return stats

    # ---------------------------------------------------------
    # Archive bucket (move from permanent/dynamic into archive)
    # 归档桶（从 permanent/dynamic 移入 archive）
    # Called by decay engine to simulate "forgetting"
    # 由衰减引擎调用，模拟"遗忘"
    # ---------------------------------------------------------
    async def archive(self, bucket_id: str) -> bool:
        """
        Move a bucket into the archive directory (preserving domain subdirs).
        将指定桶移入归档目录（保留域子目录结构）。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        # --- Lock read-modify-write + move to prevent lost update ---
        # --- 持锁完成"改类型+移动"，防止并发 update 覆盖或重建旧路径文件 ---
        with _file_lock:
            try:
                # Read once, get domain info and update type / 一次性读取
                post = frontmatter.load(file_path)
                domain = as_list(post.get("domain")) or ["未分类"]
                primary_domain = sanitize_name(domain[0]) if domain else "未分类"
                archive_subdir = os.path.join(self.archive_dir, primary_domain)
                os.makedirs(archive_subdir, exist_ok=True)

                dest = safe_path(archive_subdir, os.path.basename(file_path))

                # Update type marker then move file / 更新类型标记后移动文件
                post["type"] = "archived"
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))

                # Use shutil.move for cross-filesystem safety
                # 使用 shutil.move 保证跨文件系统安全
                shutil.move(file_path, str(dest))
            except Exception as e:
                logger.error(
                    f"Failed to archive bucket / 归档桶失败: {bucket_id}: {e}"
                )
                return False

        logger.info(f"Archived bucket / 归档记忆桶: {bucket_id} → archive/{primary_domain}/")
        self._invalidate_cache()
        return True

    # ---------------------------------------------------------
    # Faded memory (模糊印象标签)
    # 久远普通记忆衰减后不物理删除，降维压缩为轻量级模糊印象标签，
    # 供主 AI 表达自然的沧桑感与模糊回忆。
    # ---------------------------------------------------------
    async def create_faded_memory(
        self,
        original_bucket_id: str,
        faded_label: str,
        original_type: str = "dynamic",
        decay_score: float = 0.0,
    ) -> Optional[str]:
        """
        Compress a decayed bucket into a lightweight faded-memory tag.
        将衰减记忆降维压缩为轻量模糊印象标签。

        Args:
            original_bucket_id: 原记忆桶 ID
            faded_label: 模糊印象标签（一两句话，保留情绪基调与模糊事实）
            original_type: 原桶类型（默认 dynamic）
            decay_score: 触发衰减时的得分

        Returns:
            faded bucket ID，或失败时返回 None
        """
        faded_id = generate_bucket_id()
        now = now_iso()
        metadata = {
            "id": faded_id,
            "name": f"模糊印象: {faded_label[:24]}",
            "type": "faded_memory",
            "original_id": original_bucket_id,
            "original_type": original_type,
            "decay_score": decay_score,
            "faded_at": now,
            "summary": faded_label,
            "tags": [],
            "created": now,
            "last_active": now,
        }
        post = frontmatter.Post(faded_label)
        for k, v in metadata.items():
            post[k] = v

        file_path = safe_path(self.faded_dir, f"{faded_id}.md")
        with _file_lock:
            try:
                self._atomic_write(file_path, frontmatter.dumps(post))
            except Exception as e:
                logger.warning(f"Create faded memory failed / 模糊印象写入失败: {e}")
                return None
        self._invalidate_cache()
        logger.info(f"Faded memory created / 生成模糊印象: {faded_id} ← {original_bucket_id}")
        return faded_id

    async def list_faded_memories(self, limit: int = 5) -> list[dict]:
        """
        List faded-memory tags (lightweight blurred impressions).
        列出模糊印象标签（轻量模糊回忆）。
        """
        if not os.path.isdir(self.faded_dir):
            return []
        items = []
        try:
            for fname in os.listdir(self.faded_dir):
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(self.faded_dir, fname)
                try:
                    post = frontmatter.load(fpath)
                except Exception:
                    continue
                meta = dict(post.metadata)
                items.append({
                    "id": meta.get("id") or fname[:-3],
                    "name": meta.get("name", ""),
                    "label": str(post.content or "").strip(),
                    "original_id": meta.get("original_id", ""),
                    "original_type": meta.get("original_type", "dynamic"),
                    "faded_at": meta.get("faded_at", ""),
                    "decay_score": meta.get("decay_score", 0.0),
                })
        except Exception as e:
            logger.warning(f"List faded memories failed / 列出模糊印象失败: {e}")
        items.sort(key=lambda x: x.get("faded_at", ""), reverse=True)
        return items[:max(1, limit)]

    async def supersede_bucket(self, old_id: str, new_id: str, reason: str = "") -> bool:
        """Mark an old bucket as superseded, pointing to the new bucket ID.
        When facts change or habits are revised, the housekeeper lands the new
        bucket and annotates the old one with status=superseded → new bucket ID;
        superseded buckets are auto-masked in search/retrieval.
        记忆版本控制：事实更替/习惯变更时，管家落库新桶的同时把旧桶标注
        status=superseded + superseded_by=new_id；检索时自动屏蔽失效旧桶。"""
        if not old_id or not new_id or old_id == new_id:
            return False
        return await self.update(
            old_id,
            status="superseded",
            superseded_by=new_id,
            superseded_at=now_iso(),
            resolved=True,
            resolved_reason=reason or "superseded",
        )

    async def purge_expired_ephemeral(self, half_life_hours: int = 24) -> int:
        """Evaporate ephemeral buckets not re-referenced within the half-life window.
        Called by nightly dream(): vent-only content older than the half-life AND not
        re-accessed (retrieval refreshes last_accessed) is softly deleted to .trash/.
        ephemeral 半衰期蒸发：dream() 夜间调用；超过半衰期且未被再次引用的
        纯发泄内容自然蒸发（软删除移入 .trash/，并同步清理向量）。"""
        if not os.path.isdir(self.ephemeral_dir):
            return 0
        all_buckets = await self.list_all(include_archive=False)
        now = datetime.now(timezone.utc)
        cutoff = half_life_hours * 3600
        expired_ids = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            if meta.get("type") != "ephemeral":
                continue
            ref_str = str(meta.get("last_accessed") or meta.get("created") or "")
            try:
                ref_dt = datetime.fromisoformat(ref_str)
                if ref_dt.tzinfo is None:
                    ref_dt = ref_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                ref_dt = now
            if (now - ref_dt).total_seconds() > cutoff:
                expired_ids.append(b["id"])
        evaporated = 0
        for bid in expired_ids:
            if await self.delete(bid):
                # --- Sync-clean vector store / 同步清理向量库 ---
                if self.embedding_engine is not None:
                    try:
                        self.embedding_engine.delete_embedding(bid)
                    except Exception as e:
                        logger.warning(f"Failed to delete ephemeral embedding / ephemeral 向量清理失败: {bid}: {e}")
                evaporated += 1
                logger.info(f"Ephemeral bucket evaporated / ephemeral 暂存区蒸发: {bid}")
        if evaporated:
            self._invalidate_cache()
        return evaporated

    # ---------------------------------------------------------
    # Dream sandbox (临时梦境沙盒)
    # 睡眠做梦的中间联想与草稿只允许写到这里，绝不作为永久记忆落库；
    # 每日晚间做梦整理完毕、主 AI 审阅后，日终必须彻底物理清空，避免垃圾梦境重复堆积。
    # ---------------------------------------------------------
    async def save_dream_sandbox(self, date_str: str, payload: dict) -> str:
        """Write today's dream intermediate drafts to the temp sandbox.

        将今日梦境中间联想/草稿写入临时沙盒文件（temp_dreams/<date>.json）。
        绝不直接创建永久记忆桶——落库与否由主 AI 审阅后决定。
        """
        try:
            os.makedirs(self.temp_dreams_dir, exist_ok=True)
            file_path = os.path.join(self.temp_dreams_dir, f"{date_str}.json")
            self._atomic_write(file_path, json.dumps(payload, ensure_ascii=False, indent=2))
            logger.info(f"Dream sandbox written / 梦境沙盒已写入: {file_path}")
            return file_path
        except OSError as e:
            logger.warning(f"Failed to write dream sandbox / 梦境沙盒写入失败: {e}")
            return ""

    async def load_dream_sandbox(self, date_str: str) -> dict:
        """Read a sandbox file (used to restore yesterday's drafts if needed)."""
        file_path = os.path.join(self.temp_dreams_dir, f"{date_str}.json")
        if not os.path.exists(file_path):
            return {}
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load dream sandbox / 梦境沙盒读取失败: {e}")
            return {}

    async def purge_dream_sandbox(self, keep_today: bool = True) -> int:
        """Physically clear dream sandbox files (garbage dreams must not accumulate).

        物理清空梦境沙盒文件：保留今日（若有），其余一律删除。
        在日终整理（daily_review / run_housekeeper）与每次做梦开始时调用。
        """
        if not os.path.isdir(self.temp_dreams_dir):
            return 0
        today = now_iso()[:10]
        removed = 0
        try:
            for filename in os.listdir(self.temp_dreams_dir):
                if not filename.endswith(".json"):
                    continue
                if keep_today and filename == f"{today}.json":
                    continue
                try:
                    os.remove(os.path.join(self.temp_dreams_dir, filename))
                    removed += 1
                except OSError as e:
                    logger.warning(f"Failed to purge dream sandbox / 沙盒清理失败 {filename}: {e}")
        except OSError as e:
            logger.warning(f"Dream sandbox list failed / 沙盒目录读取失败: {e}")
        if removed:
            logger.info(f"Dream sandbox purged / 梦境沙盒已物理清空 {removed} 个文件")
        return removed

    # ---------------------------------------------------------
    # Internal: find bucket file across all three directories
    # 内部：在三个目录中查找桶文件
    # ---------------------------------------------------------
    def _find_bucket_file(self, bucket_id: str) -> Optional[str]:
        """
        Recursively search permanent/dynamic/archive/identity/pattern for a bucket file
        matching the given ID.
        在 permanent/dynamic/archive/identity/pattern 中递归查找指定 ID 的桶文件。
        """
        if not bucket_id:
            return None
        for dir_path in [self.permanent_dir, self.dynamic_dir, self.archive_dir, self.feel_dir, self.identity_dir, self.pattern_dir, self.milestone_dir, self.voice_dir, self.boundary_dir, self.ephemeral_dir]:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for fname in files:
                    if not fname.endswith(".md"):
                        continue
                    # Match by exact ID segment in filename
                    # 通过文件名中的 ID 片段精确匹配
                    name_part = fname[:-3]  # remove .md
                    if name_part == bucket_id or name_part.endswith(f"_{bucket_id}"):
                        return os.path.join(root, fname)
        return None

    # ---------------------------------------------------------
    # Internal: load bucket data from .md file
    # 内部：从 .md 文件加载桶数据
    # ---------------------------------------------------------
    def _load_bucket(self, file_path: str) -> Optional[dict]:
        """
        Parse a Markdown file and return structured bucket data.
        解析 Markdown 文件，返回桶的结构化数据。
        """
        try:
            post = frontmatter.load(file_path)
            metadata = dict(post.metadata)
            
            metadata = self._normalize_bucket_metadata(metadata)
            
            return {
                "id": post.get("id", Path(file_path).stem),
                "metadata": metadata,
                "content": post.content,
                "path": file_path,
            }
        except json.JSONDecodeError as e:
            # --- Corrupted payload (rare, e.g. half-written file from a crash): ---
            # --- skip this bucket and never crash the system ---
            # --- 极小概率读到损坏文件：跳过该桶并记录 warning，绝不引发系统崩溃 ---
            logger.warning(
                f"Skipping corrupted bucket file / 跳过损坏桶文件: {file_path}: {e}"
            )
            return None
        except Exception as e:
            logger.warning(
                f"Failed to load bucket file / 加载桶文件失败: {file_path}: {e}"
            )
            return None

    def _normalize_bucket_metadata(self, metadata: dict) -> dict:
        """
        Normalize bucket metadata: convert old valence/arousal to new emotions format.
        标准化桶元数据：将旧的 valence/arousal 格式转换为新的 emotions 格式。
        """
        if "emotions" not in metadata and ("valence" in metadata or "arousal" in metadata):
            # Guard against dirty YAML (null / "abc") — fall back to neutral values
            # 防止脏数据（null/"abc"）导致整桶加载失败
            valence = safe_float(metadata.get("valence"), 0.5)
            arousal = safe_float(metadata.get("arousal"), 0.3)
            metadata["valence"] = valence
            metadata["arousal"] = arousal
            metadata["emotions"] = self._valence_arousal_to_emotions(valence, arousal)
            if not metadata.get("dominant_emotion") and metadata["emotions"]:
                metadata["dominant_emotion"] = max(
                    metadata["emotions"], key=lambda e: e["intensity"]
                )["label"]
        
        if "type" not in metadata:
            metadata["type"] = "event"
        elif metadata["type"] == "dynamic":
            metadata["type"] = "event"
        
        return metadata

    def _valence_arousal_to_emotions(self, valence: float, arousal: float) -> list[dict]:
        """
        Convert old valence/arousal format to new emotions array.
        将旧的 valence/arousal 格式转换为新的 emotions 数组。
        
        Mapping rules:
        - valence > 0.5 → positive emotion, < 0.5 → negative emotion
        - arousal → intensity
        """
        emotions = []
        if valence > 0.5:
            emotions.append({"label": "正面", "intensity": min(1.0, (valence - 0.5) * 2)})
        elif valence < 0.5:
            emotions.append({"label": "负面", "intensity": min(1.0, (0.5 - valence) * 2)})
        
        if arousal > 0.2:
            if arousal > 0.7:
                emotions.append({"label": "激动", "intensity": min(1.0, (arousal - 0.7) * 3.33)})
            else:
                emotions.append({"label": "平静", "intensity": min(1.0, arousal * 1.43)})
        
        return emotions

    def _normalize_emotions(self, emotions: list[dict]) -> list[dict]:
        """
        Normalize emotion labels using synonym merging.
        使用同义词归并标准化情绪标签。
        """
        return emotions
