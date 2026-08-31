# ============================================================
# Module: Memory Thread Manager (thread_manager.py)
# 模块：记忆楼层管理器
#
# 核心目标：一条长期记忆的"事实主体"保持稳定，不因 AI 后来的
# 理解变化而被反复覆盖。AI 对同一件事在不同时间产生的新理解、
# 补充、纠正、情绪或观点，以"楼层（reply）"形式追加在原记忆下面。
#
# - 原始 memory 是主楼（main memory），保持稳定。
# - 后续 reply 是楼层，默认只追加，不覆盖旧楼层。
# - 真正删除/修改历史内容走 proposal → approval 流程。
#
# 与 event chain 的区别：
#   event chain 表示多个事件之间的时间/因果关系（A → B → C）；
#   memory thread 表示对"同一条记忆"在不同时间产生的理解变化。
# ============================================================

import os
import json
import uuid
import logging
from datetime import datetime, timezone

logger = logging.getLogger("ombre_brain.thread_manager")

# --- Reply type vocabulary / 楼层类型词表 ---
REPLY_TYPE_VOCABULARY = (
    "reflection",    # 后来的看法
    "correction",    # 对旧记录/旧理解的纠正
    "supplement",    # 补充信息
    "disagreement",  # 当前 AI 不同意过去 AI 的解释
    "feeling",       # 对这段记忆产生的新感受
)

# --- Author vocabulary / 作者词表 ---
AUTHOR_VOCABULARY = ("main_ai", "user", "system")

# --- Provenance vocabulary（与 bucket_manager 保持一致）---
PROVENANCE_VOCABULARY = (
    "user_explicit", "ai_inferred", "ai_observed", "system_event", "imported", "legacy",
)


