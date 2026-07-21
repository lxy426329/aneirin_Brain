# ============================================================
# Module: Common Utilities (utils.py)
# 模块：通用工具函数
#
# Provides config loading, logging init, path safety, ID generation, etc.
# 提供配置加载、日志初始化、路径安全校验、ID 生成等基础能力
#
# Depended on by: server.py, bucket_manager.py, dehydrator.py, decay_engine.py
# 被谁依赖：server.py, bucket_manager.py, dehydrator.py, decay_engine.py
# ============================================================

import os
import re
import uuid
import yaml
import logging
import unicodedata
from pathlib import Path
from datetime import datetime, timedelta, timezone


def load_config(config_path: str = None) -> dict:
    """
    Load configuration file.
    加载配置文件。

    Priority: environment variables > config.yaml > built-in defaults.
    优先级：环境变量 > config.yaml > 内置默认值。
    """
    # --- Built-in defaults (fallback so it runs even without config.yaml) ---
    # --- 内置默认配置（兜底，保证即使没有 config.yaml 也能跑）---
    defaults = {
        "transport": "stdio",
        "log_level": "INFO",
        "buckets_dir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets"),
        "merge_threshold": 75,
        "dehydration": {
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "max_tokens": 1024,
            "temperature": 0.1,
        },
        "decay": {
            "lambda": 0.05,
            "threshold": 0.3,
            "check_interval_hours": 24,
            "emotion_weights": {
                "base": 1.0,
                "arousal_boost": 0.8,
            },
        },
        "matching": {
            "fuzzy_threshold": 50,
            "max_results": 5,
        },
        "scoring_weights": {
            "emotion_arousal": 3.0,
            "explicit_priority": 2.0,
            "vector_similarity": 4.0,
            "topic_relevance": 5.0,
            "time_proximity": 1.5,
            "content_weight": 1.0,
        },
        "tag_normalization": {
            "enabled": True,
            "interval_hours": 168,
            "batch_threshold": 50,
            "min_tag_frequency": 2,
        },
        "activation": {
            "similarity_threshold": 0.75,    # 语义相似度阈值，低于此值不注入
            "cooldown_seconds": 300,          # 冷却时间（秒），默认5分钟
            "cooldown_decay_factor": 0.5,    # 冷却期内的降权系数（0.5=降权50%）
        },
    }

    # --- Load user config from YAML file ---
    # --- 从 YAML 文件加载用户自定义配置 ---
    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.yaml"
        )

    config = defaults.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = yaml.safe_load(f) or {}
            if isinstance(file_config, dict):
                config = _deep_merge(defaults, file_config)
            else:
                logging.warning(
                    f"Config file is not a valid YAML dict, using defaults / "
                    f"配置文件不是有效的 YAML 字典，使用默认配置: {config_path}"
                )
        except yaml.YAMLError as e:
            logging.warning(
                f"Failed to parse config file, using defaults / "
                f"配置文件解析失败，使用默认配置: {e}"
            )

    # --- Environment variable overrides (highest priority) ---
    # --- 环境变量覆盖敏感/运行时配置（优先级最高）---
    env_api_key = os.environ.get("deepseek_api_key", "") or os.environ.get("OMBRE_API_KEY", "")
    if env_api_key:
        config.setdefault("dehydration", {})["api_key"] = env_api_key

    env_base_url = os.environ.get("OMBRE_BASE_URL", "")
    if env_base_url:
        config.setdefault("dehydration", {})["base_url"] = env_base_url

    env_transport = os.environ.get("OMBRE_TRANSPORT", "")
    if env_transport:
        config["transport"] = env_transport

    env_buckets_dir = os.environ.get("OMBRE_BUCKETS_DIR", "")
    if env_buckets_dir:
        config["buckets_dir"] = env_buckets_dir

    # OMBRE_DEHYDRATION_MODEL (with OMBRE_MODEL alias) overrides dehydration.model
    env_dehy_model = os.environ.get("OMBRE_DEHYDRATION_MODEL", "") or os.environ.get("OMBRE_MODEL", "")
    if env_dehy_model:
        config.setdefault("dehydration", {})["model"] = env_dehy_model

    # OMBRE_DEHYDRATION_BASE_URL overrides dehydration.base_url
    env_dehy_base_url = os.environ.get("OMBRE_DEHYDRATION_BASE_URL", "")
    if env_dehy_base_url:
        config.setdefault("dehydration", {})["base_url"] = env_dehy_base_url

    # OMBRE_EMBEDDING_MODEL overrides embedding.model
    env_embed_model = os.environ.get("OMBRE_EMBEDDING_MODEL", "")
    if env_embed_model:
        config.setdefault("embedding", {})["model"] = env_embed_model

    # OMBRE_EMBEDDING_BASE_URL overrides embedding.base_url
    env_embed_base_url = os.environ.get("OMBRE_EMBEDDING_BASE_URL", "")
    if env_embed_base_url:
        config.setdefault("embedding", {})["base_url"] = env_embed_base_url

    # --- Ensure bucket storage directories exist ---
    # --- 确保记忆桶存储目录存在 ---
    buckets_dir = config["buckets_dir"]
    for subdir in ["permanent", "dynamic", "archive", "feel", "identity", "pattern"]:
        os.makedirs(os.path.join(buckets_dir, subdir), exist_ok=True)

    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Deep-merge two dicts; override values take precedence.
    深度合并两个字典，override 的值覆盖 base。
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def setup_logging(level: str = "INFO") -> None:
    """
    Initialize logging system.
    初始化日志系统。

    Note: In MCP stdio mode, stdout is occupied by the protocol;
    logs must go to stderr.
    注意：MCP stdio 模式下 stdout 被协议占用，日志只能走 stderr。
    """
    log_level = getattr(logging, level.upper(), None)
    if not isinstance(log_level, int):
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler()],  # StreamHandler defaults to stderr
    )


