# ============================================================
# Module: Dehydration & Auto-tagging (dehydrator.py)
# 模块：数据脱水压缩 + 自动打标
#
# Capabilities:
# 能力：
#   1. Dehydrate: compress memory content into high-density summaries (save tokens)
#      脱水：将记忆桶的原始内容压缩为高密度摘要，省 token
#   2. Merge: blend old and new content, keeping bucket size constant
#      合并：揉合新旧内容，控制桶体积恒定
#   3. Analyze: auto-analyze content for domain/emotion/tags
#      打标：自动分析内容，输出主题域/情感坐标/标签
#
# Operating modes:
# 工作模式：
#   - API only: OpenAI-compatible API (DeepSeek/Ollama/LM Studio/vLLM/Gemini etc.)
#     仅 API：通过 OpenAI 兼容客户端调用 LLM API
#   - Dehydration cache: SQLite persistent cache to avoid redundant API calls
#     脱水缓存：SQLite 持久缓存，避免重复调用 API
#
# Depended on by: server.py
# 被谁依赖：server.py
# ============================================================


import os
import re
import json
import hashlib
import sqlite3
import logging

from openai import AsyncOpenAI

from utils import count_tokens_approx

logger = logging.getLogger("ombre_brain.dehydrator")


# --- Dehydration prompt: instructs cheap LLM to compress information ---
# --- 脱水提示词：指导廉价 LLM 压缩信息 ---
DEHYDRATE_PROMPT = """你是一个信息压缩专家。请将以下内容脱水为紧凑摘要。

压缩规则：
1. 提取所有核心事实，去除冗余修饰和重复
2. 保留最新的情绪状态和态度
3. 保留所有待办/未完成事项
4. 关键数字、日期、名称必须保留
5. 目标压缩率 > 70%

输出格式（纯 JSON，无其他内容）：
{
  "core_facts": ["事实1", "事实2"],
  "emotion_state": "当前情绪关键词",
  "todos": ["待办1", "待办2"],
  "keywords": ["关键词1", "关键词2"],
  "summary": "50字以内的核心总结"
}"""

ONE_LINE_SUMMARY_PROMPT = """你是一个精炼摘要专家。请将以下内容压缩为20字以内的一句话摘要，只保留最核心的信息。

要求：
1. 严格控制在20字以内（含标点）
2. 必须包含核心人物/对象和关键动作/状态
3. 语言简洁，不使用修饰词
4. 直接输出摘要，不要加任何解释或额外内容

示例：
输入：今天下午和李明一起在公司会议室开项目评审会议，讨论了Q3的产品规划，李明提出了几个很好的建议。
输出：和李明开项目评审会讨论Q3规划"""