class ThreadManager:
    """Memory thread manager: append-only reply storage for a memory.

    记忆楼层管理器：为单条长期记忆提供"只追加"的楼层（reply）存储。
    主记忆（main memory）保持稳定，AI 后来的理解变化以 reply 追加，不覆盖原记忆。
    """

    def __init__(self, config: dict, bucket_mgr=None):
        self.config = config or {}
        self.bucket_mgr = bucket_mgr
        data_dir = config.get(
            "buckets_dir",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets"),
        )
        parent_dir = os.path.dirname(data_dir) or "."
        # threads/ 与 buckets/ 同级存放，与记忆桶系统严格隔离（同 journals/ 模式）
        self.threads_dir = os.path.join(parent_dir, "threads")
        os.makedirs(self.threads_dir, exist_ok=True)

    # ---------------------------------------------------------
    # Path helpers / 路径辅助
    # ---------------------------------------------------------
    def _reply_path(self, reply_id: str) -> str:
        return os.path.join(self.threads_dir, f"{reply_id}.json")

    # ---------------------------------------------------------
    # Add reply / 追加楼层
    # ---------------------------------------------------------
    async def add_reply(
        self,
        parent_memory_id: str,
        content: str,
        reply_type: str = "reflection",
        author: str = "main_ai",
        provenance: str = "ai_inferred",
        world_id: str = None,
        scene: list = None,
        supersedes_reply_id: str = None,
        source_context: str = None,
    ) -> str:
        """Append a reply to a memory thread. 返回 reply_id。

        默认只追加，不覆盖旧楼层。world_id/scene 默认继承 parent memory，
        允许显式覆盖（但检索时仍遵守 world + scene 隔离规则）。
        """
        # --- 校验词表 / 非法值回退安全默认 ---
        if reply_type not in REPLY_TYPE_VOCABULARY:
            reply_type = "reflection"
        if author not in AUTHOR_VOCABULARY:
            author = "system"
        if provenance not in PROVENANCE_VOCABULARY:
            provenance = "legacy"
        if supersedes_reply_id and not os.path.exists(self._reply_path(supersedes_reply_id)):
            supersedes_reply_id = None

        # --- 继承 parent memory 的 world_id / scene（未显式指定时）---
        if self.bucket_mgr is not None:
            try:
                parent = await self.bucket_mgr.get(parent_memory_id)
                if parent:
                    meta = parent.get("metadata", {})
                    if world_id is None:
                        world_id = meta.get("world_id") or "main"
                    if scene is None:
                        scene = meta.get("scene") or ["chat"]
            except Exception as e:
                logger.warning(f"Failed to inherit parent world/scene for {parent_memory_id}: {e}")
        world_id = (world_id or "main").strip() or "main"
        scene = [s for s in (scene or ["chat"]) if isinstance(s, str) and s.strip()] or ["chat"]

        reply_id = str(uuid.uuid4())[:8]
        reply = {
            "reply_id": reply_id,
            "parent_memory_id": parent_memory_id,
            "author": author,
            # 微秒精度，保证同一秒内创建的楼层也能稳定排序
            "created_at": datetime.now(timezone.utc).isoformat(),
            "content": content,
            "reply_type": reply_type,
            "provenance": provenance,
            "world_id": world_id,
            "scene": scene,
            "source_context": source_context or "",
            "supersedes_reply_id": supersedes_reply_id or "",
        }
        with open(self._reply_path(reply_id), "w", encoding="utf-8") as f:
            json.dump(reply, f, ensure_ascii=False, indent=2)
        logger.info(
            f"Added reply {reply_id} to memory {parent_memory_id} "
            f"(type={reply_type}, author={author}, world={world_id})"
        )
        return reply_id

    # ---------------------------------------------------------
    # Read replies / 读取楼层
    # ---------------------------------------------------------
    async def get_reply(self, reply_id: str) -> dict | None:
        """Get a single reply by id. 按 id 获取单条楼层。"""
        path = self._reply_path(reply_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load reply {reply_id}: {e}")
            return None

    async def list_replies(self, parent_memory_id: str) -> list:
        """List all replies of a memory, sorted by created_at ascending.

        列出某条记忆的全部楼层，按 created_at 升序（主楼 → 最新楼层）。
        """
        replies = []
        for filename in os.listdir(self.threads_dir):
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.threads_dir, filename), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("parent_memory_id") == parent_memory_id:
                    replies.append(data)
            except (json.JSONDecodeError, OSError):
                continue
        replies.sort(key=lambda r: r.get("created_at", ""))
        return replies

    async def get_thread(self, parent_memory_id: str, main_memory: dict = None) -> dict:
        """Return the full thread history: main_memory + replies (sorted).

        返回完整楼层历史：main_memory + replies（按 created_at 排序）。
        仅在 AI/用户主动请求查看时调用，普通 breath 不展开全部楼层。
        """
        replies = await self.list_replies(parent_memory_id)
        return {
            "main_memory": main_memory,
            "replies": replies,
        }

    # ---------------------------------------------------------
    # Mutation (proposal-gated) / 修改（仅由提案审批后调用）
    # ---------------------------------------------------------
    async def delete_reply(self, reply_id: str) -> bool:
        """Delete a reply. 删除楼层（仅由 proposal 审批后调用）。"""
        path = self._reply_path(reply_id)
        if not os.path.exists(path):
            return False
        os.remove(path)
        logger.info(f"Deleted reply {reply_id}")
        return True

    async def update_reply(self, reply_id: str, **kwargs) -> bool:
        """Update a reply's mutable fields. 修改楼层（仅由 proposal 审批后调用）。

        不可修改 reply_id / parent_memory_id / created_at（历史不可篡改）。
        """
        path = self._reply_path(reply_id)
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load reply for update {reply_id}: {e}")
            return False
        for k, v in kwargs.items():
            if k in ("reply_id", "parent_memory_id", "created_at"):
                continue
            data[k] = v
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Updated reply {reply_id}: {list(kwargs.keys())}")
        return True