# ============================================================
# Module: Tag Normalizer (tag_normalizer.py)
# 模块：标签归一化引擎
#
# Batch job that periodically normalizes non-standard tags
# by mapping synonyms back to the canonical generalized tag tree.
# 后台批量任务：定期将非标准标签归一化到预设的泛化标签树。
#
# Trigger conditions (whichever comes first):
# 触发条件（先到先触发）：
#   1. Every `interval_hours` (default: 168h = 1 week)
#   2. Every `batch_threshold` new records (default: 50)
#
# Depended on by: server.py
# 被谁依赖：server.py
# ============================================================

import os
import json
import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta

from openai import AsyncOpenAI

logger = logging.getLogger("ombre_brain.tag_normalizer")


# --- Canonical generalized tag tree / 预设泛化标签树 ---
CANONICAL_TAGS = [
    "工作", "学习", "生活", "健康", "人际关系",
    "兴趣爱好", "财务", "内心世界", "数字技术",
    "事务管理", "休闲娱乐", "家庭", "情感", "成长", "创造",
]

# --- Normalization prompt / 归一化提示词 ---
NORMALIZE_PROMPT = """你是一个标签归一化专家。请将输入的非标准标签映射到预设的泛化标签树中。

预设泛化标签树（只能映射到这些标签）：
["工作", "学习", "生活", "健康", "人际关系", "兴趣爱好", "财务", "内心世界", "数字技术", "事务管理", "休闲娱乐", "家庭", "情感", "成长", "创造"]

规则：
1. 每个非标准标签必须映射到上述泛化标签中的一个
2. 如果标签语义不明确，映射到最接近的泛化标签
3. 同义词应映射到同一个泛化标签（如"编程"、"代码"、"开发" → "数字技术"）
4. 输出 JSON 对象，key 为原始标签，value 为映射后的泛化标签

输入的非标准标签列表：
{tags}

输出格式（纯 JSON，无其他内容）：
{{"原始标签1": "泛化标签1", "原始标签2": "泛化标签2", ...}}"""


