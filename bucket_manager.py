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
import math
import logging
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import frontmatter
from rapidfuzz import fuzz

from utils import generate_bucket_id, sanitize_name, sanitize_filename, safe_path, now_iso

try:
    from hybrid_search import HybridSearchEngine
    HAS_HYBRID_SEARCH = True
except ImportError:
    HAS_HYBRID_SEARCH = False
    logger.warning("Hybrid search module not found, using legacy search")

logger = logging.getLogger("ombre_brain.bucket")

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
        self.pattern_dir = os.path.join(self.base_dir, "pattern")
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
        self.w_emotion_arousal = scoring.get("emotion_arousal", 3.0)    # 情绪唤醒度权重
        self.w_explicit_priority = scoring.get("explicit_priority", 2.0)  # 显式优先级权重（钉选）
        self.w_vector_similarity = scoring.get("vector_similarity", 4.0)  # 向量相似度权重
        self.w_topic = scoring.get("topic_relevance", 5.0)               # 主题相关性权重（最高）
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

        # --- Cooldown state for pattern/experience injection ---
        # --- 年轮经验注入冷却状态（内存级，不持久化）---
        # Records {bucket_id: last_injected_timestamp} for cooldown tracking
        activation_cfg = config.get("activation", {})
        self.cooldown_seconds = activation_cfg.get("cooldown_seconds", 300)  # 5 min default
        self.similarity_threshold = activation_cfg.get("similarity_threshold", 0.75)
        self.cooldown_decay_factor = activation_cfg.get("cooldown_decay_factor", 0.5)
        self._injection_history: dict[str, float] = {}  # {bucket_id: timestamp}

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
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
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
                return json.load(f)
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
        tags = tags or []
        linked_content = content

        if pinned or protected:
            importance = 10

        emotions = emotions or []
        if valence is not None or arousal is not None:
            emotions = self._valence_arousal_to_emotions(valence or 0.5, arousal or 0.3)
        
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
            "importance": max(1, min(10, importance)),
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

        # --- Thread-safe file write ---
        # --- 线程安全的文件写入 ---
        with _file_lock:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
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

            if "content" in kwargs:
                post.content = kwargs["content"]
            if "tags" in kwargs:
                post["tags"] = kwargs["tags"]
            if "importance" in kwargs:
                post["importance"] = max(1, min(10, int(kwargs["importance"])))
            if "domain" in kwargs:
                post["domain"] = kwargs["domain"]
            if "emotions" in kwargs:
                post["emotions"] = kwargs["emotions"]
            if "dominant_emotion" in kwargs:
                post["dominant_emotion"] = kwargs["dominant_emotion"]
            if "emotion_metrics" in kwargs:
                post["emotion_metrics"] = kwargs["emotion_metrics"]
            if "valence" in kwargs or "arousal" in kwargs:
                v = float(kwargs.get("valence", post.get("valence", 0.5)))
                a = float(kwargs.get("arousal", post.get("arousal", 0.3)))
                emotions = self._valence_arousal_to_emotions(v, a)
                post["emotions"] = emotions
                if not post.get("dominant_emotion") and emotions:
                    post["dominant_emotion"] = max(emotions, key=lambda e: e["intensity"])["label"]
            if "name" in kwargs:
                post["name"] = sanitize_name(kwargs["name"])
            if "resolved" in kwargs:
                # --- Task bucket protection: prevent auto-resolving from dream() ---
                # --- 任务桶保护：防止 dream() 自动解决任务 ---
                # If task_flag=True, require force_resolved=True to set resolved=True
                # 只有显式指定 force_resolved=True 才能解决 task_flag=True 的桶
                task_flag = post.get("task_flag", False)
                new_resolved = bool(kwargs["resolved"])
                
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
                post["pinned"] = bool(kwargs["pinned"])
                if kwargs["pinned"]:
                    post["importance"] = 10
            if "digested" in kwargs:
                post["digested"] = bool(kwargs["digested"])
            if "task_flag" in kwargs:
                post["task_flag"] = bool(kwargs["task_flag"])
            if "decay_stage" in kwargs:
                post["decay_stage"] = int(kwargs["decay_stage"])
            if "model_valence" in kwargs:
                post["model_valence"] = max(0.0, min(1.0, float(kwargs["model_valence"])))
            
            for key in ("exp_type", "source", "apply_count", "last_applied", "title", "one_line_summary", "source_bucket_ids", "hit_count", "last_hit", "dehydrated_summary", "previous_event_id", "next_event_id"):
                if key in kwargs:
                    post[key] = kwargs[key]

            # --- Auto-refresh activation time / 自动刷新激活时间 ---
            post["last_active"] = now_iso()

            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
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
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
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
        
        return True

    async def remove_related_bucket(self, bucket_id: str, related_id: str) -> bool:
        """Remove a bidirectional relationship between two buckets."""
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        
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
        
        return True

    async def add_event_sequence(self, bucket_id: str, event_id: str, position: int = None) -> bool:
        """Add an event to the sequence chain."""
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        
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
                importance_details[key] = max(0, min(10, int(details[key])))
        
        post["importance_details"] = importance_details
        
        total = sum(importance_details.values())
        average = total / 5 if total > 0 else post.get("importance", 5)
        post["importance"] = max(1, min(10, round(average)))
        
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        except OSError as e:
            logger.error(f"Failed to update importance details: {e}")
            return False
        
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
        Delete a memory bucket file.
        删除指定的记忆桶文件。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            os.remove(file_path)
        except OSError as e:
            logger.error(f"Failed to delete bucket file / 删除桶文件失败: {file_path}: {e}")
            return False

        logger.info(f"Deleted bucket / 删除记忆桶: {bucket_id}")
        
        self._invalidate_cache()
        return True

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

        try:
            post = frontmatter.load(file_path)
            post["last_active"] = now_iso()
            post["activation_count"] = post.get("activation_count", 0) + 1

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))

            # --- Time ripple: boost nearby memories within ±48h ---
            # --- 时间涟漪：±48小时内的记忆轻微唤醒 ---
            current_time = datetime.fromisoformat(str(post.get("created", post.get("last_active", ""))))
            await self._time_ripple(bucket_id, current_time)
            
            self._invalidate_cache()
        except Exception as e:
            logger.warning(f"Failed to touch bucket / 触碰桶失败: {bucket_id}: {e}")

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
        except Exception:
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
                    post = frontmatter.load(file_path)
                    current_count = post.get("activation_count", 1)
                    # Store as float for fractional increments; calculate_score handles it
                    post["activation_count"] = round(current_count + 0.3, 1)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(frontmatter.dumps(post))
                    rippled += 1
                except Exception:
                    continue

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

        limit = limit or self.max_results
        all_buckets = await self.list_all(include_archive=False)

        if not all_buckets:
            return []

        # --- Mask task_flag buckets if requested ---
        # --- 屏蔽任务类桶（如用户生病/疲惫/情绪化）---
        if mask_tasks:
            all_buckets = self._mask_task_buckets(all_buckets)

        # --- Layer 0: exclude superseded patterns ---
        # --- 第0层：排除已被替代的模式 ---
        all_buckets = [
            b for b in all_buckets
            if not (b["metadata"].get("type") == "pattern" and b["metadata"].get("superseded_by") is not None)
        ]

        # --- Layer 1: domain pre-filter (fast scope reduction) ---
        # --- 第一层：主题域预筛（快速缩小范围）---
        candidates = all_buckets
        if domain_filter:
            filter_set = {d.lower() for d in domain_filter}
            candidates = [
                b for b in all_buckets
                if {d.lower() for d in b["metadata"].get("domain", [])} & filter_set
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
                explicit_priority = 1.0 if (meta.get("pinned") or meta.get("protected")) else 0.0

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
                (fuzz.partial_ratio(query, d) for d in meta.get("domain", [])),
                default=0,
            )
            * 2.5
        )
        tag_score = (
            max(
                (fuzz.partial_ratio(query, tag) for tag in meta.get("tags", [])),
                default=0,
            )
            * 2
        )
        content_score = fuzz.partial_ratio(query, bucket.get("content", "")[:1000]) * self.content_weight

        return (name_score + domain_score + tag_score + content_score) / (100 * (3 + 2.5 + 2 + self.content_weight))

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

        for d in meta.get("domain", []):
            if q in d.lower():
                score += 2.5
                matches += 1

        for tag in meta.get("tags", []):
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
                emotions = meta["emotions"]
                max_intensity = max(float(e.get("intensity", 0.0)) for e in emotions)
                return max(0.0, min(1.0, max_intensity))

            arousal = float(meta.get("arousal", 0.3))
            return max(0.0, min(1.0, arousal))
        except (ValueError, TypeError):
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
            if meta.get("pinned") or meta.get("protected"):
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

        dirs = [self.permanent_dir, self.dynamic_dir, self.feel_dir, self.identity_dir, self.pattern_dir]
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
            "total_size_kb": 0.0,
            "domains": {},
        }

        for subdir, key in [
            (self.permanent_dir, "permanent_count"),
            (self.dynamic_dir, "dynamic_count"),
            (self.archive_dir, "archive_count"),
            (self.feel_dir, "feel_count"),
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

        try:
            # Read once, get domain info and update type / 一次性读取
            post = frontmatter.load(file_path)
            domain = post.get("domain", ["未分类"])
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
        return True

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
        for dir_path in [self.permanent_dir, self.dynamic_dir, self.archive_dir, self.feel_dir, self.identity_dir, self.pattern_dir]:
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
            valence = float(metadata.get("valence", 0.5))
            arousal = float(metadata.get("arousal", 0.3))
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
