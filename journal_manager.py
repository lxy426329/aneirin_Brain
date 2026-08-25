# ============================================================
# Module: Journal Manager (journal_manager.py)
# 模块：日记管理器
#
# Daily journal entries storage and retrieval.
# 每日日记条目的存储与检索。
#
# Core design:
# 核心逻辑：
#   - Each journal entry = one JSON file named by date (YYYY-MM-DD.json)
#     每条日记 = 一个以日期命名的 JSON 文件
#   - Stored in {base_dir}/journals/ (sibling of buckets_dir)
#     存储在 {base_dir}/journals/（与 buckets_dir 同级）
#   - Completely separate from the memory bucket system:
#     完全独立于记忆桶系统：
#       * NOT returned in breath() or list_all()
#       * Only accessible via explicit date query or keyword search
#       * 不参与 breath() / list_all() 记忆检索
#       * 仅通过显式日期查询或关键字搜索访问
#
# Depended on by: server.py (planned)
# 被谁依赖：server.py（计划中）
# ============================================================

import os
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class JournalManager:
    """
    Daily journal manager — stores one JSON entry per day.
    日记管理器 —— 每天存储一个 JSON 条目。

    Journals are isolated from the memory bucket system:
    日记与记忆桶系统完全隔离：
      - Not included in breath() / list_all() / search()
      - Only accessible via explicit date query or keyword search
      - 不出现在 breath() / list_all() / search() 结果中
      - 仅通过显式日期查询或关键字搜索访问
    """

    def __init__(self, base_dir: str = None, emotion_mgr=None):
        # --- Resolve base_dir: explicit param > parent of buckets_dir (from config) ---
        # --- 解析 base_dir：显式参数 > 从配置读取的 buckets_dir 父目录 ---
        # Consistent with bucket_manager.py: buckets_dir is read from config
        # (OMBRE_BUCKETS_DIR env var > config.yaml > built-in default).
        # 与 bucket_manager.py 一致：buckets_dir 从配置读取
        # （环境变量 OMBRE_BUCKETS_DIR > config.yaml > 内置默认值）。
        if base_dir is None:
            try:
                from utils import load_config
                config = load_config()
                buckets_dir = config.get("buckets_dir", "")
                if buckets_dir:
                    # journals live as a sibling of buckets/ (same parent)
                    # journals 与 buckets/ 同级（共用父目录）
                    base_dir = os.path.dirname(buckets_dir) or "."
                else:
                    base_dir = "."
            except Exception as e:
                logger.warning(
                    f"Failed to load config for journal base_dir, falling back to cwd / "
                    f"加载配置失败，回退到当前目录: {e}"
                )
                base_dir = "."

        self.base_dir = base_dir
        # --- Journals directory: {base_dir}/journals/ ---
        # --- 日记目录：{base_dir}/journals/ ---
        self.journals_dir = os.path.join(self.base_dir, "journals")
        os.makedirs(self.journals_dir, exist_ok=True)

        # --- Emotion manager for emotion_tags synonym merging (optional) ---
        # --- 情绪管理器：日记情绪标签同义词归并（可选）---
        self.emotion_mgr = emotion_mgr

        # --- Thread lock for idempotent read-modify-write (merge mode) ---
        # --- 线程锁：保证合并模式的读-改-写原子性，防止并发丢失更新 ---
        self._lock = threading.Lock()

    # ---------------------------------------------------------
    # Normalize date: empty/invalid → graceful degradation to current UTC date
    # 日期归一化：空值/格式错乱 → 优雅降级为当前 UTC 日期
    # ---------------------------------------------------------
    def _normalize_date(self, date: str) -> str:
        """Return a valid YYYY-MM-DD string, defaulting to current UTC date.
        Also accepts common alt formats (YYYY/M/D, YYYY年M月D日).
        返回合法 YYYY-MM-DD 日期，无法解析时降级为当前 UTC 日期。"""
        if date and isinstance(date, str):
            d = date.strip()
            if self._validate_date(d):
                return d
            # --- Try common alternative formats / 尝试常见替代格式 ---
            for fmt in ("%Y/%m/%d", "%Y年%m月%d日", "%Y.%m.%d"):
                try:
                    parsed = datetime.strptime(d, fmt)
                    return parsed.strftime("%Y-%m-%d")
                except ValueError:
                    continue
            logger.warning(
                f"Invalid journal date '{date}', falling back to current UTC date / "
                f"日记日期无效，降级为当前 UTC 日期"
            )
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ---------------------------------------------------------
    # Normalize emotion_tags: str / list / None → deduped comma-separated str
    # 情绪标签归一化：str / list / None → 去重后的逗号分隔字符串
    # ---------------------------------------------------------
    def _normalize_emotion_tags(self, emotion_tags) -> str:
        """Accept a comma-separated string or a list; strip, filter empties, dedup.
        兼容逗号分隔字符串与列表；去除空值与重复项。"""
        if emotion_tags is None:
            return ""
        if isinstance(emotion_tags, str):
            raw = emotion_tags
        elif isinstance(emotion_tags, (list, tuple, set)):
            raw = ",".join(str(t) for t in emotion_tags if t is not None)
        else:
            raw = str(emotion_tags)
        parts = [p.strip() for p in raw.replace("，", ",").replace("、", ",").split(",")]
        return ",".join(dict.fromkeys(p for p in parts if p))

    async def _merge_emotion_tags(self, emotion_tags: str) -> str:
        """
        Merge emotion tags via emotion_manager synonym normalization.
        Non-matching tags are kept as-is (non-destructive).
        通过 emotion_manager 对情绪标签做同义词归并；无匹配的标签原样保留（非破坏性）。
        """
        tags = [t.strip() for t in emotion_tags.split(",") if t.strip()]
        if not tags:
            return emotion_tags
        try:
            merged = await self.emotion_mgr.merge_tags(tags)
        except Exception as e:
            logger.warning(
                f"Emotion tag merge failed, keeping original / "
                f"情绪标签归并失败，保留原值: {e}"
            )
            return emotion_tags
        return ",".join(merged)

    def _validate_date(self, date: str) -> bool:
        """Returns True if date matches YYYY-MM-DD format. 校验日期是否为 YYYY-MM-DD。"""
        if not date or not isinstance(date, str):
            return False
        try:
            datetime.strptime(date, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def _entry_path(self, date: str) -> str:
        """Return the file path for a given date's journal entry. 返回指定日期日记的文件路径。"""
        # date is pre-validated as YYYY-MM-DD, safe to use directly as filename
        # date 已校验为 YYYY-MM-DD 格式，可直接用作文件名
        return os.path.join(self.journals_dir, f"{date}.json")

    # ---------------------------------------------------------
    # Create or update a journal entry
    # 创建或更新日记条目
    # ---------------------------------------------------------
    async def create_entry(
        self,
        date: str,
        event_summary: str,
        mood_comment: str = "",
        emotion_tags: str = "",
    ) -> dict:
        """
        Create or update a journal entry.
        创建或更新日记条目。

        - date: YYYY-MM-DD format
        - event_summary: Generated by AI housekeeper (factual event summary)
        - mood_comment: Added by main AI (emotional commentary, mood judgment)
        - emotion_tags: Comma-separated emotion tags (e.g. "焦虑,疲惫,但有些期待")
        - If entry exists for this date, update it (merge: keep existing fields if new ones empty)

        Returns the saved entry dict.
        """
        # --- Normalize inputs (graceful degradation, never raise on bad input) ---
        # --- 输入归一化（优雅降级，非法输入不抛异常）---
        date = self._normalize_date(date)
        emotion_tags = self._normalize_emotion_tags(emotion_tags)

        # --- Merge emotion tags via emotion_manager (synonym normalization) ---
        # --- 通过 emotion_manager 对情绪标签做同义词归并 ---
        if emotion_tags and self.emotion_mgr is not None:
            emotion_tags = await self._merge_emotion_tags(emotion_tags)

        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        file_path = self._entry_path(date)

        # --- Idempotent read-modify-write under lock / 加锁的读-改-写（幂等） ---
        with self._lock:
            # --- Load existing entry if present (merge mode) ---
            # --- 加载已有条目（合并模式）---
            existing = None
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception as e:
                    logger.warning(
                        f"Failed to load existing journal for merge, overwriting / "
                        f"加载已有日记失败，将覆盖: {date}: {e}"
                    )
                    existing = None

            if existing:
                # --- Merge: keep existing fields when new ones are empty ---
                # --- 合并：新值为空时保留旧值 ---
                entry = dict(existing)
                entry["date"] = date

                # event_summary comes from the AI housekeeper
                # event_summary 来自 AI 管家
                if event_summary and event_summary.strip():
                    entry["event_summary"] = event_summary.strip()
                    entry["housekeeper_generated_at"] = now_iso

                # mood_comment & emotion_tags come from the main AI
                # mood_comment 与 emotion_tags 来自主 AI
                if mood_comment and mood_comment.strip():
                    entry["mood_comment"] = mood_comment.strip()
                    entry["ai_completed_at"] = now_iso

                if emotion_tags:
                    entry["emotion_tags"] = emotion_tags
                    entry["ai_completed_at"] = now_iso

                # created_at preserved from existing entry
                # created_at 保留已有条目的值
            else:
                # --- New entry ---
                # --- 新建条目 ---
                has_summary = bool(event_summary and event_summary.strip())
                has_ai_input = bool(
                    (mood_comment and mood_comment.strip()) or bool(emotion_tags)
                )
                entry = {
                    "date": date,
                    "event_summary": (event_summary or "").strip(),
                    "mood_comment": (mood_comment or "").strip(),
                    "emotion_tags": emotion_tags,
                    "housekeeper_generated_at": now_iso if has_summary else "",
                    "ai_completed_at": now_iso if has_ai_input else "",
                    "created_at": now_iso,
                }

            # --- Write entry to disk ---
            # --- 写入磁盘 ---
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(entry, f, ensure_ascii=False, indent=2)
            except OSError as e:
                logger.error(
                    f"Failed to write journal entry / 写入日记失败: {date}: {e}"
                )
                raise

            logger.info(
                f"Journal entry saved / 日记已保存: date={date}, "
                f"mode={'merge' if existing else 'create'}"
            )
            return entry

    # ---------------------------------------------------------
    # Get entry by date
    # 按日期获取条目
    # ---------------------------------------------------------
    def get_entry(self, date: str) -> Optional[dict]:
        """
        Get journal entry by date. Returns None if not found.
        按日期获取日记条目。不存在则返回 None。
        """
        if not self._validate_date(date):
            return None
        file_path = self._entry_path(date)
        if not os.path.exists(file_path):
            return None
        with self._lock:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(
                    f"Failed to load journal entry / 加载日记失败: {date}: {e}"
                )
                return None

    # ---------------------------------------------------------
    # Search entries by keyword
    # 按关键字搜索条目
    # ---------------------------------------------------------
    def search_entries(self, keyword: str, limit: int = 10) -> list:
        """
        Search journal entries by keyword in event_summary, mood_comment, or emotion_tags.
        Returns list of {date, event_summary, mood_comment, emotion_tags, created_at}.

        在 event_summary、mood_comment、emotion_tags 中搜索关键字。
        返回 {date, event_summary, mood_comment, emotion_tags, created_at} 列表。
        """
        if not keyword or not keyword.strip():
            return []

        kw = keyword.lower().strip()
        results = []

        if not os.path.exists(self.journals_dir):
            return results

        for filename in os.listdir(self.journals_dir):
            if not filename.endswith(".json"):
                continue
            file_path = os.path.join(self.journals_dir, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    entry = json.load(f)
            except Exception as e:
                logger.warning(
                    f"Failed to load journal file during search / 搜索时加载日记失败: {filename}: {e}"
                )
                continue

            # --- Keyword matching across the three text fields ---
            # --- 在三个文本字段中匹配关键字 ---
            haystack = " ".join(
                [
                    str(entry.get("event_summary", "")),
                    str(entry.get("mood_comment", "")),
                    str(entry.get("emotion_tags", "")),
                ]
            ).lower()

            if kw in haystack:
                results.append(
                    {
                        "date": entry.get("date", ""),
                        "event_summary": entry.get("event_summary", ""),
                        "mood_comment": entry.get("mood_comment", ""),
                        "emotion_tags": entry.get("emotion_tags", ""),
                        "created_at": entry.get("created_at", ""),
                    }
                )

        # --- Sort by date descending, then apply limit ---
        # --- 按日期倒序，再截取 limit 条 ---
        results.sort(key=lambda r: r.get("date", ""), reverse=True)
        return results[:limit]

    # ---------------------------------------------------------
    # List recent entries
    # 列出最近的条目
    # ---------------------------------------------------------
    def list_entries(self, limit: int = 30) -> list:
        """
        List recent journal entries, sorted by date descending.
        列出最近的日记条目，按日期倒序排列。
        """
        results = []

        if not os.path.exists(self.journals_dir):
            return results

        for filename in os.listdir(self.journals_dir):
            if not filename.endswith(".json"):
                continue
            file_path = os.path.join(self.journals_dir, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                results.append(entry)
            except Exception as e:
                logger.warning(
                    f"Failed to load journal file during list / 列表时加载日记失败: {filename}: {e}"
                )
                continue

        # --- Sort by date descending ---
        # --- 按日期倒序 ---
        results.sort(key=lambda e: e.get("date", ""), reverse=True)
        return results[:limit]

    # ---------------------------------------------------------
    # Delete entry by date
    # 按日期删除条目
    # ---------------------------------------------------------
    def delete_entry(self, date: str) -> bool:
        """
        Delete a journal entry by date.
        按日期删除日记条目。
        """
        if not self._validate_date(date):
            return False
        file_path = self._entry_path(date)
        if not os.path.exists(file_path):
            return False
        with self._lock:
            try:
                os.remove(file_path)
            except OSError as e:
                logger.error(
                    f"Failed to delete journal entry / 删除日记失败: {date}: {e}"
                )
                return False

        logger.info(f"Journal entry deleted / 日记已删除: {date}")
        return True