class TagNormalizer:
    """
    Tag normalization batch job.
    Periodically scans all buckets, finds non-standard tags,
    and maps them back to the canonical tag tree via LLM.
    标签归一化批量任务。
    定期扫描所有桶，找出非标准标签，通过 LLM 映射回泛化标签树。
    """

    def __init__(self, config: dict, bucket_mgr, dehydrator=None):
        tag_cfg = config.get("tag_normalization", {})

        # --- Trigger config / 触发配置 ---
        self.interval_hours = tag_cfg.get("interval_hours", 168)  # 1 week
        self.batch_threshold = tag_cfg.get("batch_threshold", 50)
        self.min_tag_frequency = tag_cfg.get("min_tag_frequency", 2)
        self.enabled = tag_cfg.get("enabled", True)

        self.bucket_mgr = bucket_mgr
        self.dehydrator = dehydrator

        # --- LLM client (reuse dehydrator's config) ---
        dehy_cfg = config.get("dehydration", {})
        self.api_key = dehy_cfg.get("api_key", "") or os.environ.get("OMBRE_API_KEY", "")
        self.model = dehy_cfg.get("model", "deepseek-chat")
        self.base_url = dehy_cfg.get("base_url", "https://api.deepseek.com/v1")

        if self.api_key:
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=60.0,
            )
        else:
            self.client = None

        # --- State file: track last run time and record count ---
        self.state_file = os.path.join(
            config["buckets_dir"], "tag_normalizer_state.json"
        )
        self._records_since_last_run = 0
        self._last_run_at = None
        self._load_state()

        # --- Background task control ---
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def needs_run(self) -> bool:
        """Check if normalization should run (time or count threshold)."""
        if not self.enabled:
            return False
        if self._records_since_last_run >= self.batch_threshold:
            return True
        if self._last_run_at:
            elapsed = datetime.now() - self._last_run_at
            if elapsed >= timedelta(hours=self.interval_hours):
                return True
        return False

    def notify_new_record(self, count: int = 1):
        """Called when new records are added (by server.py hold/grow)."""
        self._records_since_last_run += count
        self._save_state()

    # ---------------------------------------------------------
    # State persistence / 状态持久化
    # ---------------------------------------------------------
    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self._records_since_last_run = state.get("records_since_last_run", 0)
                last_str = state.get("last_run_at")
                if last_str:
                    self._last_run_at = datetime.fromisoformat(last_str)
            except Exception as e:
                logger.warning(f"Failed to load tag normalizer state: {e}")

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            state = {
                "records_since_last_run": self._records_since_last_run,
                "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            }
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save tag normalizer state: {e}")

    # ---------------------------------------------------------
    # Core: collect and normalize tags
    # ---------------------------------------------------------
    async def run_normalization(self) -> dict:
        """
        Execute one normalization cycle:
        1. Collect all tags from all buckets
        2. Filter out canonical tags
        3. Send non-standard tags to LLM for mapping
        4. Apply mappings to buckets

        Returns: stats dict
        """
        if not self.enabled:
            return {"skipped": True, "reason": "disabled"}

        if not self.client:
            logger.warning("Tag normalization skipped: no API key configured")
            return {"skipped": True, "reason": "no_api_key"}

        logger.info("Starting tag normalization cycle / 开始标签归一化周期")

        try:
            buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for tag normalization: {e}")
            return {"error": str(e)}

        # --- Step 1: Count tag frequencies ---
        tag_counter: Counter = Counter()
        tag_to_buckets: dict[str, list[str]] = {}

        for bucket in buckets:
            meta = bucket.get("metadata", {})
            tags = meta.get("tags", [])
            if not isinstance(tags, list):
                continue
            for tag in tags:
                if not tag or not isinstance(tag, str):
                    continue
                tag = tag.strip()
                if not tag:
                    continue
                tag_counter[tag] += 1
                tag_to_buckets.setdefault(tag, []).append(bucket["id"])

        # --- Step 2: Filter non-standard tags (not in canonical tree) ---
        non_standard_tags = [
            tag for tag, count in tag_counter.items()
            if tag not in CANONICAL_TAGS and count >= self.min_tag_frequency
        ]

        if not non_standard_tags:
            logger.info("No non-standard tags found / 未发现非标准标签")
            self._records_since_last_run = 0
            self._last_run_at = datetime.now()
            self._save_state()
            return {
                "total_tags": len(tag_counter),
                "non_standard": 0,
                "normalized": 0,
                "buckets_updated": 0,
            }

        logger.info(
            f"Found {len(non_standard_tags)} non-standard tags / "
            f"发现 {len(non_standard_tags)} 个非标准标签"
        )

        # --- Step 3: Call LLM to generate mapping ---
        try:
            mapping = await self._get_tag_mapping(non_standard_tags)
        except Exception as e:
            logger.error(f"LLM tag mapping failed: {e}")
            return {"error": str(e)}

        if not mapping:
            logger.info("No mappings returned by LLM / LLM 未返回映射")
            return {
                "total_tags": len(tag_counter),
                "non_standard": len(non_standard_tags),
                "normalized": 0,
                "buckets_updated": 0,
            }

        # --- Step 4: Apply mappings to buckets ---
        buckets_updated = 0
        tags_normalized = 0

        for old_tag, new_tag in mapping.items():
            if new_tag not in CANONICAL_TAGS:
                logger.warning(f"LLM returned invalid canonical tag: {new_tag}, skipping")
                continue
            if old_tag == new_tag:
                continue

            affected_bucket_ids = tag_to_buckets.get(old_tag, [])
            tags_normalized += 1

            for bucket_id in affected_bucket_ids:
                try:
                    bucket = await self.bucket_mgr.get(bucket_id)
                    if not bucket:
                        continue
                    meta = bucket.get("metadata", {})
                    current_tags = meta.get("tags", [])
                    if old_tag not in current_tags:
                        continue

                    # Replace old tag with new tag, avoid duplicates
                    new_tags = [t for t in current_tags if t != old_tag]
                    if new_tag not in new_tags:
                        new_tags.append(new_tag)

                    await self.bucket_mgr.update(bucket_id, tags=new_tags)
                    buckets_updated += 1
                except Exception as e:
                    logger.warning(
                        f"Failed to update tags for bucket {bucket_id}: {e}"
                    )

        # --- Reset counters ---
        self._records_since_last_run = 0
        self._last_run_at = datetime.now()
        self._save_state()

        result = {
            "total_tags": len(tag_counter),
            "non_standard": len(non_standard_tags),
            "normalized": tags_normalized,
            "buckets_updated": buckets_updated,
            "mapping": mapping,
        }
        logger.info(f"Tag normalization complete / 标签归一化完成: {result}")
        return result

    # ---------------------------------------------------------
    # LLM call: get tag mapping
    # ---------------------------------------------------------
    async def _get_tag_mapping(self, non_standard_tags: list[str]) -> dict:
        """
        Call LLM to map non-standard tags to canonical tags.
        调用 LLM 将非标准标签映射到泛化标签。
        """
        tags_str = json.dumps(non_standard_tags, ensure_ascii=False)
        prompt = NORMALIZE_PROMPT.replace("{tags}", tags_str)

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": tags_str},
            ],
            max_tokens=512,
            temperature=0.1,
        )

        if not response.choices:
            return {}

        raw = response.choices[0].message.content or ""
        raw = raw.strip()

        # --- Extract JSON from response ---
        if raw.startswith("```"):
            lines = raw.split("\n")
            json_lines = []
            in_json = False
            for line in lines:
                if line.startswith("```") and not in_json:
                    in_json = True
                    continue
                elif line.startswith("```") and in_json:
                    break
                elif in_json:
                    json_lines.append(line)
            raw = "\n".join(json_lines)

        try:
            mapping = json.loads(raw)
            if not isinstance(mapping, dict):
                return {}
            return mapping
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse tag mapping JSON: {e}, raw: {raw[:200]}")
            return {}

    # ---------------------------------------------------------
    # Background task management / 后台任务管理
    # ---------------------------------------------------------
    async def ensure_started(self) -> None:
        """Ensure the normalizer is started (lazy init)."""
        if not self._running and self.enabled:
            await self.start()

    async def start(self) -> None:
        """Start the background normalization loop."""
        if self._running or not self.enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info(
            f"Tag normalizer started, interval: {self.interval_hours}h, "
            f"batch threshold: {self.batch_threshold} records"
        )

    async def stop(self) -> None:
        """Stop the background normalization loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Tag normalizer stopped / 标签归一化引擎已停止")

    async def _background_loop(self) -> None:
        """Background loop: check trigger → run → sleep → repeat."""
        # Check every hour whether trigger conditions are met
        check_interval = 3600  # 1 hour
        while self._running:
            try:
                if self.needs_run:
                    await self.run_normalization()
            except Exception as e:
                logger.error(f"Tag normalization cycle error: {e}")
            try:
                await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                break
