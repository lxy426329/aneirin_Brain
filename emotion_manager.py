# ============================================================
# Module: Emotion Manager (emotion_manager.py)
# 模块：情绪管理器
#
# Provides emotion synonym merging and normalization.
# 使用 DeepSeek API 判断情绪标签是否同义，进行归并。
# ============================================================

import os
import json
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class EmotionManager:
    """
    Emotion synonym merging and normalization.
    情绪同义词归并与标准化管理器。
    """
    
    POSITIVE_EMOTIONS = {
        '开心', '喜悦', '快乐', '幸福', '满足', '自豪', '感激', '欣慰',
        '憧憬', '热爱', '愉悦', '振奋', '得意', '自信', '兴奋', '惊喜',
        '感动', '震撼', '怀念', '思念', '牵挂', '依恋', '期待', '好奇', '惊讶'
    }
    
    NEGATIVE_EMOTIONS = {
        '难过', '悲伤', '伤心', '痛苦', '沮丧', '失望', '郁闷', '压抑',
        '担忧', '紧张', '不安', '孤独', '无助', '迷茫', '困惑', '焦虑',
        '恐惧', '害怕', '愤怒', '生气', '烦躁', '怨恨', '嫉妒', '羡慕',
        '愧疚', '自责', '悔恨', '遗憾', '失落', '委屈', '难堪', '尴尬', '羞愧'
    }
    
    NEUTRAL_EMOTIONS = {'平静', '平和', '淡定', '从容'}
    
    EMOTION_COLORS = {
        '开心': '#4A7C59', '喜悦': '#66BB6A', '快乐': '#81C784', '幸福': '#A5D6A7',
        '满足': '#8BC34A', '自豪': '#689F38', '感激': '#FFB74D', '欣慰': '#FFCA28',
        '憧憬': '#90CAF9', '热爱': '#42A5F5', '愉悦': '#64B5F6', '振奋': '#2196F3',
        '得意': '#FF7043', '自信': '#F4511E', '兴奋': '#FF8A65', '惊喜': '#CE93D8',
        '感动': '#BA68C8', '震撼': '#AB47BC', '怀念': '#8D6E63', '思念': '#A1887F',
        '牵挂': '#BCAAA4', '依恋': '#D7CCC8', '期待': '#4FC3F7', '好奇': '#29B6F6',
        '惊讶': '#26C6DA', '平静': '#90A4AE', '平和': '#78909C', '淡定': '#607D8B',
        '从容': '#546E7A', '难过': '#EF5350', '悲伤': '#E53935', '伤心': '#D32F2F',
        '痛苦': '#C62828', '沮丧': '#EF9A9A', '失望': '#E57373', '郁闷': '#EF5350',
        '压抑': '#F44336', '担忧': '#FFB74D', '紧张': '#FF9800', '不安': '#FF8F00',
        '孤独': '#78909C', '无助': '#546E7A', '迷茫': '#607D8B', '困惑': '#78909C',
        '焦虑': '#FF9800', '恐惧': '#E91E63', '害怕': '#C2185B', '愤怒': '#F44336',
        '生气': '#E53935', '烦躁': '#EF5350', '怨恨': '#D32F2F', '嫉妒': '#FFC107',
        '羡慕': '#FFB74D', '愧疚': '#9C27B0', '自责': '#7B1FA2', '悔恨': '#6A1B9A',
        '遗憾': '#7E57C2', '失落': '#9575CD', '委屈': '#E1BEE7', '难堪': '#CE93D8',
        '尴尬': '#BA68C8', '羞愧': '#AB47BC'
    }
    
    def __init__(self, config: dict):
        self.config = config
        self.base_emotions = config.get("emotions", {}).get("base_list", [])
        self.synonym_cache = {}
        
        dehy_config = config.get("dehydration", {})
        self.api_key = dehy_config.get("api_key", "") or os.environ.get("OMBRE_API_KEY", "")
        self.base_url = dehy_config.get("base_url", "") or os.environ.get("OMBRE_DEHYDRATION_BASE_URL", "") or "https://api.deepseek.com/v1"
        self.model = dehy_config.get("model", "deepseek-chat")
        
        self._client = None
    
    @property
    def client(self):
        """Lazy-load OpenAI client."""
        if self._client is None and self.api_key:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client
    
    @property
    def api_available(self):
        """Check if API is configured."""
        return bool(self.api_key and self.base_url)
    
    async def normalize_emotions(self, emotions: List[Dict[str, any]]) -> List[Dict[str, any]]:
        """
        Normalize emotion labels using synonym merging.
        使用同义词归并标准化情绪标签。
        
        Args:
            emotions: list of {"label": str, "intensity": float} dicts
        
        Returns:
            Normalized emotions list with merged synonyms.
        """
        if not emotions:
            return emotions
        
        normalized = []
        for e in emotions:
            label = e.get("label", "").strip()
            intensity = float(e.get("intensity", 0.5))
            
            if not label:
                continue
            
            normalized_label = await self._find_synonym(label)
            normalized.append({
                "label": normalized_label,
                "intensity": max(0.0, min(1.0, intensity)),
            })
        
        return self._merge_same_labels(normalized)
    
    async def _find_synonym(self, label: str) -> str:
        """
        Find the canonical synonym for a given emotion label.
        
        Args:
            label: emotion label to normalize
        
        Returns:
            Canonical emotion label
        """
        if not label:
            return ""
        
        label_lower = label.lower()
        
        if label_lower in self.synonym_cache:
            return self.synonym_cache[label_lower]
        
        for base in self.base_emotions:
            base_lower = base.lower()
            if label_lower == base_lower:
                self.synonym_cache[label_lower] = base
                return base
        
        if self.api_available:
            try:
                canonical = await self._api_find_synonym(label)
                if canonical:
                    self.synonym_cache[label_lower] = canonical
                    return canonical
            except Exception as e:
                logger.warning(f"Emotion synonym API call failed: {e}")
        
        self.synonym_cache[label_lower] = label
        return label
    
    async def _api_find_synonym(self, label: str) -> Optional[str]:
        """
        Call DeepSeek API to find synonym in base emotion list.
        
        Args:
            label: emotion label to check
        
        Returns:
            Canonical emotion label from base list, or None if not found
        """
        if not self.base_emotions:
            return None
        
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "你是一个情绪同义词识别器。请判断用户输入的情绪标签是否与给定的基础情绪词表中的某个词同义或近义。"
                },
                {
                    "role": "user",
                    "content": f"基础情绪词表: {json.dumps(self.base_emotions, ensure_ascii=False)}\n"
                               f"用户输入: {label}\n"
                               f"请从基础词表中选择一个最匹配的词。如果没有匹配的，返回空字符串。"
                               f"只返回基础词表中的词或空字符串，不要输出其他内容。"
                },
            ],
            max_tokens=32,
            temperature=0.0,
        )
        
        if not response.choices:
            return None
        
        result = response.choices[0].message.content or ""
        result = result.strip()
        
        if result in self.base_emotions:
            return result
        
        return None
    
    def _merge_same_labels(self, emotions: List[Dict[str, any]]) -> List[Dict[str, any]]:
        """
        Merge emotions with the same label, taking max intensity.
        
        Args:
            emotions: list of emotion dicts
        
        Returns:
            Merged list with unique labels
        """
        merged = {}
        for e in emotions:
            label = e.get("label", "")
            intensity = float(e.get("intensity", 0.0))
            
            if label in merged:
                if intensity > merged[label]:
                    merged[label] = intensity
            else:
                merged[label] = intensity
        
        return [{"label": k, "intensity": v} for k, v in merged.items()]
    
    def add_base_emotion(self, emotion: str) -> None:
        """
        Add a new emotion to the base list.
        
        Args:
            emotion: emotion label to add
        """
        emotion = emotion.strip()
        if emotion and emotion not in self.base_emotions:
            self.base_emotions.append(emotion)
            logger.info(f"Added base emotion: {emotion}")
    
    def get_base_emotions(self) -> List[str]:
        """
        Get the list of base emotions.
        
        Returns:
            List of base emotion labels
        """
        return list(self.base_emotions)
    
    def get_emotion_color(self, emotion: str) -> str:
        """
        Get the color associated with an emotion.
        
        Args:
            emotion: emotion label
            
        Returns:
            Hex color code
        """
        return self.EMOTION_COLORS.get(emotion, '#90A4AE')
    
    def get_emotion_category(self, emotion: str) -> str:
        """
        Get the category of an emotion (positive/negative/neutral).
        
        Args:
            emotion: emotion label
            
        Returns:
            Category: 'positive', 'negative', or 'neutral'
        """
        emotion_lower = emotion.lower()
        for e in self.POSITIVE_EMOTIONS:
            if e.lower() == emotion_lower:
                return 'positive'
        for e in self.NEGATIVE_EMOTIONS:
            if e.lower() == emotion_lower:
                return 'negative'
        for e in self.NEUTRAL_EMOTIONS:
            if e.lower() == emotion_lower:
                return 'neutral'
        return 'neutral'
    
    def get_positive_emotions(self) -> List[str]:
        """Get list of positive emotions."""
        return list(self.POSITIVE_EMOTIONS)
    
    def get_negative_emotions(self) -> List[str]:
        """Get list of negative emotions."""
        return list(self.NEGATIVE_EMOTIONS)
    
    def get_neutral_emotions(self) -> List[str]:
        """Get list of neutral emotions."""
        return list(self.NEUTRAL_EMOTIONS)
    
    def get_all_emotions(self) -> List[str]:
        """Get all emotions across all categories."""
        return list(self.POSITIVE_EMOTIONS | self.NEGATIVE_EMOTIONS | self.NEUTRAL_EMOTIONS)