# --- Timeline prompt: analyze events and create chronological flow ---
# --- 时间链提示词：分析事件并创建时间顺序流程 ---
TIMELINE_PROMPT = """你是一个时间线梳理专家。请根据以下多个记忆事件，梳理出一个清晰的时间顺序流程。

梳理规则：
1. 将所有事件按照时间顺序排列
2. 识别事件之间的因果关系和发展脉络
3. 合并重复或相关的事件
4. 提取每个阶段的关键信息和情绪变化
5. 如果没有明确时间信息，根据内容推断相对顺序

时间格式要求（重要）：
- time 字段必须使用绝对日期，格式为 YYYY-MM-DD（如 2024-03-15）
- 如果事件有明确的时间戳，直接提取日期部分
- 如果只有相对时间（如"3天前"），请根据当前日期推算出绝对日期
- 不要使用"昨天"、"上周"等相对时间描述作为 time 字段值

输出格式（纯 JSON，无其他内容）：
{
  "title": "事件名称",
  "phases": [
    {
      "time": "2024-03-15",
      "description": "该阶段发生的事情",
      "key_points": ["关键点1", "关键点2"],
      "emotions": ["情绪1", "情绪2"]
    }
  ],
  "summary": "整个事件的总结"
}"""
DIGEST_PROMPT = """你是一个日记整理专家。用户会发送一段包含今天各种事情的文本（可能很杂乱），请你将其拆分成多个独立的记忆条目。

整理规则：
1. 每个条目应该是一个独立的主题/事件（不要混在一起）
2. 为每个条目自动分析元数据
3. 去除无意义的口水话和重复信息，保留核心内容
4. 同一主题的零散信息应合并为一个条目
5. 如果有待办事项，单独提取为一个条目
6. 单个条目内容不少于50字，过短的零碎信息合并到最相关的条目中
7. 总条目数控制在 2~6 个，避免过度碎片化
8. 在 content 中对人名、地名、专有名词用 [[双链]] 标记（如 [[婷易]]、[[Obsidian]]），普通词汇不要加

输出格式（纯 JSON 数组，无其他内容）：
[
  {
    "name": "条目标题（10字以内）",
    "content": "整理后的内容",
    "domain": ["主题域1"],
    "emotions": [{"label": "情绪1", "intensity": 0.8}, {"label": "情绪2", "intensity": 0.5}],
    "dominant_emotion": "情绪1",
    "tags": ["核心词1", "核心词2", "扩展词1", "扩展词2"],
    "importance": 5
  }
]

emotions 规则：提取 1~3 个最能描述条目的情绪标签及强度（0~1）。情绪标签请从以下列表中选择：
正面情绪：开心、喜悦、快乐、幸福、满足、自豪、感激、欣慰、憧憬、热爱、愉悦、振奋、得意、自信、兴奋、惊喜、感动、震撼、怀念、思念、牵挂、依恋、期待、好奇、惊讶
中性情绪：平静、平和、淡定、从容
负面情绪：难过、悲伤、伤心、痛苦、沮丧、失望、郁闷、压抑、担忧、紧张、不安、孤独、无助、迷茫、困惑、焦虑、恐惧、害怕、愤怒、生气、烦躁、怨恨、嫉妒、羡慕、愧疚、自责、悔恨、遗憾、失落、委屈、难堪、尴尬、羞愧
intensity 表示情绪强度，0=无，1=极强，根据内容情绪强烈程度判断

tags 生成规则：先从原文精准提取 3~5 个核心词，再引申扩展 5~8 个语义相关词（近义词、上位词、关联场景词），合并为一个数组。

主题域可选（选最精确的 1~2 个，只选真正相关的）：
  日常: ["饮食", "穿搭", "出行", "居家", "购物"]
  人际: ["家庭", "恋爱", "友谊", "社交"]
  成长: ["工作", "学习", "考试", "求职"]
  身心: ["健康", "心理", "睡眠", "运动"]
  兴趣: ["游戏", "影视", "音乐", "阅读", "创作", "手工"]
  数字: ["编程", "AI", "硬件", "网络"]
  事务: ["财务", "计划", "待办"]
  内心: ["情绪", "回忆", "梦境", "自省"]
importance: 1-10，根据内容重要程度判断
valence: 0~1（0=消极, 0.5=中性, 1=积极）
arousal: 0~1（0=平静, 0.5=普通, 1=激动）"""


# --- Merge prompt: instruct LLM to blend old and new memories ---
# --- 合并提示词：指导 LLM 揉合新旧记忆 ---
MERGE_PROMPT = """你是一个信息合并专家。请将旧记忆与新内容合并为一份统一的简洁记录。

合并规则：
1. 新内容与旧记忆冲突时，以新内容为准
2. 去除重复信息
3. 保留所有重要事实
4. 总长度尽量不超过旧记忆的 120%
5. 对出现的人名、地名、专有名词用 [[双链]] 标记（如 [[婷易]]、[[Obsidian]]），普通词汇不要加

直接输出合并后的文本，不要加额外说明。"""


