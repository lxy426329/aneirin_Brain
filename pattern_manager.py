# ============================================================
# Module: Pattern Manager (pattern_manager.py)
# 模块：模式管理器
#
# Manages pattern layer storage and operations.
# 管理模式层的存储和操作。
# ============================================================

import os
import logging
from typing import List, Dict, Optional
from pathlib import Path
import frontmatter

from utils import generate_bucket_id, sanitize_name, now_iso, safe_path

logger = logging.getLogger(__name__)


class PatternManager:
    """
    Manages pattern storage (rules, guidelines, behavioral patterns).
    模式管理器：存储从经历中提炼出的规律、准则、行为模式。
    """
    
    def __init__(self, config: dict):
        self.base_dir = config["buckets_dir"]
        self.pattern_dir = os.path.join(self.base_dir, "pattern")
        os.makedirs(self.pattern_dir, exist_ok=True)
    
    async def create(
        self,
        summary: str,
        source_events: List[str] = None,
        applicable_scenes: List[str] = None,
        confidence: float = 0.5,
        tags: List[str] = None,
        content: str = "",
        name: str = "",
    ) -> str:
        """
        Create a new pattern.
        
        Args:
            summary: pattern description / 规律描述
            source_events: list of bucket IDs from which this pattern was derived
            applicable_scenes: list of scene tags where this pattern applies
            confidence: confidence level 0~1
            tags: additional tags
            content: detailed content
            name: pattern name
        
        Returns:
            pattern ID
        """
        pattern_id = generate_bucket_id()
        pattern_name = sanitize_name(name) if name else pattern_id
        
        metadata = {
            "id": pattern_id,
            "name": pattern_name,
            "type": "pattern",
            "summary": summary,
            "source_events": source_events or [],
            "applicable_scenes": applicable_scenes or [],
            "confidence": max(0.0, min(1.0, confidence)),
            "tags": tags or [],
            "created": now_iso(),
            "last_active": now_iso(),
            "activation_count": 0,
            "superseded_by": None,
            "conflict_resolution": None,
        }
        
        post = frontmatter.Post(content)
        for k, v in metadata.items():
            post[k] = v
        
        file_path = safe_path(self.pattern_dir, f"{pattern_id}.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))
        
        await self._resolve_conflicts(pattern_id, summary, tags, applicable_scenes)
        
        logger.info(f"Created pattern: {pattern_id} ({pattern_name})")
        return pattern_id
    
    async def _resolve_conflicts(
        self,
        new_pattern_id: str,
        new_summary: str,
        new_tags: List[str] = None,
        new_scenes: List[str] = None,
    ):
        """
        Detect and resolve conflicts with existing patterns.
        
        When a new pattern is created, check for existing patterns in the same
        domain/scene that might conflict. Mark conflicting patterns as superseded.
        
        Conflict detection criteria:
        1. Same applicable_scenes (intersection >= 1)
        2. Overlapping tags (intersection >= 2)
        3. Similar summary content (semantic similarity)
        """
        all_patterns = await self.list_all()
        new_tags_set = set(new_tags or [])
        new_scenes_set = set(new_scenes or [])
        
        for pattern in all_patterns:
            if pattern["id"] == new_pattern_id:
                continue
            
            meta = pattern["metadata"]
            
            if meta.get("superseded_by") is not None:
                continue
            
            existing_tags = set(meta.get("tags", []))
            existing_scenes = set(meta.get("applicable_scenes", []))
            
            tag_overlap = len(new_tags_set & existing_tags)
            scene_overlap = len(new_scenes_set & existing_scenes)
            
            summary_similar = self._is_similar_summary(new_summary, meta.get("summary", ""))
            
            if (scene_overlap >= 1 and tag_overlap >= 2) or summary_similar:
                await self.update(
                    pattern["id"],
                    superseded_by=new_pattern_id,
                    conflict_resolution="superseded",
                )
                logger.info(f"Marked pattern {pattern['id']} as superseded by {new_pattern_id}")
    
    def _is_similar_summary(self, summary1: str, summary2: str) -> bool:
        """
        Simple similarity check for summaries.
        Returns True if summaries are likely describing the same rule/pattern.
        """
        if not summary1 or not summary2:
            return False
        
        words1 = set(summary1.lower().split())
        words2 = set(summary2.lower().split())
        
        if not words1 or not words2:
            return False
        
        intersection = words1 & words2
        union = words1 | words2
        
        jaccard_similarity = len(intersection) / len(union) if union else 0.0
        
        return jaccard_similarity > 0.4
    
    async def get(self, pattern_id: str) -> Optional[dict]:
        """Get pattern by ID."""
        if not pattern_id:
            return None
        file_path = safe_path(self.pattern_dir, f"{pattern_id}.md")
        if not os.path.exists(file_path):
            return None
        return self._load_pattern(file_path)
    
    async def update(
        self,
        pattern_id: str,
        summary: str = None,
        source_events: List[str] = None,
        applicable_scenes: List[str] = None,
        confidence: float = None,
        tags: List[str] = None,
        content: str = None,
        name: str = None,
        superseded_by: str = None,
        conflict_resolution: str = None,
    ) -> bool:
        """Update pattern fields."""
        file_path = safe_path(self.pattern_dir, f"{pattern_id}.md")
        if not os.path.exists(file_path):
            return False
        
        try:
            post = frontmatter.load(file_path)
            
            if summary is not None:
                post["summary"] = summary
            if source_events is not None:
                post["source_events"] = source_events
            if applicable_scenes is not None:
                post["applicable_scenes"] = applicable_scenes
            if confidence is not None:
                post["confidence"] = max(0.0, min(1.0, confidence))
            if tags is not None:
                post["tags"] = tags
            if content is not None:
                post.content = content
            if name is not None:
                post["name"] = sanitize_name(name)
            if superseded_by is not None:
                post["superseded_by"] = superseded_by
            if conflict_resolution is not None:
                post["conflict_resolution"] = conflict_resolution
            
            post["last_active"] = now_iso()
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            
            return True
        except Exception as e:
            logger.error(f"Failed to update pattern: {e}")
            return False
    
    async def delete(self, pattern_id: str) -> bool:
        """Delete pattern."""
        file_path = safe_path(self.pattern_dir, f"{pattern_id}.md")
        if not os.path.exists(file_path):
            return False
        
        try:
            os.remove(file_path)
            logger.info(f"Deleted pattern: {pattern_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete pattern: {e}")
            return False
    
    async def list_all(self) -> List[dict]:
        """List all patterns."""
        patterns = []
        if not os.path.exists(self.pattern_dir):
            return patterns
        
        for entry in os.listdir(self.pattern_dir):
            if entry.endswith(".md"):
                file_path = safe_path(self.pattern_dir, entry)
                pattern = self._load_pattern(file_path)
                if pattern:
                    patterns.append(pattern)
        
        patterns.sort(key=lambda p: p["metadata"].get("created", ""), reverse=True)
        return patterns
    
    async def add_source_event(self, pattern_id: str, bucket_id: str) -> bool:
        """Add a source event to a pattern."""
        pattern = await self.get(pattern_id)
        if not pattern:
            return False
        
        sources = pattern["metadata"].get("source_events", [])
        if bucket_id not in sources:
            sources.append(bucket_id)
            await self.update(pattern_id, source_events=sources)
        return True
    
    async def update_confidence(self, pattern_id: str, delta: float) -> bool:
        """Update confidence by delta."""
        pattern = await self.get(pattern_id)
        if not pattern:
            return False
        
        current = pattern["metadata"].get("confidence", 0.5)
        new_confidence = max(0.0, min(1.0, current + delta))
        return await self.update(pattern_id, confidence=new_confidence)
    
    def _load_pattern(self, file_path: str) -> Optional[dict]:
        """Load pattern from file."""
        try:
            post = frontmatter.load(file_path)
            return {
                "id": post.get("id", Path(file_path).stem),
                "metadata": dict(post.metadata),
                "content": post.content,
                "path": file_path,
            }
        except Exception as e:
            logger.warning(f"Failed to load pattern: {file_path}: {e}")
            return None