def generate_bucket_id(existing_ids: list = None) -> str:
    """
    Generate a unique bucket ID (12-char short UUID for readability).
    生成唯一的记忆桶 ID（12 位短 UUID，方便人类阅读）。
    
    Args:
        existing_ids: List of existing bucket IDs to check against (optional)
    """
    existing_ids = existing_ids or []
    for _ in range(10):
        candidate = uuid.uuid4().hex[:12]
        if candidate not in existing_ids:
            return candidate
    raise RuntimeError("Failed to generate unique bucket ID after 10 attempts")


def strip_wikilinks(text: str) -> str:
    """
    Remove Obsidian wikilink brackets: [[word]] → word
    去除 Obsidian 双链括号
    """
    return re.sub(r"\[\[([^\]]+)\]\]", r"\1", text) if text else text


def sanitize_name(name: str) -> str:
    """
    Sanitize bucket name for metadata storage.
    Prevents YAML parsing errors by removing problematic characters.
    
    Keeps: letters, numbers, Chinese chars, spaces, hyphens, colons, underscores
    Removes: quotes, slashes, backslashes, control characters
    """
    if not isinstance(name, str):
        return "unnamed"
    cleaned = re.sub(r'[^\w\s\u4e00-\u9fff\-:]', '', name, flags=re.UNICODE)
    cleaned = cleaned.strip()[:80]
    return cleaned if cleaned else "unnamed"


def sanitize_filename(name: str) -> str:
    """
    Sanitize bucket name for file system storage.
    More restrictive than sanitize_name because file systems have stricter rules.
    
    # Windows illegal chars: backslash, colon, asterisk, question mark, quotes, angle brackets, pipe
    # Unix illegal chars: slash, null
    
    Keeps: letters, numbers, Chinese chars, spaces, hyphens, underscores
    Removes: colons, quotes, slashes, backslashes, control characters, etc.
    """
    if not isinstance(name, str):
        return "unnamed"
    cleaned = re.sub(r'[^\w\s\u4e00-\u9fff\-]', '', name, flags=re.UNICODE)
    cleaned = cleaned.strip()[:80]
    return cleaned if cleaned else "unnamed"


def safe_path(base_dir: str, filename: str) -> Path:
    """
    Construct a safe file path, ensuring it stays within base_dir.
    Prevents directory traversal.
    构造安全的文件路径，确保最终路径始终在 base_dir 内部。
    """
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError(
            f"Path safety check failed / 路径安全检查失败: "
            f"{target} is not inside / 不在 {base} 内"
        )
    return target


def count_tokens_approx(text: str) -> int:
    """
    Rough token count estimate.
    粗略估算 token 数。

    Chinese ≈ 1 char = 1.5 tokens, English ≈ 1 word = 1.3 tokens.
    Used to decide whether dehydration is needed; precision not required.
    中文 ≈ 1字=1.5token，英文 ≈ 1词=1.3token。
    用于判断是否需要脱水压缩，不追求精确。
    """
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    return int(chinese_chars * 1.5 + english_words * 1.3 + len(text) * 0.05)