# --- Auto-tagging prompt: analyze content for domain and emotion coords ---
# --- 自动打标提示词：分析内容的主题域和情感坐标 ---
ANALYZE_PROMPT = """你是一个内容分析器。请分析以下文本，输出结构化的元数据。

分析规则：
1. domain（主题域）：选最精确的 1~2 个，只选真正相关的
   日常: ["饮食", "穿搭", "出行", "居家", "购物"]
   人际: ["家庭", "恋爱", "友谊", "社交"]
   成长: ["工作", "学习", "考试", "求职"]
   身心: ["健康", "心理", "睡眠", "运动"]
   兴趣: ["游戏", "影视", "音乐", "阅读", "创作", "手工"]
   数字: ["编程", "AI", "硬件", "网络"]
   事务: ["财务", "计划", "待办"]
   内心: ["情绪", "回忆", "梦境", "自省"]

2. emotions（情绪数组）：提取 1~3 个最能描述文本情绪的标签及详细指标
   格式: [{"label": "情绪标签", "intensity": 0.0~1.0, "polarity": "positive/negative/neutral", "arousal_level": "low/medium/high", "duration": "momentary/short/long"}, ...]
   
   情绪标签请从普鲁奇克情绪轮中选择：
   正面情绪：喜悦、信任、期待、惊讶、乐观、自信、感激、满足、自豪、热爱、愉悦、振奋、感动、怀念、牵挂、依恋
   负面情绪：悲伤、厌恶、愤怒、恐惧、忧虑、沮丧、失望、焦虑、愧疚、悔恨、遗憾、失落、孤独、无助、困惑
   中性情绪：平静、平和、淡定、从容、好奇、思考、专注
   
   指标说明：
   - intensity：情绪强度，0=无，1=极强（0.0-0.3=弱，0.3-0.6=中，0.6-0.8=强，0.8-1.0=极强）
   - polarity：情绪极性，positive=正面，negative=负面，neutral=中性
   - arousal_level：唤醒水平，low=低唤醒（平静、放松），medium=中唤醒（一般情绪），high=高唤醒（强烈情绪）
   - duration：持续时间感知，momentary=瞬间，short=短期，long=长期

3. dominant_emotion（主导情绪）：从 emotions 中选一个最核心的情绪标签

4. emotion_metrics（情绪强度综合指标）：
   - overall_intensity：整体情绪强度（0~1，所有情绪强度的加权平均）
   - emotional_range：情绪波动范围（0~1，情绪多样性和变化程度）
   - emotional_valence：情绪效价（-1~1，正值偏向正面，负值偏向负面）

5. tags（标签）：分为两类，合并为一个数组：
   A. 固定泛化标签（必选 1~3 个）：从以下列表中选择最相关的
      ["工作", "学习", "生活", "健康", "人际关系", "兴趣爱好", "财务", "内心世界", "数字技术", "事务管理", "休闲娱乐", "家庭", "情感", "成长", "创造"]
   B. 具体内容标签（选 3~5 个）：从原文提取的具体关键词
   总计 4~8 个标签

6. suggested_name（建议桶名）：8~12字的精炼标题，必须满足以下要求：
   - 使用动宾结构或主谓结构，如"学习Python"、"和朋友聚餐"、"心情低落"
   - 包含核心动作或状态，让读者一眼明白内容主题
   - 不要用泛泛的词汇，要有具体的对象或动作
   - 避免重复domain中的词作为开头
   - 如果文本是对话或思考，提炼核心主题作为标题
   - 优先使用动词+名词的组合，如"完成项目"、"制定计划"、"阅读书籍"

7. 在 tags 和 suggested_name 中不要使用 [[]] 双链标记

输出格式（纯 JSON，无其他内容）：
{
  "domain": ["主题域1", "主题域2"],
  "emotions": [{"label": "情绪1", "intensity": 0.8, "polarity": "positive", "arousal_level": "high", "duration": "short"}, {"label": "情绪2", "intensity": 0.5, "polarity": "negative", "arousal_level": "medium", "duration": "momentary"}],
  "dominant_emotion": "情绪1",
  "emotion_metrics": {"overall_intensity": 0.65, "emotional_range": 0.7, "emotional_valence": 0.3},
  "tags": ["泛化标签1", "泛化标签2", "具体标签1", "具体标签2", "..."],
  "suggested_name": "精炼标题"
}"""