def now_iso() -> str:
    """
    Return current time as ISO format string with timezone offset.
    返回当前时间的 ISO 格式字符串（带时区偏移）。
    
    Always includes timezone information to prevent timezone-related
    bugs in cross-region deployments (e.g., cloud Docker in UTC vs 
    client in UTC+8).
    
    Format: "2026-07-22T16:00:00+08:00"
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_slice(text: str, start: int = 0, end: int = None) -> str:
    """
    Unicode-safe string slicing that preserves character boundaries.
    
    Python's string slicing operates on code points, but some visible
    characters (like Emoji 👨‍👩‍👧‍👦) consist of multiple code points.
    This function ensures we don't slice in the middle of such characters.
    
    Args:
        text: The input string
        start: Start index (default 0)
        end: End index (default None, meaning end of string)
    
    Returns:
        Safely sliced string
    """
    if not text:
        return ""
    
    if end is None:
        end = len(text)
    
    if start < 0:
        start = max(0, len(text) + start)
    if end < 0:
        end = max(0, len(text) + end)
    
    if start >= end:
        return ""
    
    result = text[start:end]
    
    if end < len(text):
        next_char = text[end]
        # Check if the next character is a combining mark or part of a surrogate pair
        # If so, we need to include it to complete the character
        while end < len(text) and unicodedata.combining(text[end]):
            result += text[end]
            end += 1
    
    return result


# ============================================================
# Timeline time conversion utilities
# 时间链时间转换工具
# ============================================================

# --- Regex patterns for Chinese relative time ---
_RELATIVE_TIME_PATTERNS = [
    # (pattern, extractor: lambda m -> timedelta or None)
    (re.compile(r"(\d+)\s*天前"), lambda m: timedelta(days=int(m.group(1)))),
    (re.compile(r"(\d+)\s*周前"), lambda m: timedelta(weeks=int(m.group(1)))),
    (re.compile(r"(\d+)\s*个月前"), lambda m: timedelta(days=int(m.group(1)) * 30)),
    (re.compile(r"(\d+)\s*月前"), lambda m: timedelta(days=int(m.group(1)) * 30)),
    (re.compile(r"(\d+)\s*年前"), lambda m: timedelta(days=int(m.group(1)) * 365)),
    (re.compile(r"前天"), lambda m: timedelta(days=2)),
    (re.compile(r"昨天"), lambda m: timedelta(days=1)),
    (re.compile(r"今天"), lambda m: timedelta(days=0)),
    (re.compile(r"上周"), lambda m: timedelta(weeks=1)),
    (re.compile(r"上个月"), lambda m: timedelta(days=30)),
    (re.compile(r"去年"), lambda m: timedelta(days=365)),
    # English patterns
    (re.compile(r"(\d+)\s*days?\s*ago", re.IGNORECASE), lambda m: timedelta(days=int(m.group(1)))),
    (re.compile(r"(\d+)\s*weeks?\s*ago", re.IGNORECASE), lambda m: timedelta(weeks=int(m.group(1)))),
    (re.compile(r"(\d+)\s*months?\s*ago", re.IGNORECASE), lambda m: timedelta(days=int(m.group(1)) * 30)),
    (re.compile(r"(\d+)\s*years?\s*ago", re.IGNORECASE), lambda m: timedelta(days=int(m.group(1)) * 365)),
    (re.compile(r"yesterday", re.IGNORECASE), lambda m: timedelta(days=1)),
    (re.compile(r"today", re.IGNORECASE), lambda m: timedelta(days=0)),
]

# --- Regex for absolute date patterns ---
_DATE_PATTERNS = [
    # YYYY-MM-DD
    re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"),
    # YYYY/MM/DD
    re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})"),
    # YYYY年MM月DD日
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
    # MM月DD日 (current year)
    re.compile(r"(\d{1,2})月(\d{1,2})日"),
]


def normalize_to_iso_date(time_str: str, reference_time: datetime = None) -> str:
    """
    Convert any time string (relative or absolute) to ISO-8601 date (YYYY-MM-DD).

    将任意时间字符串（相对或绝对）转换为 ISO-8601 日期格式（YYYY-MM-DD）。

    Handles:
    - Relative: "3天前", "昨天", "上周", "2 months ago"
    - Absolute: "2024-03-15", "2024年3月15日", "3月15日"

    Args:
        time_str: Input time string (may be relative or absolute)
        reference_time: Reference time for relative calculation (default: now)

    Returns:
        ISO-8601 date string "YYYY-MM-DD", or original string if parsing fails
    """
    if not time_str or not time_str.strip():
        return ""

    time_str = time_str.strip()
    ref = reference_time or datetime.now()

    # --- Try absolute date patterns first ---
    for pattern in _DATE_PATTERNS:
        match = pattern.search(time_str)
        if match:
            groups = match.groups()
            try:
                if len(groups) == 3:
                    year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                elif len(groups) == 2:
                    # MM月DD日 without year → use current year
                    year, month, day = ref.year, int(groups[0]), int(groups[1])
                else:
                    continue
                dt = datetime(year=year, month=month, day=day)
                return dt.strftime("%Y-%m-%d")
            except (ValueError, IndexError):
                continue

    # --- Try relative time patterns ---
    for pattern, extractor in _RELATIVE_TIME_PATTERNS:
        match = pattern.search(time_str)
        if match:
            try:
                delta = extractor(match)
                if delta is not None:
                    target = ref - delta
                    return target.strftime("%Y-%m-%d")
            except (ValueError, IndexError):
                continue

    # --- Already ISO format ---
    try:
        dt = datetime.fromisoformat(time_str)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        pass

    # --- Cannot parse, return original ---
    return time_str


def format_relative_time(iso_date_str: str, reference_time: datetime = None) -> str:
    """
    Convert an ISO-8601 date to a human-readable relative time description.

    将 ISO-8601 日期转换为人类可读的相对时间描述。

    Output examples:
    - "今天" (same day)
    - "昨天" (1 day ago)
    - "3天前"
    - "2周前"
    - "1个月前"
    - "3个月前"
    - "1年前"
    - "2024-03-15" (more than 1 year ago, show absolute date)

    Args:
        iso_date_str: ISO-8601 date string (YYYY-MM-DD or full ISO)
        reference_time: Reference time (default: now)

    Returns:
        Human-readable relative time string
    """
    if not iso_date_str or not iso_date_str.strip():
        return ""

    ref = reference_time or datetime.now()

    # --- Parse the ISO date ---
    try:
        # Try full ISO first, then date-only
        try:
            target = datetime.fromisoformat(iso_date_str)
        except ValueError:
            target = datetime.strptime(iso_date_str[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return iso_date_str  # Cannot parse, return original

    delta = ref - target
    total_days = delta.total_seconds() / 86400

    # --- Future dates or same day ---
    if total_days < 1:
        return "今天"

    if total_days < 2:
        return "昨天"

    if total_days < 7:
        days = int(total_days)
        return f"{days}天前"

    if total_days < 30:
        weeks = int(total_days / 7)
        return f"{weeks}周前"

    if total_days < 365:
        months = int(total_days / 30)
        return f"{months}个月前"

    years = int(total_days / 365)
    if years < 2:
        months = int((total_days % 365) / 30)
        if months > 0:
            return f"1年{months}个月前"
        return "1年前"

    # --- More than 2 years: show absolute date ---
    return target.strftime("%Y年%m月%d日")


# ============================================================
# Vulnerable state detection / 脆弱状态检测
# Used by breath() to auto-mask task_flag buckets when user is
# emotional / sick / exhausted, preventing the model from acting
# like a cold KPI machine pushing tasks.
# 在用户表达情绪 / 生病 / 疲惫时自动屏蔽任务类记忆桶
# ============================================================

# --- Sick / physical discomfort keywords ---
# --- 生病 / 身体不适关键词 ---
_SICK_KEYWORDS = [
    # Chinese
    "发烧", "头痛", "头疼", "肚子痛", "胃痛", "胃疼", "感冒", "咳嗽",
    "嗓子疼", "喉咙痛", "拉肚子", "腹泻", "痛经", "月经", "生理期",
    "生病", "不舒服", "不适", "恶心", "想吐", "呕吐", "晕", "眩晕",
    "牙痛", "牙疼", "腰痛", "腰酸", "背痛", "颈椎", "扭伤", "过敏",
    "发炎", "感染", "发烧了", "低烧", "高烧", "量体温", "吃药",
    "打针", "输液", "挂水", "住院", "看医生", "去医院", "急诊",
    "病了", "病倒", "养病", "复查", "复诊",
    # English
    "fever", "headache", "stomachache", "sick", "ill", "nausea",
    "vomit", "dizzy", "allergy", "cough", "sore throat",
    "menstrual", "period", "cramp", "hospital", "doctor",
]

# --- Exhausted / fatigue keywords ---
# --- 疲惫 / 劳累关键词 ---
_TIRED_KEYWORDS = [
    # Chinese
    "好累", "很累", "太累了", "累死了", "疲惫", "疲倦", "困死了",
    "困得不行", "想睡觉", "没力气", "没精力", "没精神", "心力交瘁",
    "心累", "身心俱疲", "撑不住", "熬不住", "熬夜", "失眠",
    "睡不着", "睡眠不足", "过度劳累", "疲劳", "乏", "乏力",
    "虚脱", "脱力", "透支", "元气大伤",
    # English
    "exhausted", "tired", "fatigued", "sleepy", "drained",
    "burnt out", "burnout", "insomnia", "can't sleep", "no energy",
    "worn out", "spent", "depleted",
]

# --- Emotional / distress keywords ---
# --- 情绪 / 低落关键词 ---
_EMOTIONAL_KEYWORDS = [
    # Chinese
    "难过", "伤心", "心痛", "心碎", "哭", "哭了", "想哭", "崩溃",
    "抑郁", "抑郁了", "焦虑", "焦虑症", "烦躁", "烦死了", "心烦",
    "失落", "沮丧", "低落", "情绪低落", "emo", "emo了",
    "孤独", "孤单", "寂寞", "空虚", "无助", "绝望", "崩溃了",
    "崩溃边缘", "撑不下去", "不想活", "想死", "自残", "心理压力",
    "压力大", "喘不过气", "窒息", "受不了", "受不了了",
    "委屈", "心酸", "心疼", "难受", "难受到", "心痛",
    # English
    "sad", "depressed", "anxiety", "anxious", "crying", "cried",
    "breakdown", "lonely", "hopeless", "misery", "miserable",
    "stressed", "overwhelmed", "empty", "worthless", "suicidal",
]


def detect_vulnerable_state(text: str) -> dict:
    """
    Detect if the user is in a vulnerable state (sick / tired / emotional).
    检测用户是否处于脆弱状态（生病 / 疲惫 / 情绪化）。

    When vulnerable, breath() will auto-mask all task_flag=True buckets
    to prevent the model from pushing tasks like a cold KPI machine.

    当检测到脆弱状态时，breath() 会自动屏蔽所有 task_flag=True 的桶，
    防止模型像个冰冷的 KPI 机器一样跑来催任务。

    Args:
        text: User's input text / 用户输入文本

    Returns:
        {
            "is_vulnerable": bool,  # True if any vulnerable state detected
            "state": "sick" | "tired" | "emotional" | "normal",
            "matched_keywords": list[str],  # What triggered the detection
            "reason": str,  # Human-readable reason
        }
    """
    if not text or not text.strip():
        return {
            "is_vulnerable": False,
            "state": "normal",
            "matched_keywords": [],
            "reason": "",
        }

    text_lower = text.lower()
    matched = []

    # --- Check sick keywords ---
    sick_hits = [kw for kw in _SICK_KEYWORDS if kw in text_lower]
    if sick_hits:
        matched.extend(sick_hits)

    # --- Check tired keywords ---
    tired_hits = [kw for kw in _TIRED_KEYWORDS if kw in text_lower]
    if tired_hits:
        matched.extend(tired_hits)

    # --- Check emotional keywords ---
    emotional_hits = [kw for kw in _EMOTIONAL_KEYWORDS if kw in text_lower]
    if emotional_hits:
        matched.extend(emotional_hits)

    if not matched:
        return {
            "is_vulnerable": False,
            "state": "normal",
            "matched_keywords": [],
            "reason": "",
        }

    # --- Determine primary state (priority: sick > emotional > tired) ---
    # --- 状态优先级：生病 > 情绪 > 疲惫 ---
    if sick_hits:
        state = "sick"
        reason = f"检测到生病/身体不适信号: {', '.join(sick_hits[:3])}"
    elif emotional_hits:
        state = "emotional"
        reason = f"检测到情绪信号: {', '.join(emotional_hits[:3])}"
    else:
        state = "tired"
        reason = f"检测到疲惫信号: {', '.join(tired_hits[:3])}"

    return {
        "is_vulnerable": True,
        "state": state,
        "matched_keywords": matched,
        "reason": reason,
    }