class Dehydrator:
    """
    Data dehydrator + content analyzer.
    Three capabilities: dehydration / merge / auto-tagging (domain + emotion).
    API-only: every public method requires a working LLM API.
    If the API is unavailable, methods raise RuntimeError so callers can
    surface the failure to the user instead of silently producing low-quality results.
    数据脱水器 + 内容分析器。
    三大能力：脱水压缩 / 新旧合并 / 自动打标。
    仅走 API：API 不可用时直接抛出 RuntimeError，调用方明确感知。
    （根据 BEHAVIOR_SPEC.md 三、降级行为表决策：无本地降级）
    """

    def __init__(self, config: dict):
        # --- Read dehydration API config / 读取脱水 API 配置 ---
        dehy_cfg = config.get("dehydration", {})
        self.api_key = dehy_cfg.get("api_key", "") or os.environ.get("OMBRE_API_KEY", "")
        self.model = dehy_cfg.get("model", "deepseek-chat")
        self.base_url = dehy_cfg.get("base_url", "https://api.deepseek.com/v1")
        self.max_tokens = dehy_cfg.get("max_tokens", 1024)
        self.temperature = dehy_cfg.get("temperature", 0.1)

        # --- API availability / 是否有可用的 API ---
        self.api_available = bool(self.api_key)

        # --- Initialize OpenAI-compatible client ---
        # --- 初始化 OpenAI 兼容客户端 ---
        if self.api_available:
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=60.0,
            )
        else:
            self.client = None

        # --- SQLite dehydration cache ---
        # --- SQLite 脱水缓存：content hash → summary ---
        db_path = os.path.join(config["buckets_dir"], "dehydration_cache.db")
        self.cache_db_path = db_path
        self._init_cache_db()

    def _get_cache_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.cache_db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_cache_db(self):
        """Create dehydration cache table if not exists."""
        os.makedirs(os.path.dirname(self.cache_db_path), exist_ok=True)
        conn = self._get_cache_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dehydration_cache (
                content_hash TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()

    def _get_cached_summary(self, content: str) -> str | None:
        """Look up cached dehydration result by content hash."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        conn = self._get_cache_connection()
        row = conn.execute(
            "SELECT summary FROM dehydration_cache WHERE content_hash = ?",
            (content_hash,)
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def _set_cached_summary(self, content: str, summary: str):
        """Store dehydration result in cache."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        conn = self._get_cache_connection()
        conn.execute(
            "INSERT OR REPLACE INTO dehydration_cache (content_hash, summary, model) VALUES (?, ?, ?)",
            (content_hash, summary, self.model)
        )
        conn.commit()
        conn.close()

    def invalidate_cache(self, content: str):
        """Remove cached summary for specific content (call when bucket content changes)."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        conn = self._get_cache_connection()
        conn.execute("DELETE FROM dehydration_cache WHERE content_hash = ?", (content_hash,))
        conn.commit()
        conn.close()

    # ---------------------------------------------------------
    # Dehydrate: compress raw content into concise summary
    # 脱水：将原始内容压缩为精简摘要
    # API only (no local fallback)
    # 仅通过 API 脱水（无本地回退）
    # ---------------------------------------------------------
    async def dehydrate(self, content: str, metadata: dict = None, brief: bool = False) -> str:
        """
        Dehydrate/compress memory content.
        Returns formatted summary string ready for Claude context injection.
        Uses SQLite cache to avoid redundant API calls.
        对记忆内容做脱水压缩。
        返回格式化的摘要字符串，可直接注入 Claude 上下文。
        使用 SQLite 缓存避免重复调用 API。
        
        Args:
            content: 原始内容
            metadata: 桶元数据
            brief: 是否返回简洁格式（仅元数据头 + summary）
        """
        if not content or not content.strip():
            return "（空记忆 / empty memory）"

        # --- Content is short enough, no compression needed ---
        # --- 内容已经很短，不需要压缩 ---
        if count_tokens_approx(content) < 100:
            return self._format_output(content, metadata, brief=brief)

        # --- Check cache first ---
        # --- 先查缓存 ---
        cached = self._get_cached_summary(content)
        if cached:
            return self._format_output(cached, metadata, brief=brief)

        # --- API dehydration (no local fallback) ---
        # --- API 脱水（无本地降级）---
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，请配置 OMBRE_API_KEY")

        result = await self._api_dehydrate(content)
        # --- Cache the result ---
        self._set_cached_summary(content, result)
        return self._format_output(result, metadata, brief=brief)

    # ---------------------------------------------------------
    # Merge: blend new content into existing bucket
    # 合并：将新内容揉入已有桶，保持体积恒定
    # ---------------------------------------------------------
    async def merge(self, old_content: str, new_content: str) -> str:
        """
        Merge new content with old memory, preventing infinite bucket growth.
        将新内容与旧记忆合并，避免桶无限膨胀。
        """
        if not old_content and not new_content:
            return ""
        if not old_content:
            return new_content or ""
        if not new_content:
            return old_content

        # --- API merge (no local fallback) ---
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，请检查 config.yaml 中的 dehydration 配置")
        try:
            result = await self._api_merge(old_content, new_content)
            if result:
                return result
            raise RuntimeError("API 合并返回空结果")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"API 合并失败，请检查 API 连接: {e}") from e

    # ---------------------------------------------------------
    # API call: dehydration
    # API 调用：脱水压缩
    # ---------------------------------------------------------
    async def _api_dehydrate(self, content: str) -> str:
        """
        Call LLM API for intelligent dehydration (via OpenAI-compatible client).
        调用 LLM API 执行智能脱水。
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": DEHYDRATE_PROMPT},
                {"role": "user", "content": content[:3000]},
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

    # ---------------------------------------------------------
    # --- API call: merge
    # API 调用：合并
    # ---------------------------------------------------------
    async def _api_merge(self, old_content: str, new_content: str) -> str:
        """
        Call LLM API for intelligent merge (via OpenAI-compatible client).
        调用 LLM API 执行智能合并。
        """
        user_msg = f"旧记忆：\n{old_content[:2000]}\n\n新内容：\n{new_content[:2000]}"
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": MERGE_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

    # ---------------------------------------------------------
    # Generate one-line summary (20 chars max)
    # 生成一句话摘要（最多20字）
    # ---------------------------------------------------------
    async def generate_one_line_summary(self, content: str) -> str:
        """
        Generate a concise one-line summary (max 20 characters).
        Used for quick display in memory network and search results.
        生成简洁的一句话摘要（最多20字）。
        用于记忆网络和搜索结果中的快速展示。
        
        Args:
            content: 原始内容
        Returns:
            20字以内的一句话摘要
        """
        if not content or not content.strip():
            return ""
        
        content = content.strip()[:1000]
        
        if len(content) <= 20:
            return content
        
        if not self.api_available:
            return content[:20]
        
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": ONE_LINE_SUMMARY_PROMPT},
                    {"role": "user", "content": content},
                ],
                max_tokens=30,
                temperature=0.1,
            )
            if not response.choices:
                return content[:20]
            
            summary = response.choices[0].message.content or ""
            summary = summary.strip()[:20]
            return summary if summary else content[:20]
        except Exception as e:
            logger.warning(f"Failed to generate one-line summary: {e}")
            return content[:20]


    # ---------------------------------------------------------
    # Output formatting
    # 输出格式化
    # Wraps dehydrated result with bucket name, tags, emotion coords
    # 把脱水结果包装成带桶名、标签、情感坐标的可读文本
    # ---------------------------------------------------------
    def _format_output(self, content: str, metadata: dict = None, brief: bool = False) -> str:
        """
        Format dehydrated result into context-injectable text.
        将脱水结果格式化为可注入上下文的文本。
        
        Args:
            content: 脱水后的内容（可能是 JSON 或纯文本）
            metadata: 桶元数据
            brief: 是否返回简洁格式（仅元数据头 + summary）
        """
        header = ""
        if metadata and isinstance(metadata, dict):
            name = metadata.get("name", "未命名")
            domains = ", ".join(metadata.get("domain", []))
            header = f"📌 记忆桶: {name}"
            if domains:
                header += f" [主题:{domains}]"
            
            emotions = metadata.get("emotions", [])
            if emotions:
                emotion_str = ", ".join(f"{e['label']}({e['intensity']:.1f})" for e in emotions)
                header += f" [情感:{emotion_str}]"
            else:
                try:
                    valence = float(metadata.get("valence", 0.5))
                    arousal = float(metadata.get("arousal", 0.3))
                    header += f" [情感:V{valence:.1f}/A{arousal:.1f}]"
                except (ValueError, TypeError):
                    pass
            
            model_v = metadata.get("model_valence")
            if model_v is not None:
                try:
                    header += f" [我的视角:V{float(model_v):.1f}]"
                except (ValueError, TypeError):
                    pass
            if metadata.get("digested"):
                header += " [已消化]"
            header += "\n"
        
        # Remove wikilinks for display
        content = re.sub(r'\[\[([^\]]+)\]\]', r'\1', content)
        
        # If brief mode, try to extract summary from JSON content
        # brief 模式下，尝试从 JSON 中提取 summary
        if brief:
            summary = self._extract_summary(content)
            if summary:
                return f"{header}→ {summary}"
        
        return f"{header}{content}"
    
    def _extract_summary(self, content: str) -> str | None:
        """
        Extract summary field from JSON content.
        从 JSON 内容中提取 summary 字段。
        
        Args:
            content: 可能是 JSON 格式的内容
            
        Returns:
            summary 字符串，如果解析失败则返回 None
        """
        try:
            cleaned = content.strip()
            # Handle potential markdown code block wrapping
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            result = json.loads(cleaned)
            if isinstance(result, dict) and "summary" in result:
                return result["summary"]
        except (json.JSONDecodeError, IndexError, ValueError):
            pass
        return None

    # ---------------------------------------------------------
    # Auto-tagging: analyze content for domain + emotion + tags
    # 自动打标：分析内容，输出主题域 + 情感坐标 + 标签
    # Called by server.py when storing new memories
    # 存新记忆时由 server.py 调用
    # ---------------------------------------------------------
    async def analyze(self, content: str) -> dict:
        """
        Analyze content and return structured metadata.
        分析内容，返回结构化元数据。

        Returns: {"domain", "valence", "arousal", "tags", "suggested_name"}
        """
        if not content or not content.strip():
            return self._default_analysis()

        # --- API analyze (no local fallback) ---
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，请检查 config.yaml 中的 dehydration 配置")
        try:
            result = await self._api_analyze(content)
            if result:
                return result
            raise RuntimeError("API 打标返回空结果")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"API 打标失败，请检查 API 连接: {e}") from e

    # ---------------------------------------------------------
    # API call: auto-tagging
    # API 调用：自动打标
    # ---------------------------------------------------------
    async def _api_analyze(self, content: str) -> dict:
        """
        Call LLM API for content analysis / tagging.
        调用 LLM API 执行内容分析打标。
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": ANALYZE_PROMPT},
                {"role": "user", "content": content[:2000]},
            ],
            max_tokens=256,
            temperature=0.1,
        )
        if not response.choices:
            return self._default_analysis()
        raw = response.choices[0].message.content or ""
        if not raw.strip():
            return self._default_analysis()
        return self._parse_analysis(raw)

    # ---------------------------------------------------------
    # Parse API JSON response with safety checks
    # 解析 API 返回的 JSON，做安全校验
    # Ensure valence/arousal in 0~1, domain/tags valid
    # ---------------------------------------------------------
    def _parse_analysis(self, raw: str) -> dict:
        """
        Parse and validate API tagging result.
        解析并校验 API 返回的打标结果。
        """
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            result = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"API tagging JSON parse failed / JSON 解析失败: {raw[:200]}")
            return self._default_analysis()

        if not isinstance(result, dict):
            return self._default_analysis()

        emotions = []
        raw_emotions = result.get("emotions", [])
        if isinstance(raw_emotions, list):
            for e in raw_emotions[:3]:
                if isinstance(e, dict) and "label" in e:
                    intensity = float(e.get("intensity", 0.5))
                    polarity = e.get("polarity", "neutral")
                    arousal_level = e.get("arousal_level", "medium")
                    duration = e.get("duration", "short")
                    emotions.append({
                        "label": str(e["label"]),
                        "intensity": max(0.0, min(1.0, intensity)),
                        "polarity": polarity,
                        "arousal_level": arousal_level,
                        "duration": duration,
                    })

        dominant_emotion = ""
        if emotions:
            dominant_emotion = emotions[0]["label"]
        else:
            dominant_emotion = result.get("dominant_emotion", "")

        emotion_metrics = result.get("emotion_metrics", {})
        if not isinstance(emotion_metrics, dict):
            emotion_metrics = {}

        overall_intensity = float(emotion_metrics.get("overall_intensity", 0.5))
        overall_intensity = max(0.0, min(1.0, overall_intensity))

        emotional_range = float(emotion_metrics.get("emotional_range", 0.0))
        emotional_range = max(0.0, min(1.0, emotional_range))

        emotional_valence = float(emotion_metrics.get("emotional_valence", 0.0))
        emotional_valence = max(-1.0, min(1.0, emotional_valence))

        tags = result.get("tags", [])[:10]

        return {
            "domain": result.get("domain", ["未分类"])[:3],
            "emotions": emotions,
            "dominant_emotion": dominant_emotion,
            "emotion_metrics": {
                "overall_intensity": overall_intensity,
                "emotional_range": emotional_range,
                "emotional_valence": emotional_valence,
            },
            "tags": tags,
            "suggested_name": str(result.get("suggested_name", ""))[:20],
        }

    # ---------------------------------------------------------
    # Default analysis result (empty content or total failure)
    # 默认分析结果（内容为空或完全失败时用）
    # ---------------------------------------------------------
    def _default_analysis(self) -> dict:
        """
        Return default neutral analysis result.
        返回默认的中性分析结果。
        """
        return {
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

    # ---------------------------------------------------------
    # Timeline: analyze events and create chronological flow
    # 时间链：分析事件并创建时间顺序流程
    # ---------------------------------------------------------
    async def timeline(self, events: list[dict]) -> dict:
        """
        Analyze a list of events and create a chronological timeline flow.
        分析一系列事件，创建时间顺序流程。

        Args:
            events: List of event dicts with at least "content" field
        
        Returns: {"title", "phases", "summary"}
        """
        if not events or len(events) == 0:
            return {"title": "无事件", "phases": [], "summary": "没有可用的事件数据"}

        # --- API timeline (no local fallback) ---
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，请检查 config.yaml 中的 dehydration 配置")
        try:
            result = await self._api_timeline(events)
            if result:
                return result
            raise RuntimeError("API 时间链返回空结果")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"API 时间链失败，请检查 API 连接: {e}") from e

    # ---------------------------------------------------------
    # API call: timeline
    # API 调用：时间链
    # ---------------------------------------------------------
    async def _api_timeline(self, events: list[dict]) -> dict:
        """
        Call LLM API for timeline generation.
        调用 LLM API 生成时间链。
        """
        event_texts = []
        for i, event in enumerate(events):
            content = event.get("content", "")
            name = event.get("name", event.get("topic", ""))
            time = event.get("created", event.get("time", ""))
            emotions = event.get("emotions", [])
            emotion_str = ""
            if emotions:
                emotion_str = " [情感: " + ", ".join(f"{e['label']}({e['intensity']:.1f})" for e in emotions) + "]"
            
            event_texts.append(f"事件{i+1}：{'【' + name + '】' if name else ''}{content}{emotion_str}{' [' + time + ']' if time else ''}")
        
        user_msg = "\n\n".join(event_texts)
        
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": TIMELINE_PROMPT},
                {"role": "user", "content": user_msg[:5000]},
            ],
            max_tokens=2048,
            temperature=0.1,
        )
        if not response.choices:
            return {"title": "无结果", "phases": [], "summary": "API 返回空"}
        raw = response.choices[0].message.content or ""
        if not raw.strip():
            return {"title": "无结果", "phases": [], "summary": "API 返回空"}
        return self._parse_timeline(raw)

    # ---------------------------------------------------------
    # Parse timeline result with safety checks
    # 解析时间链结果，做安全校验
    # ---------------------------------------------------------
    def _parse_timeline(self, raw: str) -> dict:
        """
        Parse and validate API timeline result.
        解析并校验 API 返回的时间链结果。
        """
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            result = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"Timeline JSON parse failed / JSON 解析失败: {raw[:200]}")
            return {"title": "解析失败", "phases": [], "summary": "无法解析 API 返回结果"}

        if not isinstance(result, dict):
            return {"title": "解析失败", "phases": [], "summary": "API 返回格式错误"}

        phases = []
        raw_phases = result.get("phases", [])
        if isinstance(raw_phases, list):
            for phase in raw_phases[:20]:
                if isinstance(phase, dict):
                    phases.append({
                        "time": str(phase.get("time", "")),
                        "description": str(phase.get("description", "")),
                        "key_points": [str(k) for k in (phase.get("key_points", [])[:5])],
                        "emotions": [str(e) for e in (phase.get("emotions", [])[:3])],
                    })

        return {
            "title": str(result.get("title", "未命名事件")),
            "phases": phases,
            "summary": str(result.get("summary", "")),
        }

    # ---------------------------------------------------------
    # Diary digest: split daily notes into independent memory entries
    # 日记整理：把一大段日常拆分成多个独立记忆条目
    # For the "grow" tool — "dump a day's content and it gets organized"
    # 给 grow 工具用，"一天结束发一坨内容"靠这个
    # ---------------------------------------------------------
    async def digest(self, content: str) -> list[dict]:
        """
        Split a large chunk of daily content into independent memory entries.
        将一大段日常内容拆分成多个独立记忆条目。

        Returns: [{"name", "content", "domain", "valence", "arousal", "tags", "importance"}, ...]
        """
        if not content or not content.strip():
            return []

        # --- API digest (no local fallback) ---
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，请检查 config.yaml 中的 dehydration 配置")
        try:
            result = await self._api_digest(content)
            if result:
                return result
            raise RuntimeError("API 日记整理返回空结果")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"API 日记整理失败，请检查 API 连接: {e}") from e

    # ---------------------------------------------------------
    # API call: diary digest
    # API 调用：日记整理
    # ---------------------------------------------------------
    async def _api_digest(self, content: str) -> list[dict]:
        """
        Call LLM API for diary organization.
        调用 LLM API 执行日记整理。
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": DIGEST_PROMPT},
                {"role": "user", "content": content[:5000]},
            ],
            max_tokens=2048,
            temperature=0.0,
        )
        if not response.choices:
            return []
        raw = response.choices[0].message.content or ""
        if not raw.strip():
            return []
        return self._parse_digest(raw)

    # ---------------------------------------------------------
    # Parse diary digest result with safety checks
    # 解析日记整理结果，做安全校验
    # ---------------------------------------------------------
    def _parse_digest(self, raw: str) -> list[dict]:
        """
        Parse and validate API diary digest result.
        解析并校验 API 返回的日记整理结果。
        """
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            items = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"Diary digest JSON parse failed / JSON 解析失败: {raw[:200]}")
            return []

        if not isinstance(items, list):
            return []

        validated = []
        for item in items:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            try:
                importance = max(1, min(10, int(item.get("importance", 5))))
            except (ValueError, TypeError):
                importance = 5

            emotions = []
            raw_emotions = item.get("emotions", [])
            if isinstance(raw_emotions, list):
                for e in raw_emotions[:3]:
                    if isinstance(e, dict) and "label" in e:
                        intensity = float(e.get("intensity", 0.5))
                        emotions.append({
                            "label": str(e["label"]),
                            "intensity": max(0.0, min(1.0, intensity)),
                        })
            
            dominant_emotion = ""
            if emotions:
                dominant_emotion = emotions[0]["label"]
            else:
                dominant_emotion = item.get("dominant_emotion", "")

            validated.append({
                "name": str(item.get("name", ""))[:20],
                "content": str(item.get("content", "")),
                "domain": item.get("domain", ["未分类"])[:3],
                "emotions": emotions,
                "dominant_emotion": dominant_emotion,
                "tags": item.get("tags", [])[:15],
                "importance": importance,
            })
        return validated
