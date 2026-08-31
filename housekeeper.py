# ============================================================
# Module: Memory Housekeeper (housekeeper.py)
# 模块：记忆管家
#
# Batch pipeline service with:
# 1. Event Chain persistence
# 2. Echo Chamber (system-level staging area)
# 3. Daily/Weekly cron jobs
# 4. Event classification rules (Atomic vs Long-term)
#
# All operations are staging-only - no direct deletion or overwriting.
# Final approval rests with the main AI.
# ============================================================

import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta, timezone

try:
    from rapidfuzz import fuzz, process
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

# Journal manager — primary storage for daily journal entries
# 日记管理器 — 每日日记条目的主存储
from journal_manager import JournalManager

# Primary tag closed vocabulary (from dehydrator) / 主标签封闭词表
try:
    from dehydrator import PRIMARY_TAG_VOCAB
except ImportError:
    PRIMARY_TAG_VOCAB = ("生活", "健康", "学习", "工作", "关系", "情绪", "约定", "兴趣")

# Safe conversion helpers / 安全类型转换工具
try:
    from utils import safe_int, safe_float, as_list, safe_json_loads
except ImportError:
    def safe_int(value, default=0):
        try:
            return int(value) if value is not None else default
        except (ValueError, TypeError):
            return default

    def safe_float(value, default=0.0):
        try:
            return float(value) if value is not None else default
        except (ValueError, TypeError):
            return default

    def as_list(value):
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return list(value)
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return [value]

# --- Proposal TTL (stale proposal 防护) ---
# 提案默认有效期：超过该时长未审批的提案视为过期，禁止执行。
# 防止很久以前的旧提案在状态已变化后被批准执行（stale proposal）。
PROPOSAL_TTL_DAYS = 7


def _infer_memory_class(bucket_type: str) -> str | None:
    """Infer a suggested memory_class from the existing type (映射层，修正 5).

    从现有 type 推断建议的 memory_class（event/experience/person/boundary/plan/principle）。
    无法可靠推断时返回 None（不强行推断，宁缺毋滥）。
    仅用于生成 enrichment 提案的建议值，不直接修改记忆。
    """
    mapping = {
        "boundary": "boundary",
        "permanent": "principle",
        "pattern": "experience",
        "identity": "person",
        "dynamic": "event",
        "feel": "experience",
    }
    return mapping.get(bucket_type)

logger = logging.getLogger("ombre_brain.housekeeper")


class EventChain:
    """
    Event Chain data structure for persistent storage.
    事件链数据结构，用于持久化存储。
    """
    
    def __init__(self, chain_id: str, topic: str, status: str = "in_progress"):
        self.chain_id = chain_id
        self.topic = topic
        self.status = status
        self.timeline = []
        self.summary = ""
        self.created = datetime.now(timezone.utc).isoformat()
        self.updated = self.created
        self.source_bucket_ids = []
        # --- Entity graph fields / 图谱关联字段 ---
        # entities: core entities involved in this chain (persons/items/concepts)
        # entities: 该事件链涉及的核心实体词（人物/物品/概念等）
        self.entities: list[str] = []
        # related_chain_ids: chains sharing entity overlap with this one
        # related_chain_ids: 与当前事件链存在实体交集的其他事件链 ID
        self.related_chain_ids: list[str] = []

    def to_dict(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "topic": self.topic,
            "status": self.status,
            "timeline": self.timeline,
            "summary": self.summary,
            "created": self.created,
            "updated": self.updated,
            "source_bucket_ids": self.source_bucket_ids,
            "entities": self.entities,
            "related_chain_ids": self.related_chain_ids,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EventChain":
        # --- Tolerate corrupted/missing keys instead of raising KeyError ---
        # --- 容忍缺失/损坏字段，避免 KeyError 导致链条被静默丢弃 ---
        if not isinstance(data, dict):
            return cls("", "未命名事件链", "draft")
        chain = cls(data.get("chain_id", ""), data.get("topic", "未命名事件链"), data.get("status", "in_progress"))
        chain.timeline = data.get("timeline", []) or []
        if not isinstance(chain.timeline, list):
            chain.timeline = []
        chain.summary = data.get("summary", "") or ""
        chain.created = data.get("created") or datetime.now(timezone.utc).isoformat()
        chain.updated = data.get("updated") or chain.created
        chain.source_bucket_ids = data.get("source_bucket_ids", []) or []
        if not isinstance(chain.source_bucket_ids, list):
            chain.source_bucket_ids = []
        chain.entities = data.get("entities", []) or []
        if not isinstance(chain.entities, list):
            chain.entities = []
        chain.related_chain_ids = data.get("related_chain_ids", []) or []
        if not isinstance(chain.related_chain_ids, list):
            chain.related_chain_ids = []
        return chain


class CleanupProposal:
    """
    Proposal for cleaning up stale memories.
    废旧记忆清理提案。
    """
    
    def __init__(self, proposal_id: str, bucket_id: str, reason: str):
        self.proposal_id = proposal_id
        self.bucket_id = bucket_id
        self.reason = reason
        self.status = "pending"
        self.created = datetime.now(timezone.utc).isoformat()
        self.bucket_info = {}
    
    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "bucket_id": self.bucket_id,
            "reason": self.reason,
            "status": self.status,
            "created": self.created,
            "bucket_info": self.bucket_info,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "CleanupProposal":
        proposal = cls(data["proposal_id"], data["bucket_id"], data.get("reason", ""))
        proposal.status = data.get("status", "pending")
        proposal.created = data.get("created", datetime.now(timezone.utc).isoformat())
        proposal.bucket_info = data.get("bucket_info", {})
        return proposal


class MergeProposal:
    """
    Proposal for merging similar memories.
    相似记忆合并提案。
    """
    
    def __init__(self, proposal_id: str, bucket_ids: list, summary: str):
        self.proposal_id = proposal_id
        self.bucket_ids = bucket_ids
        self.summary = summary
        self.status = "pending"
        self.created = datetime.now(timezone.utc).isoformat()
    
    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "bucket_ids": self.bucket_ids,
            "summary": self.summary,
            "status": self.status,
            "created": self.created,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "MergeProposal":
        return cls(data["proposal_id"], data.get("bucket_ids", []), data.get("summary", ""))


class EchoChamber:
    """
    System-level staging area for:
    - Daily/Weekly digests
    - Pending cleanup/merge proposals
    - Event chain drafts
    """
    
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.digests_dir = os.path.join(base_dir, "digests")
        self.pending_actions_dir = os.path.join(base_dir, "pending_actions")
        
        os.makedirs(self.digests_dir, exist_ok=True)
        os.makedirs(self.pending_actions_dir, exist_ok=True)
    
    async def write_digest(self, digest_type: str, content: str, metadata: dict = None):
        """
        Write a daily/weekly digest to echo chamber.
        digest_type: daily/weekly
        """
        digest_id = f"{digest_type}_{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        file_path = os.path.join(self.digests_dir, f"{digest_id}.json")
        
        digest = {
            "digest_id": digest_id,
            "digest_type": digest_type,
            "content": content,
            "metadata": metadata or {},
            "created": datetime.now(timezone.utc).isoformat(),
            "reviewed": False,
        }
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(digest, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Written {digest_type} digest: {digest_id}")
    
    async def get_pending_digests(self, digest_type: str = "all") -> list:
        """Get unreviewed digests."""
        digests = []
        for filename in os.listdir(self.digests_dir):
            if filename.endswith(".json"):
                file_path = os.path.join(self.digests_dir, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if data.get("reviewed") is False:
                            if digest_type == "all" or data.get("digest_type") == digest_type:
                                digests.append(data)
                except Exception as e:
                    logger.warning(f"Failed to load digest: {file_path}: {e}")
        
        digests.sort(key=lambda d: d.get("created", ""), reverse=True)
        return digests
    
    async def mark_digest_reviewed(self, digest_id: str):
        """Mark a digest as reviewed."""
        file_path = os.path.join(self.digests_dir, f"{digest_id}.json")
        if not os.path.exists(file_path):
            return False
        
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        data["reviewed"] = True
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        return True
    
    async def add_pending_action(self, action_type: str, data: dict, proposed_by: str = "ai_manage") -> str:
        """
        Add a pending action to echo chamber.
        action_type: cleanup/merge/chain_update/change/organize
        proposed_by: 提案来源（ai_manage / main_ai / user），默认 ai_manage。
        返回 action_id，供调用方引用提案。
        提案带默认有效期（PROPOSAL_TTL_DAYS 天），过期后禁止执行（stale proposal 防护）。
        """
        action_id = str(uuid.uuid4())[:8]
        file_path = os.path.join(self.pending_actions_dir, f"{action_id}.json")
        
        action = {
            "action_id": action_id,
            "action_type": action_type,
            "status": "pending",
            "proposed_by": proposed_by,
            "data": data,
            "created": datetime.now(timezone.utc).isoformat(),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(days=PROPOSAL_TTL_DAYS)
            ).isoformat(),
        }
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(action, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Added pending action: {action_type} - {action_id} (proposed_by={proposed_by})")
        return action_id

    def _is_proposal_expired(self, data: dict) -> bool:
        """True if a pending proposal has passed its expiry (stale proposal 防护).

        判断提案是否过期：expires_at 存在且早于当前时间 → 过期。
        旧提案（无 expires_at 字段）按未过期处理，兼容历史数据。
        """
        expiry = data.get("expires_at")
        if not expiry:
            return False
        try:
            expiry_dt = datetime.fromisoformat(str(expiry))
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.astimezone()
            return datetime.now(expiry_dt.tzinfo) > expiry_dt
        except (ValueError, TypeError):
            return False
    
    async def get_pending_actions(self, action_type: str = "all") -> list:
        """Get pending actions."""
        actions = []
        for filename in os.listdir(self.pending_actions_dir):
            if filename.endswith(".json"):
                file_path = os.path.join(self.pending_actions_dir, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if data.get("status") == "pending":
                            if action_type == "all" or data.get("action_type") == action_type:
                                actions.append(data)
                except Exception as e:
                    logger.warning(f"Failed to load action: {file_path}: {e}")
        
        actions.sort(key=lambda a: a.get("created", ""), reverse=True)
        return actions
    
    async def update_action_status(self, action_id: str, status: str, approved_by: str = "user"):
        """Update action status (approve/reject/executed).

        更新提案状态。approved_by 记录审批者（user / main_ai），
        为未来两级权限审批留接口（修正 6）。
        """
        file_path = os.path.join(self.pending_actions_dir, f"{action_id}.json")
        if not os.path.exists(file_path):
            return False
        
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        data["status"] = status
        if status in ("approved", "rejected", "executed"):
            data["approved_by"] = approved_by
            data["approved_at"] = datetime.now(timezone.utc).isoformat()
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Updated action {action_id} → {status} (approved_by={approved_by})")
        return True
    
    async def get_review_summary(self) -> dict:
        """Get summary of all pending items for main AI review."""
        digests = await self.get_pending_digests()
        actions = await self.get_pending_actions()
        
        return {
            "pending_digests": len(digests),
            "pending_actions": len(actions),
            "digests": digests,
            "actions": actions,
        }


class Housekeeper:
    """
    Memory housekeeper service with daily/weekly pipeline.
    Auto-starts on initialization and runs scheduled tasks automatically.
    Uses AI (via dehydrator) to generate high-quality summaries.
    """
    
    def __init__(self, config: dict, bucket_mgr, dehydrator=None, identity_mgr=None, embedding_engine=None):
        self.config = config or {}
        self.bucket_mgr = bucket_mgr
        self.dehydrator = dehydrator
        self.identity_mgr = identity_mgr
        self.embedding_engine = embedding_engine
        
        data_dir = config.get("buckets_dir", os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets"))
        self.event_chains_dir = os.path.join(data_dir, "event_chains")
        self.echo_chamber_dir = os.path.join(data_dir, "echo_chamber")
        self._state_file = os.path.join(self.echo_chamber_dir, ".housekeeper_state.json")
        
        os.makedirs(self.event_chains_dir, exist_ok=True)
        os.makedirs(self.echo_chamber_dir, exist_ok=True)
        
        self.echo_chamber = EchoChamber(self.echo_chamber_dir)

        # Journal manager — primary storage for daily journal entries
        # 日记管理器 — 每日日记条目的主存储（主存储；echo chamber 仅用于审阅）
        # Journals live as a sibling of buckets/ for strict isolation from bucket space.
        # 日记与 buckets/ 同级存放，与记忆桶系统严格隔离。
        parent_dir = os.path.dirname(data_dir) or "."
        self.journals_dir = os.path.join(parent_dir, "journals")
        os.makedirs(self.journals_dir, exist_ok=True)
        self.journal_mgr = JournalManager(base_dir=parent_dir)

        # --- Cycle tracker — physiology context for daily review ---
        # --- 例假周期追踪 —— 为日终整理提供健康上下文 ---
        try:
            from cycle_tracker import CycleTracker
            self.cycle_tracker = CycleTracker(data_dir)
        except Exception as e:
            logger.warning(f"CycleTracker init failed / 周期追踪初始化失败: {e}")
            self.cycle_tracker = None

        # --- Housekeeper behaviour flags / 管家行为开关 ---
        # auto_schedule: run daily/weekly jobs automatically in the background.
        #   Default OFF — the daily review is triggered by the main AI via
        #   daily_review() / run_housekeeper() (product positioning: the
        #   housekeeper never interrupts the user's conversation proactively).
        # auto_schedule：是否后台自动调度日终/周任务。默认关闭 ——
        #   日终整理由主 AI 主动调用（管家不主动打断日常对话）。
        hk_conf = config.get("housekeeper", {}) or {}
        self.auto_schedule = bool(hk_conf.get("auto_schedule", False))
        # auto_journal_draft: whether the legacy daily summary may auto-write a
        #   journal draft. Default OFF — the housekeeper has no access to the
        #   un-stored conversation, so today's summary must be written by the
        #   main AI; the housekeeper only provides structure & storage help.
        # auto_journal_draft：旧管线是否自动写日记草稿。默认关闭 ——
        #   管家看不到未落库的对话全文，今日总结由主 AI 依据对话写出。
        self.auto_journal_draft = bool(hk_conf.get("auto_journal_draft", False))
        # cleanup_interval_days: full-scan interval. Every N days the housekeeper
        #   runs a global sweep (deep decay → faded_memory; ring extraction from
        #   recent half-month frequency). Default 15.
        # cleanup_interval_days：周期性全局大扫除间隔天数，默认 15 天一次。
        self.cleanup_interval_days = max(1, int(safe_int(hk_conf.get("cleanup_interval_days"), 15) or 15))
        # cleanup_max_conflicts: hard cap for conflict proposals per single scan.
        # cleanup_max_conflicts：单次扫描产出的冲突提案总量强制上限（防假警报刷屏）。
        self.cleanup_max_conflicts = max(1, int(safe_int(hk_conf.get("cleanup_max_conflicts"), 5) or 5))
        # incremental_match_top_k: how many top historical buckets to pull per
        #   new memory during nightly incremental dream (no full scan).
        # incremental_match_top_k：增量做梦时每个新记忆拉取的旧桶比对上限（禁止全量扫描）。
        self.incremental_match_top_k = max(1, min(5, int(safe_int(hk_conf.get("incremental_match_top_k"), 5) or 5)))
        # conflict_confidence_threshold: only conflicts above this confidence
        #   (0.0~1.0) may generate proposals. Default 0.85.
        # conflict_confidence_threshold：仅高于该置信度的冲突才允许生成提案，默认 0.85。
        self.conflict_confidence_threshold = max(0.0, min(1.0, float(
            safe_float(hk_conf.get("conflict_confidence_threshold"), 0.85) or 0.85
        )))
        # optimize_llm_limit: max LLM re-analysis calls per optimization scan.
        #   Keeps the daily incremental + monthly full scans affordable.
        # optimize_llm_limit：每次优化扫描最多对多少条记忆做 LLM 重分析（控制成本）。
        self.optimize_llm_limit = max(0, int(safe_int(hk_conf.get("optimize_llm_limit"), 5) or 5))
        # optimize_max_proposals: hard cap for optimization proposals per scan.
        # optimize_max_proposals：单次扫描产出的优化提案上限（防刷屏）。
        self.optimize_max_proposals = max(1, int(safe_int(hk_conf.get("optimize_max_proposals"), 20) or 20))

        self._task: asyncio.Task | None = None
        self._running = False
        self._start_lock = asyncio.Lock()  # Prevent concurrent start races / 防并发启动竞态
        self._last_daily_run = None
        self._last_weekly_run = None
        self._last_full_cleanup = None  # 周期大扫除上次执行时间
        self._last_scan_run = None  # 每日增量优化扫描上次执行时间
        self._last_full_scan_run = None  # 月度全量优化扫描上次执行时间
        
        self._load_state()
        
        # NOTE: Do NOT call asyncio.create_task(self.start()) here.
        # The event loop is NOT running during __init__ (called at module level).
        # Housekeeper is started via ensure_started() (lazy) or an explicit
        # startup hook in server.py.
        # 不要在 __init__ 中调用 asyncio.create_task，此时事件循环尚未启动。
        # 管家通过 ensure_started()（懒加载）或 server.py 的启动钩子来启动。
    
    def _load_state(self):
        """Load last run times from state file."""
        if os.path.exists(self._state_file):
            try:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                
                if state.get("last_daily_run"):
                    try:
                        self._last_daily_run = datetime.fromisoformat(state["last_daily_run"])
                        if self._last_daily_run.tzinfo is None:
                            self._last_daily_run = self._last_daily_run.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        self._last_daily_run = None
                
                if state.get("last_weekly_run"):
                    try:
                        self._last_weekly_run = datetime.fromisoformat(state["last_weekly_run"])
                        if self._last_weekly_run.tzinfo is None:
                            self._last_weekly_run = self._last_weekly_run.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        self._last_weekly_run = None

                if state.get("last_full_cleanup"):
                    try:
                        self._last_full_cleanup = datetime.fromisoformat(state["last_full_cleanup"])
                        if self._last_full_cleanup.tzinfo is None:
                            self._last_full_cleanup = self._last_full_cleanup.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        self._last_full_cleanup = None

                if state.get("last_scan_run"):
                    try:
                        self._last_scan_run = datetime.fromisoformat(state["last_scan_run"])
                        if self._last_scan_run.tzinfo is None:
                            self._last_scan_run = self._last_scan_run.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        self._last_scan_run = None

                if state.get("last_full_scan_run"):
                    try:
                        self._last_full_scan_run = datetime.fromisoformat(state["last_full_scan_run"])
                        if self._last_full_scan_run.tzinfo is None:
                            self._last_full_scan_run = self._last_full_scan_run.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        self._last_full_scan_run = None

                logger.info(
                    f"Loaded housekeeper state: daily={self._last_daily_run}, "
                    f"weekly={self._last_weekly_run}, full_cleanup={self._last_full_cleanup}"
                )
            except Exception as e:
                logger.error(f"Failed to load housekeeper state: {e}")
    
    def _save_state(self):
        """Save last run times to state file."""
        try:
            state = {
                "last_daily_run": self._last_daily_run.isoformat() if self._last_daily_run else None,
                "last_weekly_run": self._last_weekly_run.isoformat() if self._last_weekly_run else None,
                "last_full_cleanup": self._last_full_cleanup.isoformat() if self._last_full_cleanup else None,
                "last_scan_run": self._last_scan_run.isoformat() if self._last_scan_run else None,
                "last_full_scan_run": self._last_full_scan_run.isoformat() if self._last_full_scan_run else None,
                "saved_at": datetime.now(timezone.utc).isoformat()
            }
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            logger.debug("Housekeeper state saved")
        except Exception as e:
            logger.error(f"Failed to save housekeeper state: {e}")
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    async def ensure_started(self):
        """Ensure the housekeeper is started (lazy init on first call)."""
        if not self._running:
            async with self._start_lock:
                if not self._running:
                    await self.start()
    
    async def start(self):
        """Start the housekeeper background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info("Housekeeper started (daily/weekly pipeline)")
    
    async def stop(self):
        """Stop the housekeeper background task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Housekeeper stopped")
    
    async def _background_loop(self):
        """Background loop: check schedule every hour."""
        while self._running:
            try:
                await self._check_schedule()
            except Exception as e:
                logger.error(f"Housekeeper schedule error: {e}")
            
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                break
    
    async def _check_schedule(self):
        """Check if daily/weekly jobs should run.
        检查每日/每周任务及记忆整理扫描的调度。
        """
        now = datetime.now(timezone.utc)

        # ---------------------------------------------------------
        # Memory-organization scans — always auto-scheduled.
        # 记忆整理扫描 —— 始终自动调度（独立于 auto_schedule）：
        #   - Daily incremental: scan memories stored in the last 48h, propose
        #     tag / importance / type adjustments for the main AI to approve.
        #     每日增量：扫描最近 48h 入库的记忆，对标签/权重/类型提出调整提案。
        #   - Monthly full scan: sweep the whole library, propose optimizations.
        #     月度全量：完整扫描整个记忆库，提出优化提案。
        # Only optimization PROPOSALS are created here — nothing is changed
        # until the main AI approves via approve_action().
        # 这里只生成优化提案，任何改动都需主 AI 审批后才执行。
        # ---------------------------------------------------------
        if self._last_scan_run is None or (now - self._last_scan_run).total_seconds() >= 86400:
            try:
                scan = await self._scan_recent_for_optimization()
                logger.info(f"Daily optimization scan: {scan}")
            except Exception as e:
                logger.error(f"Daily optimization scan failed / 每日优化扫描失败: {e}")
            self._last_scan_run = now
            self._save_state()

        if self._last_full_scan_run is None or (now - self._last_full_scan_run).days >= 30:
            try:
                full = await self._scan_full_for_optimization()
                logger.info(f"Full optimization scan: {full}")
            except Exception as e:
                logger.error(f"Full optimization scan failed / 全量优化扫描失败: {e}")
            self._last_full_scan_run = now
            self._save_state()

        # --- Auto-scheduling is OFF by default: the main AI triggers the daily
        # --- review explicitly (product positioning). Users opting in can set
        # --- housekeeper.auto_schedule: true.
        # --- 默认关闭自动调度：日终整理由主 AI 主动调用；如需旧行为可开启配置。
        if not self.auto_schedule:
            return
        
        should_daily = False
        should_weekly = False
        
        if self._last_daily_run is None:
            should_daily = True
        else:
            hours_since_daily = (now - self._last_daily_run).total_seconds() / 3600
            if hours_since_daily >= 24:
                should_daily = True
        
        if self._last_weekly_run is None:
            should_weekly = now.weekday() == 6
        else:
            days_since_weekly = (now - self._last_weekly_run).days
            if days_since_weekly >= 7 and now.weekday() == 6:
                should_weekly = True
        
        if should_daily:
            logger.info("Starting daily housekeeper job...")
            await self.run_daily_job()
            self._last_daily_run = now
            self._save_state()
        
        if should_weekly:
            logger.info("Starting weekly housekeeper job...")
            await self.run_weekly_job()
            self._last_weekly_run = now
            self._save_state()
    
    # =========================================================
    # Memory-organization scans — proposals only, no writes.
    # 记忆整理扫描 —— 只生成优化提案，任何改动需主 AI 审批后执行。
    #   Daily incremental: recently stored memories (last 48h).
    #     每日增量：最近 48h 入库的记忆。
    #   Monthly full scan: whole library.
    #     月度全量：完整记忆库。
    # =========================================================

    async def _scan_recent_for_optimization(self, hours: int = 48) -> dict:
        """Daily incremental scan: propose tag / importance / type adjustments
        for memories stored within the last `hours`.
        每日增量扫描：对最近 hours 小时内入库的记忆提出标签/权重/类型调整建议。"""
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Optimization scan: list failed / 列表失败: {e}")
            return {"error": str(e)}

        recent = []
        for b in all_buckets:
            meta = b.get("metadata", {}) or {}
            if meta.get("pinned") or meta.get("protected"):
                continue
            created_str = meta.get("created", "")
            if not created_str:
                continue
            try:
                created = datetime.fromisoformat(str(created_str))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < since:
                    continue
            except (ValueError, TypeError):
                continue
            recent.append(b)

        if not recent:
            return {"window_hours": hours, "scanned": 0, "proposals": 0}
        return await self._run_optimization_scan(recent, llm_limit=self.optimize_llm_limit)

    async def _scan_full_for_optimization(self) -> dict:
        """Monthly full scan: sweep the whole library for optimization proposals.
        月度全量扫描：完整扫描记忆库，提出优化提案。"""
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Full optimization scan: list failed / 列表失败: {e}")
            return {"error": str(e)}
        if not all_buckets:
            return {"scanned": 0, "proposals": 0}
        # Full scan: rule checks cover everything; LLM re-analysis only sampled.
        # 全量扫描：规则检查覆盖全部，LLM 重分析仅抽检（控制成本）。
        return await self._run_optimization_scan(all_buckets, llm_limit=max(1, self.optimize_llm_limit))

    async def _run_optimization_scan(self, buckets: list, llm_limit: int = 5) -> dict:
        """Shared scan pipeline: rule checks (+ optional LLM re-analysis) → proposals.
        共享扫描管线：规则检查（+ 可选 LLM 重分析）→ 生成优化提案。"""
        proposals = 0
        scanned = 0
        llm_used = 0
        # Skip buckets that already have a pending optimize proposal / 跳过已有待审优化提案的桶
        try:
            pending = await self.echo_chamber.get_pending_actions("optimize")
            pending_buckets = {p.get("data", {}).get("bucket_id", "") for p in pending}
        except Exception:
            pending_buckets = set()

        for b in buckets:
            if proposals >= self.optimize_max_proposals:
                break
            bucket_id = b.get("id", "")
            if not bucket_id or bucket_id in pending_buckets:
                continue
            scanned += 1
            suggestions = self._optimization_suggestions(b)
            # Optional LLM re-analysis to confirm/refine the tag suggestion (capped).
            # 可选 LLM 重分析，确认/修正标签建议（限量）。
            if (
                self.dehydrator is not None
                and llm_used < llm_limit
                and (b.get("content") or "").strip()
            ):
                try:
                    analysis = await self.dehydrator.analyze((b.get("content") or "")[:2000])
                    llm_used += 1
                    suggestions = self._merge_llm_suggestions(suggestions, b, analysis)
                except Exception as e:
                    logger.debug(f"Optimization LLM analyze skipped / 重分析跳过: {e}")
            if suggestions:
                await self._create_optimize_proposal(
                    bucket_id, (b.get("metadata", {}) or {}).get("name", ""), suggestions
                )
                proposals += 1
                pending_buckets.add(bucket_id)

        return {"scanned": scanned, "proposals": proposals, "llm_analyzed": llm_used}

    @staticmethod
    def _optimization_suggestions(bucket: dict) -> list:
        """Rule-based checks: missing / non-standard tags, importance, type.
        规则检查：标签缺失/非标准、权重缺失/越界、类型缺失。"""
        meta = bucket.get("metadata", {}) or {}
        suggestions = []
        primary_tags = meta.get("primary_tags") or []
        sub_tags = meta.get("sub_tags") or []
        tags = meta.get("tags") or []
        importance = meta.get("importance", 5)
        btype = meta.get("type", "")

        # --- 主标签：缺失或不在封闭词表 ---
        if not primary_tags:
            suggestions.append({
                "field": "primary_tags",
                "current": primary_tags,
                "suggested": ["生活"],
                "reason": "缺少主标签，建议补充默认主标签「生活」",
            })
        else:
            valid = [x for x in primary_tags if x in PRIMARY_TAG_VOCAB]
            invalid = [x for x in primary_tags if x not in PRIMARY_TAG_VOCAB]
            if invalid:
                suggestions.append({
                    "field": "primary_tags",
                    "current": primary_tags,
                    "suggested": valid[:1] if valid else ["生活"],
                    "reason": f"主标签 {invalid} 不在封闭词表内，建议映射到标准主标签",
                })

        # --- 全标签为空 ---
        if not tags and not primary_tags and not sub_tags and not any(
            s["field"] == "primary_tags" for s in suggestions
        ):
            suggestions.append({
                "field": "tags",
                "current": [],
                "suggested": ["生活"],
                "reason": "记忆没有任何标签，建议补充标签便于检索",
            })

        # --- 权重：缺失或越界 ---
        try:
            imp_num = int(importance)
        except (TypeError, ValueError):
            imp_num = 5
        if not (1 <= imp_num <= 10):
            suggestions.append({
                "field": "importance",
                "current": importance,
                "suggested": 5,
                "reason": f"权重值 {importance} 超出 1~10 范围，建议重置为默认值 5",
            })

        # --- 类型：缺失 ---
        if not btype:
            suggestions.append({
                "field": "type",
                "current": "",
                "suggested": "event",
                "reason": "缺少类型字段，建议按默认事件类型归类",
            })

        return suggestions

    @staticmethod
    def _merge_llm_suggestions(suggestions: list, bucket: dict, analysis: dict) -> list:
        """Merge LLM re-analysis into rule suggestions (tag dimension only).
        将 LLM 重分析结果合并进规则建议（仅标签维度）。"""
        meta = bucket.get("metadata", {}) or {}
        current_primary = meta.get("primary_tags") or []
        llm_primary = [t for t in (analysis.get("primary_tags") or []) if t in PRIMARY_TAG_VOCAB]
        if llm_primary and set(llm_primary) != set(current_primary):
            for i, s in enumerate(suggestions):
                if s["field"] == "primary_tags":
                    suggestions[i] = {
                        "field": "primary_tags",
                        "current": current_primary,
                        "suggested": llm_primary,
                        "reason": f"重分析后建议主标签调整为 {llm_primary}",
                    }
                    return suggestions
            suggestions.insert(0, {
                "field": "primary_tags",
                "current": current_primary,
                "suggested": llm_primary,
                "reason": f"重分析后建议主标签调整为 {llm_primary}",
            })
        return suggestions

    async def _create_optimize_proposal(self, bucket_id: str, bucket_name: str, suggestions: list) -> str:
        """Write an optimize proposal to the echo chamber — proposal only.
        将优化提案写入回音壁（仅提案，不执行任何更改）。"""
        summary = "；".join(
            f"{s['field']}: {s['current']} → {s['suggested']}（{s['reason']}）" for s in suggestions
        )
        await self.echo_chamber.add_pending_action(
            action_type="optimize",
            data={
                "bucket_id": bucket_id,
                "bucket_name": bucket_name,
                "suggestions": suggestions,
                "summary": summary,
            },
        )
        logger.info(f"Created optimize proposal for bucket {bucket_id}: {summary}")
        return bucket_id

    async def run_daily_job(self) -> dict:
        """
        Daily job: lightweight summary of today's conversations.
        Append key facts to corresponding Event Chains as temporary nodes.
        Detect and report memory conflicts.
        Do NOT delete any data.
        """
        logger.info("Running daily housekeeper job...")
        results = {}
        
        try:
            results["daily_summary"] = await self._daily_summary()
        except Exception as e:
            logger.error(f"Daily summary failed: {e}")
            results["daily_summary"] = {"error": str(e)}
        
        try:
            results["chain_updates"] = await self._daily_chain_update()
        except Exception as e:
            logger.error(f"Daily chain update failed: {e}")
            results["chain_updates"] = {"error": str(e)}
        
        try:
            results["conflicts"] = await self._daily_conflict_detection()
        except Exception as e:
            logger.error(f"Conflict detection failed: {e}")
            results["conflicts"] = {"error": str(e)}

        try:
            results["identity_detection"] = await self._daily_identity_detection()
        except Exception as e:
            logger.error(f"Identity detection failed: {e}")
            results["identity_detection"] = {"error": str(e)}

        logger.info(f"Daily job complete: {results}")
        return results

    # =========================================================
    # Daily Review — main-AI-triggered structured end-of-day package
    # 日终整理 —— 主 AI 主动调用的结构化日终"一包结果"
    #
    # Product positioning / 产品定位：
    #   - The housekeeper never interrupts the user's daily conversation.
    #     管家不主动打断用户与主 AI 的日常对话。
    #   - The main AI triggers this at end-of-day (prompt convention).
    #     日终由主 AI 在提示词约定下主动调用。
    #   - The housekeeper scans stored memories only and proposes candidates
    #     with options — it never writes/deletes anything itself.
    #     管家只扫描已入库记忆、提出候选与可选方案，不擅自做最终删改。
    #   - The main AI holds final decision power (delete / sink / raise /
    #     merge / keep / rewrite) and executes via its own tool calls.
    #     主 AI 拥有最终决策权，并通过 trace/hold 等工具执行。
    # =========================================================
    async def run_daily_review(self, record_report: bool = True, run_pipeline: bool = False) -> dict:
        """
        Run the structured end-of-day review and return a "single package" dict.
        执行结构化日终整理，返回"一包结果"字典。

        Returns:
            mode                  : "daily_review"
            review_date           : YYYY-MM-DD (UTC)
            summary_guidance      : 今日总结引导（主 AI 依据对话写出，管家不编造）
            candidates            : 记忆维护候选列表（每条含原因/可选方案/默认建议）
            execution_instructions: 主 AI 最终决策指引
            health                : 扫描错误/警告
            scan_summary          : 扫描统计
            pipeline              : （可选，run_pipeline=True 时）旧管线副作用结果
        """
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        errors: list[str] = []
        warnings: list[str] = []

        # --- Scan stored memories (no LLM, no writes) / 扫描已入库记忆 ---
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Daily review: failed to list buckets / 记忆列表失败: {e}")
            all_buckets = []
            errors.append(f"扫描记忆库失败: {e}")

        candidates: list[dict] = []
        for scan_name, scan_fn in [
            ("expired_task", self._scan_expired_tasks),
            ("stale", self._scan_stale_memories),
            ("duplicate", self._scan_duplicate_memories),
            ("conflict", self._scan_conflict_candidates),
        ]:
            try:
                candidates.extend(await scan_fn(all_buckets))
            except Exception as e:
                logger.error(f"Daily review scan {scan_name} failed / 扫描失败: {e}")
                warnings.append(f"扫描「{scan_name}」失败: {e}")

        # --- Submit duplicate candidates as merge proposals to echo chamber ---
        # --- 将重复记忆候选提交为「合并」提案到回音壁，供主 AI 审批 ---
        # 管家只提案不执行；主 AI 批准后由 approve_action 的 merge 分支真正合并。
        try:
            merge_submitted = 0
            for c in candidates:
                if c.get("category") != "duplicate":
                    continue
                detail = c.get("detail", {})
                older_id = detail.get("duplicate_of", "")
                newer_id = c.get("bucket_id", "")
                if not older_id or not newer_id or older_id == newer_id:
                    continue
                await self.echo_chamber.add_pending_action(
                    action_type="merge",
                    data={
                        "primary_bucket_id": older_id,
                        "secondary_bucket_id": newer_id,
                        "name": c.get("name", ""),
                        "reason": c.get("reason", ""),
                        "similarity": detail.get("similarity", 0),
                        "newer_created": detail.get("newer_created", ""),
                        "older_created": detail.get("older_created", ""),
                        "content_preview": detail.get("content_preview", ""),
                    },
                )
                merge_submitted += 1
            if merge_submitted:
                logger.info(f"Daily review: submitted {merge_submitted} merge proposals / 提交 {merge_submitted} 条合并提案")
        except Exception as e:
            logger.warning(f"Failed to submit merge proposals / 提交合并提案失败: {e}")
            warnings.append(f"提交合并提案失败: {e}")

        # --- Journal status for summary guidance / 日记状态（引导用） ---
        journal_status = {"exists": False, "has_event_summary": False, "has_mood_comment": False}
        try:
            # get_entry is a synchronous method — do NOT await it.
            # get_entry 为同步方法，不可 await。
            entry = self.journal_mgr.get_entry(today)
            if entry:
                journal_status["exists"] = True
                journal_status["has_event_summary"] = bool((entry.get("event_summary") or "").strip())
                journal_status["has_mood_comment"] = bool((entry.get("mood_comment") or "").strip())
        except Exception as e:
            warnings.append(f"读取日记状态失败: {e}")

        # --- Optional legacy pipeline side-effects / 可选旧管线副作用 ---
        # Used only by run_housekeeper() (backward compat). The new daily_review
        # interface itself stays read-only unless the caller opts in.
        # 仅供 run_housekeeper() 兼容使用；daily_review 默认保持只读。
        pipeline = {}
        if run_pipeline:
            for task_name, task_fn in [
                ("daily_summary", self._daily_summary),
                ("chain_updates", self._daily_chain_update),
                ("conflicts", self._daily_conflict_detection),
                ("identity_detection", self._daily_identity_detection),
            ]:
                try:
                    pipeline[task_name] = await task_fn()
                except Exception as e:
                    logger.error(f"Daily review pipeline {task_name} failed: {e}")
                    pipeline[task_name] = {"error": str(e)}

        # --- Cycle status for health context / 生理周期状态（健康上下文）---
        cycle_status = {}
        if getattr(self, "cycle_tracker", None) is not None:
            try:
                summary = self.cycle_tracker.get_cycle_summary()
                if summary and summary.get("total_records", 0) > 0:
                    cycle_status = summary
            except Exception as e:
                warnings.append(f"读取周期状态失败: {e}")

        # --- Build the review package / 组装一包结果 ---
        candidates_by_category = {}
        for c in candidates:
            cat = c.get("category", "other")
            candidates_by_category[cat] = candidates_by_category.get(cat, 0) + 1

        result = {
            "mode": "daily_review",
            "review_date": today,
            "summary_guidance": self._build_summary_guidance(today, journal_status),
            "candidates": candidates,
            "execution_instructions": self._build_execution_instructions(),
            "health": {"errors": errors, "warnings": warnings},
            "cycle_status": cycle_status,
            "scan_summary": {
                "scanned_buckets": len(all_buckets),
                "candidates_total": len(candidates),
                "candidates_by_category": candidates_by_category,
            },
        }
        if pipeline:
            result["pipeline"] = pipeline

        # --- Record the scan report to echo chamber for later reference / ---
        # --- 回音壁记录本次日终扫描摘要（仅存档，最终写操作仍以主 AI 工具为准）---
        if record_report:
            try:
                await self.echo_chamber.write_digest(
                    digest_type="daily_review",
                    content=self._render_review_report_text(result),
                    metadata={
                        "review_date": today,
                        "scanned_buckets": len(all_buckets),
                        "candidates_total": len(candidates),
                        "candidates_by_category": candidates_by_category,
                        "journal_status": journal_status,
                        "candidates": candidates,
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to write daily review report / 写日终报告失败: {e}")
                warnings.append(f"写回音壁日终报告失败: {e}")

        logger.info(f"Daily review complete: {len(candidates)} candidates across {len(all_buckets)} buckets")
        return result

    # ---------------------------------------------------------
    # Scan 1: expired task-type memories (simple explainable rule)
    # 过期任务扫描：task_flag 且正文含明确日期且日期已过去
    # ---------------------------------------------------------
    async def _scan_expired_tasks(self, all_buckets: list) -> list:
        import re as _re
        today = datetime.now(timezone.utc).date()
        candidates = []
        for b in all_buckets:
            meta = b["metadata"]
            if not meta.get("task_flag"):
                continue
            if meta.get("resolved", False):
                continue
            content = b["content"]
            dates = []
            # YYYY-MM-DD / YYYY/M/D / YYYY年M月D日 / YYYY.M.D
            for m in _re.finditer(r'(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})', content):
                try:
                    d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
                    if d not in dates:
                        dates.append(d)
                except ValueError:
                    continue
            # M月D日（按今年推断）
            for m in _re.finditer(r'(\d{1,2})月(\d{1,2})日', content):
                try:
                    d = datetime(today.year, int(m.group(1)), int(m.group(2))).date()
                    if d not in dates:
                        dates.append(d)
                except ValueError:
                    continue
            past_dates = [d for d in dates if d < today]
            if not past_dates:
                continue
            last_date = max(past_dates)
            days_past = (today - last_date).days
            candidates.append({
                "bucket_id": b["id"],
                "name": meta.get("name", "") or b["content"][:40],
                "category": "expired_task",
                "reason": f"任务型记忆，正文含明确日期 {last_date.isoformat()}，已过去 {days_past} 天",
                "options": ["mark_resolved", "archive_or_sink", "delete", "keep"],
                "default_suggestion": "mark_resolved",
                "detail": {
                    "created": meta.get("created", ""),
                    "importance": safe_int(meta.get("importance"), 5),
                    "last_accessed": meta.get("last_accessed", meta.get("created", "")),
                    "content_preview": b["content"][:120],
                },
            })
        return candidates

    # ---------------------------------------------------------
    # Scan 2: stale / low-value memories (simple explainable rule)
    # 长期低价值扫描：30天未访问 + 低重要度 + 低激活次数
    # ---------------------------------------------------------
    async def _scan_stale_memories(self, all_buckets: list) -> list:
        now = datetime.now(timezone.utc)
        candidates = []
        for b in all_buckets:
            meta = b["metadata"]
            btype = meta.get("type")
            if btype in ("permanent", "feel", "identity", "pattern", "experience", "milestone", "voice", "boundary", "ephemeral"):
                continue
            if meta.get("pinned") or meta.get("protected"):
                continue
            if meta.get("resolved", False):
                continue
            importance = safe_int(meta.get("importance"), 5)
            if importance >= 7:
                continue
            activation_count = safe_int(meta.get("activation_count"), 0)
            if activation_count >= 3:
                continue
            last_str = meta.get("last_accessed", meta.get("created", ""))
            try:
                last = datetime.fromisoformat(str(last_str))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                days_since = max(0, (now - last).days)
            except (ValueError, TypeError):
                days_since = 30  # Unknown timestamp → treat as stale-eligible / 未知时间按可候选处理
            if days_since < 30:
                continue
            # Simple explainable weight (README 权重池浮现公式的简化版)
            # 简化权重 = 唤醒度×0.3 + 重要度/10×0.2 + 时间亲近度×0.5
            arousal = safe_float(meta.get("arousal"), 0.3)
            time_proximity = max(0.0, 1.0 - days_since / 30.0)
            weight = arousal * 0.3 + (importance / 10.0) * 0.2 + time_proximity * 0.5
            candidates.append({
                "bucket_id": b["id"],
                "name": meta.get("name", "") or b["content"][:40],
                "category": "stale",
                "reason": (
                    f"{days_since}天未访问，重要度{importance}，激活{activation_count}次，"
                    f"估算权重 {weight:.2f}"
                ),
                "options": ["keep", "archive_or_sink", "raise_importance", "delete"],
                "default_suggestion": "archive_or_sink" if weight < 0.3 else "keep",
                "detail": {
                    "created": meta.get("created", ""),
                    "importance": importance,
                    "activation_count": activation_count,
                    "last_accessed": last_str,
                    "weight": round(weight, 3),
                    "content_preview": b["content"][:120],
                },
            })
        return candidates

    # ---------------------------------------------------------
    # Scan 3: duplicate memories (conservative, no aggressive merging)
    # 重复记忆扫描：内容相似度 ≥88 才标记（保守策略）
    # ---------------------------------------------------------
    async def _scan_duplicate_memories(self, all_buckets: list) -> list:
        if not HAS_RAPIDFUZZ:
            return []
        # Only compare regular memory buckets / 仅比较常规记忆桶
        pool = [
            b for b in all_buckets
            if b.get("metadata", {}).get("type") not in ("permanent", "feel", "identity", "pattern", "experience", "milestone", "voice", "boundary", "ephemeral")
        ]
        # Bounded O(n²): cap the pool by creation order / 限制比较规模，按创建时间排序取前 300
        pool.sort(key=lambda b: b.get("metadata", {}).get("created", ""))
        pool = pool[:300]
        candidates = []
        emitted = set()
        for i in range(len(pool)):
            a = pool[i]
            if a.get("metadata", {}).get("pinned") or a.get("metadata", {}).get("protected"):
                continue
            for j in range(i + 1, len(pool)):
                b = pool[j]
                if b.get("metadata", {}).get("pinned") or b.get("metadata", {}).get("protected"):
                    continue
                ratio = fuzz.ratio(a["content"][:500], b["content"][:500])
                if ratio < 88:
                    continue
                key = (a["id"], b["id"])
                if key in emitted:
                    continue
                emitted.add(key)
                # Emit the newer bucket as the candidate, suggest merging into the older
                # 以较新的桶为候选，建议并入较早的桶
                if a.get("metadata", {}).get("created", "") <= b.get("metadata", {}).get("created", ""):
                    older, newer = a, b
                else:
                    older, newer = b, a
                candidates.append({
                    "bucket_id": newer["id"],
                    "name": newer.get("metadata", {}).get("name", "") or newer["content"][:40],
                    "category": "duplicate",
                    "reason": f"与记忆 {older['id']} 内容高度相似（相似度 {ratio}%），疑似重复记录",
                    "options": [f"merge_with:{older['id']}", "keep"],
                    "default_suggestion": f"merge_with:{older['id']}",
                    "detail": {
                        "similarity": ratio,
                        "duplicate_of": older["id"],
                        "older_created": older.get("metadata", {}).get("created", ""),
                        "newer_created": newer.get("metadata", {}).get("created", ""),
                        "content_preview": newer["content"][:120],
                    },
                })
        return candidates

    # ---------------------------------------------------------
    # Scan 4: conflict candidates (reuse existing regex rules, no submission)
    # 冲突候选扫描：复用现有规则检测，但不自动提交回音壁提案
    # ---------------------------------------------------------
    async def _scan_conflict_candidates(self, all_buckets: list) -> list:
        today_start = datetime.now(timezone.utc) - timedelta(hours=24)
        today_buckets = []
        history_buckets = []
        for b in all_buckets:
            meta = b["metadata"]
            if meta.get("type") in ("permanent", "feel") or meta.get("pinned") or meta.get("protected"):
                continue
            created_str = meta.get("created", "")
            if not created_str:
                continue
            try:
                created = datetime.fromisoformat(str(created_str))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created >= today_start:
                    today_buckets.append(b)
                else:
                    history_buckets.append(b)
            except (ValueError, TypeError):
                continue
        candidates = []
        seen = set()
        for tb in today_buckets:
            for hb in history_buckets:
                # --- 主题隔离优先：不同主题域禁止比对 ---
                if self._domain_isolated(tb["metadata"], hb["metadata"]):
                    continue
                r = self._detect_conflict(tb["content"], hb["content"])
                if not r:
                    continue
                key = (tb["id"], hb["id"])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({
                    "bucket_id": tb["id"],
                    "name": tb["metadata"].get("name", "") or tb["content"][:40],
                    "category": "conflict",
                    "reason": r["reason"],
                    "options": ["keep", "mark_old_resolved", "delete_new"],
                    "default_suggestion": "keep",
                    "detail": {
                        "conflict_type": r["type"],
                        "confidence_score": r.get("confidence_score", 0.0),
                        "new_bucket_id": tb["id"],
                        "old_bucket_id": hb["id"],
                        "old_content_preview": hb["content"][:120],
                        "new_content_preview": tb["content"][:120],
                    },
                })
                # --- 单次扫描产出上限：超过 cleanup_max_conflicts 立即停止 ---
                if len(candidates) >= self.cleanup_max_conflicts:
                    return candidates
        return candidates

    # ---------------------------------------------------------
    # Summary guidance for the main AI / 今日总结引导
    # ---------------------------------------------------------
    def _build_summary_guidance(self, today: str, journal_status: dict) -> dict:
        if journal_status.get("has_event_summary"):
            journal_note = (
                "今日日记已存在事件摘要；如对话有新进展，可用 complete_journal 补充或更新。"
            )
        else:
            journal_note = (
                "今日日记尚无事件摘要：请依据今日对话写出事实要点，"
                "调用 complete_journal(date, event_summary=..., mood_comment=..., emotion_tags=...) 落库。"
            )
        return {
            "message": (
                "请根据本日对话写出今日事件的事实要点（谁/做了什么/结果/决定），并可附上整体情绪点评。"
                "管家无法看到未写入 Brain 的对话全文，以下仅为结构引导，内容需由你（主 AI）依据对话理解写出，"
                "不要编造对话中未出现的日程或事件。"
            ),
            "suggested_fields": {
                "event_summary": "今日事实要点（主 AI 依据对话写出）",
                "mood_comment": "可选：今日整体情绪点评",
                "emotion_tags": "可选：情绪标签，如 焦虑,疲惫,期待（字符串或数组均可）",
            },
            "hold_template": {
                "content": "单条记忆正文",
                "valence": "0.0~1.0（可选，优先于自动打标）",
                "arousal": "0.0~1.0（可选）",
                "tags": "标签列表或逗号分隔字符串",
                "importance": "1~10，默认 5",
                "source": "如 daily_review / chat / yeeban",
            },
            "journal_status": journal_status,
            "journal_note": journal_note,
        }

    # ---------------------------------------------------------
    # Execution instructions / 执行说明（主 AI 最终决策）
    # ---------------------------------------------------------
    def _build_execution_instructions(self) -> str:
        return (
            "以上候选仅为管家扫描结果与参考建议，最终操作权在你（主 AI）。"
            "请逐条（或按策略）做出最终决策，并使用对应工具执行，不必局限于管家的默认建议：\n"
            "  - 删除: trace(bucket_id=..., delete=True)\n"
            "  - 沉底: trace(bucket_id=..., resolved=1)（任务型需加 force_resolved=1）\n"
            "  - 提权: trace(bucket_id=..., importance=N)\n"
            "  - 合并: 先读取两条记忆内容，用 hold() 写入合并后的完整记忆，再 trace(delete=True) 删除冗余条目\n"
            "  - 保留: 不做任何操作\n"
            "你也可改写、拆分，或对候选之外的记忆做任意调整。所有写操作均以你调用的工具为准，"
            "管家不做任何删除或修改。"
        )

    # ---------------------------------------------------------
    # Render the echo-chamber review report text / 回音壁日终报告文本
    # ---------------------------------------------------------
    def _render_review_report_text(self, result: dict) -> str:
        ss = result.get("scan_summary", {})
        lines = [f"日终整理报告（{result.get('review_date', '')}）"]
        lines.append(f"扫描记忆 {ss.get('scanned_buckets', 0)} 条，候选 {ss.get('candidates_total', 0)} 条")
        for cat, n in (ss.get("candidates_by_category", {}) or {}).items():
            lines.append(f"  - {cat}: {n}")
        lines.append("")
        for c in result.get("candidates", []):
            lines.append(f"[{c.get('category')}] {c.get('name', '')} (id={c.get('bucket_id')})")
            lines.append(f"  原因: {c.get('reason', '')}")
            lines.append(f"  可选方案: {', '.join(c.get('options', []))}  |  默认建议(仅参考): {c.get('default_suggestion', '')}")
        if result.get("pipeline"):
            lines.append("")
            lines.append("管线（兼容模式）结果:")
            for k, v in result["pipeline"].items():
                lines.append(f"  - {k}: {v}")
        return "\n".join(lines)

    async def run_weekly_job(self) -> dict:
        """
        Weekly job: deduplicate and merge Event Chains.
        Scan stale low-weight memories, mark pending_delete, generate cleanup drafts.
        Submit to echo_chamber for main AI final review.
        """
        logger.info("Running weekly housekeeper job...")
        results = {}
        
        try:
            results["chain_merge"] = await self._weekly_chain_merge()
        except Exception as e:
            logger.error(f"Weekly chain merge failed: {e}")
            results["chain_merge"] = {"error": str(e)}
        
        try:
            results["cleanup_scan"] = await self._weekly_cleanup_scan()
        except Exception as e:
            logger.error(f"Weekly cleanup scan failed: {e}")
            results["cleanup_scan"] = {"error": str(e)}

        try:
            results["pattern_proposals"] = await self._extract_pattern_proposals()
        except Exception as e:
            logger.error(f"Weekly pattern extraction failed: {e}")
            results["pattern_proposals"] = {"error": str(e)}
        
        try:
            await self._write_weekly_digest(results)
        except Exception as e:
            logger.error(f"Weekly digest write failed: {e}")
        
        logger.info(f"Weekly job complete: {results}")
        return results
    
    async def _daily_summary(self) -> dict:
        """Generate daily summary of today's buckets with mood analysis using AI.

        Improvements:
        1. Strict time window: only buckets from last 24 hours (now - 24h)
        2. NULL created_at filtering: skip buckets without timestamps
        3. Topic isolation: group by chain_id before sending to LLM
        """
        today_start = datetime.now(timezone.utc) - timedelta(hours=24)
        
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return {"error": str(e)}
        
        # --- Strict time window + NULL created_at filtering ---
        # --- 严格时间窗口 + 过滤无时间戳脏数据 ---
        today_buckets = []
        for b in all_buckets:
            created_str = b["metadata"].get("created", "")
            if not created_str:
                continue  # Skip NULL/dirty data / 跳过无时间戳脏数据
            try:
                created = datetime.fromisoformat(str(created_str))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created >= today_start:
                    today_buckets.append(b)
            except (ValueError, TypeError):
                continue  # Skip unparseable timestamps / 跳过无法解析的时间戳
        
        if not today_buckets:
            return {"message": "No buckets from today"}
        
        # --- Topic isolation: group buckets by chain_id ---
        # --- 主题隔离：按事件链分组 ---
        try:
            all_chains = await self.get_event_chains()
        except Exception as e:
            logger.warning(f"Failed to load event chains for topic isolation: {e}")
            all_chains = []
        
        # Build bucket_id → chain mapping
        chain_bucket_ids = {}  # chain_id → set of bucket_ids
        chain_map = {}  # chain_id → EventChain
        for chain in all_chains:
            chain_bucket_ids[chain.chain_id] = set(chain.source_bucket_ids)
            chain_map[chain.chain_id] = chain
        
        # Classify each today's bucket
        chain_groups = {}  # chain_id → list of buckets
        atomic_buckets = []  # buckets not belonging to any chain
        
        for b in today_buckets:
            bucket_id = b.get("id", "")
            assigned = False
            for chain_id, source_ids in chain_bucket_ids.items():
                if bucket_id in source_ids:
                    chain_groups.setdefault(chain_id, []).append(b)
                    assigned = True
                    break
            if not assigned:
                atomic_buckets.append(b)
        
        # --- Generate summaries per group ---
        # --- 按组生成摘要 ---
        group_summaries = []
        bucket_count = 0
        all_contents = []
        
        # Helper to call LLM for a group
        async def _summarize_group(group_label: str, group_buckets: list) -> str:
            nonlocal bucket_count, all_contents
            bucket_count += len(group_buckets)
            contents = "\n\n".join([b["content"] for b in group_buckets[:30]])
            all_contents.extend([b["content"] for b in group_buckets])
            
            if not self.dehydrator or not self.dehydrator.client:
                return "\n".join([f"- {b['content'][:100]}" for b in group_buckets])
            
            try:
                prompt = f"""你是一个AI记忆管家。请根据以下记忆内容，生成一段有信息量的主题摘要。

主题：{group_label}

要求：
1. 用中文，语言自然流畅
2. 提炼核心事实：谁做了什么、结果如何、有什么感受或决定
3. 不要简单罗列条目，要整合成连贯的一段话
4. 突出关键信息和变化，去掉无关细节
5. 80-150字

内容：
{contents[:2000]}
"""
                response = await self.dehydrator.client.chat.completions.create(
                    model=self.dehydrator.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=300,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"Group summary failed for {group_label}: {e}")
                return "\n".join([f"- {b['content'][:80]}" for b in group_buckets])
        
        # Summarize event chain groups
        for chain_id, buckets in chain_groups.items():
            topic = chain_map[chain_id].topic if chain_id in chain_map else chain_id
            summary = await _summarize_group(f"事件链「{topic}」", buckets)
            group_summaries.append(f"【{topic}】\n{summary}")
        
        # Summarize atomic buckets (isolated events)
        if atomic_buckets:
            # Further group atomic buckets by semantic similarity using _extract_topics
            atomic_groups = await self._extract_topics(atomic_buckets)
            for topic_label, topic_buckets in atomic_groups.items():
                summary = await _summarize_group(topic_label, topic_buckets)
                group_summaries.append(f"【{topic_label}】\n{summary}")
        
        # --- Assemble final daily digest ---
        # --- 组装最终每日摘要 ---
        summary = ""
        if group_summaries:
            summary = "\n\n".join(group_summaries)
        else:
            summary = "\n".join([f"- {b['content'][:100]}" for b in today_buckets])
        
        # --- Mood analysis ---
        mood_tags = self._analyze_daily_mood(today_buckets, all_contents)
        
        await self.echo_chamber.write_digest(
            digest_type="daily",
            content=summary,
            metadata={
                "bucket_count": bucket_count,
                "chain_groups": len(chain_groups),
                "atomic_groups": len(atomic_buckets),
                "mood_tags": mood_tags["tags"],
                "mood_score": mood_tags["score"],
                "mood_level": mood_tags["level"],
            }
        )

        # Journal draft is OFF by default (product positioning: the housekeeper
        # cannot see the un-stored conversation, so today's summary must be
        # written by the main AI based on its own understanding of the dialogue.
        # The housekeeper only provides structure & storage assistance via
        # complete_journal). Users can opt back in with housekeeper.auto_journal_draft.
        # 日记草稿默认关闭：管家看不到未落库对话全文，今日总结由主 AI 依据对话写出
        # （通过 complete_journal 落库）；如需旧行为可开启 housekeeper.auto_journal_draft。
        if self.auto_journal_draft:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            await self.journal_mgr.create_entry(
                date=today,
                event_summary=summary,
                mood_comment="",  # To be filled by main AI / 由主 AI 后续填写
                emotion_tags="",
            )

        logger.info(f"Daily summary: {bucket_count} buckets across {len(chain_groups)} chains + {len(atomic_buckets)} atomic events")
        
        return {
            "buckets_processed": bucket_count,
            "chain_groups": len(chain_groups),
            "atomic_events": len(atomic_buckets),
            "mood_tags": mood_tags["tags"],
            "mood_level": mood_tags["level"],
        }
    
    def _analyze_daily_mood(self, buckets: list, contents: list) -> dict:
        """
        Analyze the emotional baseline for today's conversations.
        Returns mood tags and score.
        """
        import re
        
        mood_patterns = {
            "anxious": [
                r'(焦虑|不安|烦躁|心烦|着急|紧张)',
                r'(压力大|压力|焦虑症)',
                r'(睡不着|失眠|睡不好)',
            ],
            "unwell": [
                r'(痛|疼|难受|不舒服)',
                r'(生病|感冒|发烧|咳嗽)',
                r'(肚子痛|胃痛|头痛|痛经)',
                r'(疲惫|累|疲倦)',
            ],
            "sad": [
                r'(难过|伤心|悲伤|沮丧)',
                r'(失望|失落|绝望)',
                r'(想哭|流泪)',
            ],
            "happy": [
                r'(开心|高兴|快乐|喜悦)',
                r'(幸福|满足|满意)',
                r'(兴奋|激动)',
            ],
            "angry": [
                r'(生气|愤怒|发火)',
                r'(讨厌|烦|烦死)',
                r'(无语|受不了)',
            ],
        }
        
        tag_counts = {tag: 0 for tag in mood_patterns}
        
        for content in contents:
            for tag, patterns in mood_patterns.items():
                for pattern in patterns:
                    if re.search(pattern, content):
                        tag_counts[tag] += 1
        
        detected_tags = [tag for tag, count in tag_counts.items() if count > 0]
        
        negative_tags = {"anxious", "unwell", "sad", "angry"}
        positive_tags = {"happy"}
        
        negative_count = sum(tag_counts[t] for t in negative_tags if t in tag_counts)
        positive_count = sum(tag_counts[t] for t in positive_tags if t in tag_counts)
        
        total_count = negative_count + positive_count
        
        if total_count == 0:
            score = 0
            level = "neutral"
        elif negative_count > positive_count:
            score = -negative_count
            if negative_count >= 3:
                level = "low"
            else:
                level = "slightly_low"
        else:
            score = positive_count
            if positive_count >= 3:
                level = "high"
            else:
                level = "slightly_high"
        
        return {
            "tags": detected_tags,
            "score": score,
            "level": level,
        }
    
    async def _daily_chain_update(self) -> dict:
        """Update event chains with today's relevant buckets (temporary nodes)."""
        today_start = datetime.now(timezone.utc) - timedelta(hours=24)
        
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return {"error": str(e)}
        
        today_buckets = []
        for b in all_buckets:
            meta = b["metadata"]
            if meta.get("type") in ("permanent", "feel") or meta.get("pinned") or meta.get("protected"):
                continue
            
            created_str = meta.get("created", "")
            if not created_str:
                continue  # Skip NULL/dirty data / 跳过无时间戳脏数据
            try:
                created = datetime.fromisoformat(str(created_str))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created >= today_start:
                    today_buckets.append(b)
            except (ValueError, TypeError):
                continue
        
        chains = await self.get_event_chains()
        chains_updated = 0
        
        for bucket in today_buckets:
            if not self._is_long_term_event(bucket):
                continue
            
            topic = await self._generate_topic_name(bucket["content"])
            matched_chain = None
            
            for chain in chains:
                if HAS_RAPIDFUZZ:
                    if fuzz.ratio(chain.topic, topic) >= 60:
                        matched_chain = chain
                        break
                else:
                    if topic in chain.topic or chain.topic in topic:
                        matched_chain = chain
                        break
            
            if matched_chain:
                await self._append_temporary_node(matched_chain, bucket)
                chains_updated += 1
            else:
                if await self._should_create_chain(topic):
                    await self._create_event_chain(topic, [bucket])
                    chains_updated += 1
        
        # --- Rebuild cross-chain entity graph / 重建事件链实体交叉索引 ---
        graph = await self._rebuild_chain_graph()
        
        return {"chains_updated": chains_updated, **graph}
    
    def _is_long_term_event(self, bucket: dict) -> bool:
        """
        Determine if this is a long-term event that should be in an Event Chain.
        Rules:
        - Atomic events (一次性事实、即时状态等) -> NO
        - Long-term events (病程跟进、备考、项目开发、家庭健康等) -> YES
        """
        content = bucket["content"]
        meta = bucket["metadata"]
        
        import re
        
        atomic_patterns = [
            r'^(吃|喝|买|去)\s*[了过]$',
            r'^我去洗个手$',
            r'^我去吃饭$',
            r'^我去喝水$',
            r'^我去睡觉$',
            r'^我走了$',
            r'^再见$',
            r'^晚安$',
            r'^(我室友|室友)\s*(刚才|刚刚)',
            r'^(我同学|同学)\s*(刚才|刚刚)',
            r'^(他|她|他们|她们)\s*(刚才|刚刚)',
            r'^有人\s*(叫我|找我|敲门)',
            r'^(快递|外卖)\s*(到了|来了)',
            r'^(灯|空调|电视)\s*(开了|关了)',
        ]
        
        for pattern in atomic_patterns:
            if re.match(pattern, content.strip()):
                logger.info(f"[Event Classification] 一次性事件: {content[:50]} (匹配模式: {pattern})")
                return False
        
        long_term_patterns = [
            r'(生病|身体不适|不舒服|难受|痛)',
            r'(备考|学习|考试)',
            r'(项目|开发|工作)',
            r'(恋爱|感情|关系)',
            r'(减肥|健身|运动)',
            r'(旅行|出差)',
            r'(持续|一直|经常|频繁)',
            r'(复查|复诊|检查|化验)',
            r'(药|药单|开药|吃药)',
            r'(妈妈|爸爸|家人|父母)\s*(生病|看病|住院)',
            r'(手术|治疗|疗程)',
            r'(病情|症状|好转|恶化)',
            r'(关于.*的事|关于.*的问题)',
        ]
        
        for pattern in long_term_patterns:
            if re.search(pattern, content):
                logger.info(f"[Event Classification] 长效事件: {content[:50]} (匹配模式: {pattern})")
                return True
        
        tags = meta.get("tags", [])
        long_term_tags = ["health", "study", "work", "project", "relationship", "family"]
        if any(tag.lower() in long_term_tags for tag in tags):
            logger.info(f"[Event Classification] 长效事件(标签): {content[:50]}")
            return True
        
        logger.info(f"[Event Classification] 默认归类为一次性事件: {content[:50]}")
        return False
    
    async def _daily_conflict_detection(self) -> dict:
        """
        Detect memory conflicts between today's memories and historical memories.
        If conflicts found, submit to echo_chamber for main AI review.
        """
        today_start = datetime.now(timezone.utc) - timedelta(hours=24)
        
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return {"error": str(e)}
        
        today_buckets = []
        history_buckets = []
        
        for b in all_buckets:
            meta = b["metadata"]
            if meta.get("type") in ("permanent", "feel") or meta.get("pinned") or meta.get("protected"):
                continue
            
            created_str = meta.get("created", "")
            if not created_str:
                continue  # Skip NULL/dirty data / 跳过无时间戳脏数据
            try:
                created = datetime.fromisoformat(str(created_str))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created >= today_start:
                    today_buckets.append(b)
                else:
                    history_buckets.append(b)
            except (ValueError, TypeError):
                continue
        
        if not today_buckets or not history_buckets:
            return {"message": "Not enough data for conflict detection"}
        
        conflicts_found = 0
        
        for today_bucket in today_buckets:
            today_content = today_bucket["content"]
            today_meta = today_bucket["metadata"]
            
            for history_bucket in history_buckets:
                # --- 主题隔离优先：不同主题域的记忆桶绝对禁止进行矛盾比对 ---
                # --- Topic-first filtering: isolated domains must never be compared ---
                if self._domain_isolated(today_meta, history_bucket["metadata"]):
                    continue
                
                history_content = history_bucket["content"]
                
                conflict_result = self._detect_conflict(today_content, history_content)
                if conflict_result:
                    conflicts_found += 1
                    
                    await self.echo_chamber.add_pending_action(
                        action_type="conflict",
                        data={
                            "new_bucket_id": today_bucket["id"],
                            "old_bucket_id": history_bucket["id"],
                            # --- Explicit memory ids for approval execution / 显式记忆 ID，供审批执行使用 ---
                            "new_memory_id": today_bucket["id"],
                            "old_memory_id": history_bucket["id"],
                            "new_content": today_content[:200],
                            "old_content": history_content[:200],
                            "conflict_type": conflict_result["type"],
                            "conflict_reason": conflict_result["reason"],
                            "confidence_score": conflict_result.get("confidence_score", 0.0),
                            "new_metadata": {
                                "created": today_bucket["metadata"].get("created", ""),
                                "name": today_bucket["metadata"].get("name", ""),
                            },
                            "old_metadata": {
                                "created": history_bucket["metadata"].get("created", ""),
                                "name": history_bucket["metadata"].get("name", ""),
                            },
                        }
                    )
                    
                    logger.info(f"Conflict detected: {conflict_result['reason']}")
                    # --- 单次扫描产出上限：超过 cleanup_max_conflicts 立即停止 ---
                    # --- Hard cap: stop as soon as the per-scan proposal limit is reached ---
                    if conflicts_found >= self.cleanup_max_conflicts:
                        return {"conflicts_found": conflicts_found, "capped": True}
        
        return {"conflicts_found": conflicts_found}
    
    # ---------------------------------------------------------
    # Conflict detection: topic isolation + structured propositions
    # 冲突检测：主题隔离优先 + 语义主旨比对（命题化）+ 置信度门槛
    # ---------------------------------------------------------
    def _extract_propositions(self, text: str) -> list:
        """
        Extract structured propositions (subject + action + object + polarity).
        Rule-based lightweight parser — no LLM, no keyword-only regex matching.
        轻量规则式命题抽取：按标点切句 → 定位主语/动作/对象 → 判断否定极性。
        用于语义主旨比对，替代旧的“要/不要”字面正则粗暴匹配。
        """
        import re
        # 否定词：'别' 用负向回顾排除 特别/区别/分别/告别/离别/个别 等非否定语境
        _NEG = re.compile(
            r'(不|没|无|(?<![特区分告离个])别|勿|未|从不|从未|不再|从没|尚未|禁止|拒绝|不要|不想|不愿意|没法|无法|不能|不会|还没)'
        )
        _SENT = re.compile(r'[。！？!?；;\n]+')
        # 主语候选：人称代词与常用称呼（voice/identity 常用实体）
        _SUBJ = re.compile(
            r'^(我|你|我们|咱们|您|他|她|它|他们|她们|它们|TA|ta|Ta|顾尘|祁桉|宝宝|爸妈|妈妈|爸爸|朋友|客户|同事|老板|老婆|老公)'
        )
        # 动作动词候选：常见谓词（中文无空格分词，采用谓词词典最长前缀命中）
        _VERB = re.compile(
            r'(喜欢|讨厌|希望|期望|愿意|坚持|放弃|开始|结束|完成|安排|决定|同意|反对|答应|拒绝|允许|禁止|打算|计划|'
            r'需要|必须|应该|可以|不能|想吃|不想吃|吃|喝|用|买|做|打|玩|睡|去|来|说|写|看|听|学|带|穿|戴|住|开|关|改|删|存)'
        )
        props = []
        for sent in _SENT.split(text):
            sent = sent.strip().strip('“”"\'‘’（）()…、').strip()
            if not sent or len(sent) < 3:
                continue
            negated = bool(_NEG.search(sent))
            subj_m = _SUBJ.search(sent)
            subject = subj_m.group(0) if subj_m else sent[:2]
            verb_m = _VERB.search(sent)
            if verb_m:
                verb = verb_m.group(0)
                obj = sent[verb_m.end():][:40]
            else:
                verb = ""
                obj = sent[:40]
            props.append({
                "subject": subject,
                "verb": verb,
                "object": obj.strip(),
                "negated": negated,
                "raw": sent[:80],
            })
        return props

    def _propositions_conflict(self, props_new: list, props_old: list) -> dict | None:
        """
        Compare propositions between new and old content.
        A conflict is reported only when the same subject+action core opposes in
        polarity AND similarity lifts confidence above the threshold (> 0.85).
        语义主旨比对：同一主语+动作的核心命题若极性相反且相似度足够，
        计算 confidence_score；仅高于门槛才判为冲突。
        """
        # 动词同义组：同一组内的动词视为同一动作（如 喜欢/想吃/想要 都是偏好动词）
        # Verb synonym groups: verbs in the same group are treated as the same action.
        _VERB_GROUPS = {
            "preference": {"喜欢", "爱", "爱吃", "想吃", "想要", "想", "要", "希望", "愿意", "讨厌", "不爱", "不喜欢", "不想"},
            "consumption": {"吃", "喝", "用", "买", "玩"},
            "movement": {"去", "来", "走", "回", "到"},
            "communication": {"说", "写", "看", "听", "学", "读"},
        }

        def _ratio(a: str, b: str) -> float:
            if not a or not b:
                return 0.0
            if HAS_RAPIDFUZZ:
                return float(fuzz.ratio(a, b))
            return 100.0 if a == b else 0.0

        def _verb_match(v1: str, v2: str) -> bool:
            """Verbs match when identical, similar, or in the same synonym group.
            动词匹配：相同 / 相似 / 属于同一同义组 均视为匹配。"""
            if not v1 or not v2:
                return False
            if _ratio(v1, v2) >= 60:
                return True
            for group in _VERB_GROUPS.values():
                if v1 in group and v2 in group:
                    return True
            return False

        for pn in props_new:
            for po in props_old:
                # 同极性不构成矛盾 / Same polarity cannot contradict
                if pn["negated"] == po["negated"]:
                    continue
                # 动作不一致 → 非同一命题 / Different action → different proposition
                if pn["verb"] and po["verb"] and not _verb_match(pn["verb"], po["verb"]):
                    continue
                vratio = _ratio(pn["verb"], po["verb"])
                # 同义组匹配但字面不相似时，按高相似度计分（如 想吃↔喜欢）
                # Synonym-group match with low literal similarity counts as high.
                if vratio < 60 and pn["verb"] and po["verb"] and _verb_match(pn["verb"], po["verb"]):
                    vratio = 100.0
                sratio = _ratio(pn["subject"], po["subject"])
                oratio = _ratio(pn["object"], po["object"])
                # confidence = 极性对立基础分 + 动词/主语/对象相似度加权
                confidence = 0.45 + 0.20 * (vratio / 100.0) + 0.15 * (sratio / 100.0) + 0.20 * (oratio / 100.0)
                confidence = min(0.99, confidence)
                if confidence > self.conflict_confidence_threshold:
                    return {
                        "type": "proposition",
                        "confidence_score": round(confidence, 2),
                        "reason": (
                            f"命题冲突：旧记录「{po['raw']}」与新记录「{pn['raw']}」"
                            f"围绕同一动作「{pn['verb'] or po['verb']}」立场相反（置信 {confidence * 100:.0f}%）"
                        ),
                    }
        return None

    def _domain_isolated(self, meta_new: dict, meta_old: dict) -> bool:
        """
        Topic-first filtering: buckets belonging to different topic domains must
        never be compared for contradictions. Returns True when the comparison
        is FORBIDDEN (isolated topic domains).
        主题隔离优先：属于不同主题域的记忆桶绝对禁止进行矛盾比对。
        缺失主题域时保守放行（避免漏检），但域集合明确分离时禁止比对。
        """
        dom_new = {str(d).strip() for d in as_list(meta_new.get("domain")) if str(d).strip()}
        dom_old = {str(d).strip() for d in as_list(meta_old.get("domain")) if str(d).strip()}
        if not dom_new or not dom_old:
            return False
        return dom_new.isdisjoint(dom_old)

    def _detect_conflict(self, new_content: str, old_content: str) -> dict | None:
        """
        Detect semantic conflicts via structured proposition matching.
        Returns {"type", "reason", "confidence_score"} only when confidence
        exceeds the threshold (default 0.85). No keyword/regex brute matching.
        结构化命题比对判定冲突：仅当置信度 > 门槛（默认 0.85）才返回结果。
        """
        props_new = self._extract_propositions(new_content)
        props_old = self._extract_propositions(old_content)
        if not props_new or not props_old:
            return None
        result = self._propositions_conflict(props_new, props_old)
        if result and result.get("confidence_score", 0.0) > self.conflict_confidence_threshold:
            return result
        return None

    async def _daily_identity_detection(self) -> dict:
        """
        Detect frequently mentioned persons in recent memories.
        If a person appears >= 3 times in the last 7 days and is not yet in identity records,
        submit a proposal to echo_chamber for the main AI to decide whether to create an identity record.

        检测近期记忆中频繁出现的人物。若某人物在过去7天内被提及>=3次且尚未收录为身份档案，
        则向回音壁提交提案，由主AI决定是否创建身份档案。
        """
        import re
        from collections import Counter

        week_ago = datetime.now(timezone.utc) - timedelta(days=7)

        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return {"error": str(e)}

        # --- Get existing identity names to exclude ---
        # --- 获取已有身份档案名称以排除（含别名，避免重复提案）---
        existing_names = set()
        try:
            if self.identity_mgr is not None:
                identities = await self.identity_mgr.list_all()
                for ident in identities:
                    meta = ident.get("metadata", {})
                    name = meta.get("name", "")
                    if name:
                        existing_names.add(name)
                    for alias in meta.get("aliases", []) or []:
                        if alias:
                            existing_names.add(alias)
            # Fallback: also scan identity-type buckets' name fields / 兜底：扫描身份桶 name 字段
            all_identity_buckets = await self.bucket_mgr.list_all(include_archive=False)
            for b in all_identity_buckets:
                if b.get("metadata", {}).get("type") == "identity":
                    n = b.get("metadata", {}).get("name", "")
                    if n:
                        existing_names.add(n)
        except Exception as e:
            logger.warning(f"Failed to load existing identities / 加载已有身份失败: {e}")

        # --- Person name patterns (Chinese names 2-4 chars, family roles) ---
        # --- 人物名称匹配模式（中文姓名2-4字，亲属称谓等）---
        # NOTE: lazy quantifier {2,4}? prevents the keyword (一起/说/…) from being
        #       swallowed into the captured name (e.g. 小明一起).
        # 注意：使用惰性量词，防止关键词（一起/说等）被贪婪捕获吞入人名。
        person_patterns = [
            r'(?:跟|和|与)([\u4e00-\u9fff]{2,4}?)(?:一起|说|聊|去|吃|玩|见面|打电话|约)',
            r'([\u4e00-\u9fff]{2,4}?)(?:说|告诉|给我|让我|帮我|找我|叫我|给我|生气|开心|难过)',
            r'(妈妈|爸爸|奶奶|爷爷|外公|外婆|舅舅|阿姨|叔叔|老师|同学|室友|朋友|同事|闺蜜|男朋友|女朋友)',
        ]

        # --- Also check identity buckets' name fields directly ---
        # --- 同时直接检查记忆桶 name 字段中的人物名 ---
        person_counter = Counter()
        person_contexts = {}
        person_emotions = {}

        for b in all_buckets:
            meta = b["metadata"]
            if meta.get("type") in ("permanent", "feel", "identity"):
                continue

            created_str = meta.get("created", "")
            if not created_str:
                continue
            try:
                created = datetime.fromisoformat(str(created_str))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < week_ago:
                    continue
            except (ValueError, TypeError):
                continue

            content = b["content"]
            found_persons = set()

            for pattern in person_patterns:
                matches = re.findall(pattern, content)
                for match in matches:
                    if isinstance(match, tuple):
                        match = match[0]
                    if match and match not in ("我", "你", "他", "她", "他们", "她们", "我们", "你们", "这个", "那个"):
                        found_persons.add(match)

            for person in found_persons:
                person_counter[person] += 1
                if person not in person_contexts:
                    person_contexts[person] = content[:80]

                valence = meta.get("valence", -1)
                if valence >= 0:
                    if person not in person_emotions:
                        person_emotions[person] = []
                    if valence < 0.3:
                        person_emotions[person].append("负面")
                    elif valence > 0.7:
                        person_emotions[person].append("正面")
                    else:
                        person_emotions[person].append("中性")

        # --- Submit proposals for high-frequency persons not in identity records ---
        # --- 为高频且未收录的人物提交提案 ---
        proposals_created = 0
        for person, count in person_counter.most_common():
            if count < 3:
                break
            if person in existing_names:
                continue

            emotions = person_emotions.get(person, [])
            dominant_emotion = ""
            if emotions:
                emotion_count = Counter(emotions)
                dominant_emotion = emotion_count.most_common(1)[0][0]

            await self.echo_chamber.add_pending_action(
                action_type="identity_proposal",
                data={
                    "person_name": person,
                    "mention_count": count,
                    "dominant_emotion": dominant_emotion,
                    "sample_context": person_contexts.get(person, ""),
                    "reason": f"人物「{person}」在过去7天内被提及{count}次"
                              + (f"，主要情绪倾向：{dominant_emotion}" if dominant_emotion else "")
                              + "，建议创建身份档案",
                },
            )
            proposals_created += 1
            logger.info(f"Identity proposal created for: {person} (mentioned {count} times)")

        return {"identity_proposals_created": proposals_created}

    # =========================================================
    # Nightly incremental dream (每日增量做梦) — incremental scan only
    # 增量扫描模式：只读当天 24h 新记忆，search() Top-K 精准比对，
    # 禁止无脑载入全量数据库进行 Full Scan。
    # =========================================================
    async def daily_incremental_dream(self, date_str: str = "") -> dict:
        """
        Nightly incremental dream scan (runs at end-of-day / sleep time).
        每日增量扫描模式（晚间做梦时运行）。

        - Reads ONLY memories created within the last 24h (raw slices / dynamic).
        - For each new memory, extracts core topic/vector keywords and calls
          search() to pull top-K (incremental_match_top_k, 3~5) highly relevant
          HISTORICAL old buckets — no full-database load & compare.
        - Compares new vs old via structured-proposition conflict detection;
          high-confidence conflicts go to the echo chamber (hard-capped).
        - Intermediate associations/drafts are written ONLY to the dream sandbox
          (temp_dreams/<date>.json), never persisted as permanent memory.
        """
        now = datetime.now(timezone.utc)
        today = date_str or now.strftime("%Y-%m-%d")
        since = now - timedelta(hours=24)

        # --- 1) Enumerate recent memories (metadata-only, cheap) ---
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Incremental dream: list failed / 增量做梦列表失败: {e}")
            return {"error": str(e)}

        new_memories = []
        for b in all_buckets:
            meta = b["metadata"]
            btype = meta.get("type", "dynamic")
            if meta.get("pinned") or meta.get("protected"):
                continue
            # Raw slices / dynamic buckets only — skip curated layers.
            # Note: list_all() normalizes type "dynamic" → "event".
            # 仅处理新记忆（动态/原始切片）；list_all 会把 dynamic 归一化为 event。
            if btype not in ("event", "dynamic", "raw_slice", "slice"):
                continue
            created_str = meta.get("created", "")
            if not created_str:
                continue
            try:
                created = datetime.fromisoformat(str(created_str))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < since:
                    continue
            except (ValueError, TypeError):
                continue
            new_memories.append(b)

        if not new_memories:
            return {
                "date": today,
                "new_memories": 0,
                "topics": [],
                "conflict_proposals": 0,
                "sandbox_written": False,
            }

        # --- 2) Per new memory: search() top-K historical buckets & compare ---
        sandbox_entries = []
        conflict_proposals = 0
        topics_found = set()
        for nb in new_memories:
            content = nb["content"] or ""
            meta = nb["metadata"]
            query = (meta.get("name") or "") + " " + content[:120]
            try:
                results = await self.bucket_mgr.search(
                    query=query,
                    limit=self.incremental_match_top_k,
                    mask_tasks=True,
                )
            except Exception as e:
                logger.warning(f"Incremental dream search failed: {e}")
                results = []

            # Keep only HISTORICAL old buckets (created before the 24h window)
            old_candidates = []
            for r in results:
                if r.get("id") == nb["id"]:
                    continue
                rc = r.get("metadata", {}).get("created", "")
                try:
                    rcreated = datetime.fromisoformat(str(rc)) if rc else now
                    if rcreated.tzinfo is None:
                        rcreated = rcreated.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    rcreated = now
                if rcreated >= since:
                    continue
                old_candidates.append(r)

            for oc in old_candidates:
                oc_meta = oc.get("metadata", {})
                # --- 主题隔离优先：不同主题域禁止比对 ---
                if self._domain_isolated(meta, oc_meta):
                    continue
                cr = self._detect_conflict(content, oc.get("content", ""))
                if cr and conflict_proposals < self.cleanup_max_conflicts:
                    conflict_proposals += 1
                    await self.echo_chamber.add_pending_action(
                        action_type="conflict",
                        data={
                            "new_bucket_id": nb["id"],
                            "old_bucket_id": oc["id"],
                            "new_memory_id": nb["id"],
                            "old_memory_id": oc["id"],
                            "new_content": content[:200],
                            "old_content": (oc.get("content") or "")[:200],
                            "conflict_type": cr.get("type", ""),
                            "conflict_reason": cr.get("reason", ""),
                            "confidence_score": cr.get("confidence_score", 0.0),
                            "new_metadata": {
                                "created": meta.get("created", ""),
                                "name": meta.get("name", ""),
                            },
                            "old_metadata": {
                                "created": oc_meta.get("created", ""),
                                "name": oc_meta.get("name", ""),
                            },
                            "source": "daily_incremental_dream",
                        },
                    )
                    logger.info(f"Incremental dream conflict: {cr.get('reason')}")

            # --- Dream association note → sandbox ONLY (never permanent memory) ---
            sandbox_entries.append({
                "new_bucket_id": nb["id"],
                "name": meta.get("name", nb["id"]),
                "linked_old_buckets": [oc.get("id") for oc in old_candidates[:3]],
            })
            for d in as_list(meta.get("domain")):
                if d and str(d).strip():
                    topics_found.add(str(d).strip())

        # --- 3) Write associations to dream sandbox (temp file only) ---
        sandbox_payload = {
            "date": today,
            "created_at": now.isoformat(),
            "new_memories": len(new_memories),
            "topics": sorted(topics_found)[:20],
            "conflict_proposals": conflict_proposals,
            "entries": sandbox_entries,
        }
        written = ""
        try:
            written = await self.bucket_mgr.save_dream_sandbox(today, sandbox_payload)
        except Exception as e:
            logger.warning(f"Incremental dream sandbox write failed: {e}")

        # --- 4) Sync in-memory index with disk (proposals may have mutated status) ---
        # --- 内存索引热重载：本轮可能产生 superseded 标记/新提案，强制索引与硬盘一致 ---
        try:
            self.bucket_mgr.invalidate_index()
        except Exception as e:
            logger.warning(f"Incremental dream index reload failed: {e}")

        return {
            "date": today,
            "new_memories": len(new_memories),
            "topics": sorted(topics_found)[:20],
            "conflict_proposals": conflict_proposals,
            "sandbox_written": bool(written),
        }

    # =========================================================
    # Periodic global cleanup (周期大扫除) — every cleanup_interval_days
    # 仅每隔 cleanup_interval_days（默认 15 天）或收到显式指令时运行一次全量扫描：
    #   1. 深度遗忘衰减：长期未激活的普通记忆压缩为 faded_memory
    #   2. 近半月频次提炼：总结真正的 ring/（年轮认知桶）
    # =========================================================
    async def _should_run_full_cleanup(self, now: datetime | None = None) -> bool:
        """Whether the periodic full cleanup is due (cleanup_interval_days)."""
        now = now or datetime.now(timezone.utc)
        if self._last_full_cleanup is None:
            return False  # first run: skip auto-trigger to avoid surprises
        return (now - self._last_full_cleanup).total_seconds() >= self.cleanup_interval_days * 86400

    async def periodic_full_cleanup(self, force: bool = False) -> dict:
        """
        Periodic global cleanup (周期大扫除).
        Runs a full scan every cleanup_interval_days (default 15) or when
        explicitly instructed (force=True).

        - Deep decay: long-inactive ordinary memories → faded_memory (lightweight
          blurred impressions), marking the originals superseded.
        - Ring extraction: distills real ring/ (年轮认知桶) from recent half-month
          frequency; strictly separated from Voice (expression/habits) to avoid
          semantically overlapping buckets.
        """
        now = datetime.now(timezone.utc)
        if not force and not await self._should_run_full_cleanup(now):
            return {"run": False, "reason": "not_due"}

        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Full cleanup: list failed / 大扫除列表失败: {e}")
            return {"run": False, "error": str(e)}

        faded_created = 0
        # --- 1) Deep decay: low-activity ordinary memories → faded_memory ---
        for b in all_buckets:
            meta = b["metadata"]
            btype = meta.get("type", "dynamic")
            if btype in (
                "permanent", "feel", "identity", "ring", "voice", "boundary",
                "milestone", "ephemeral", "faded_memory", "anchor",
            ):
                continue
            if meta.get("pinned") or meta.get("protected"):
                continue
            if meta.get("superseded_by") or meta.get("status") == "superseded":
                continue
            importance = safe_int(meta.get("importance"), 5)
            activation = safe_int(meta.get("activation_count"), 0)
            if importance >= 6 or activation >= 3:
                continue
            last_str = meta.get("last_accessed", meta.get("created", ""))
            try:
                last = datetime.fromisoformat(str(last_str))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                days_since = max(0, (now - last).days)
            except (ValueError, TypeError):
                days_since = 999
            if days_since < 45:
                continue
            faded_label = (
                f"记得很久以前有过一件关于「{meta.get('name', '')}」的事，"
                f"具体细节已经模糊了。"
            )
            try:
                fid = await self.bucket_mgr.create_faded_memory(
                    original_bucket_id=b["id"],
                    faded_label=faded_label,
                    original_type=btype,
                    decay_score=min(1.0, days_since * 0.01),
                )
                if fid:
                    faded_created += 1
                    # --- 版本控制：原桶标记为 superseded 并指向模糊印象桶 ---
                    await self.bucket_mgr.update(
                        b["id"], status="superseded", superseded_by=fid
                    )
            except Exception as e:
                logger.warning(f"Full cleanup decay failed for {b['id']}: {e}")

        # --- 2) Ring extraction: recent half-month frequency → ring bucket ---
        ring_created = 0
        try:
            half_month = now - timedelta(days=15)
            from collections import Counter
            recent_pool = []
            domain_counter = Counter()
            for b in all_buckets:
                meta = b["metadata"]
                if meta.get("type") in (
                    "permanent", "feel", "voice", "ring", "identity",
                    "boundary", "milestone", "ephemeral", "faded_memory",
                ):
                    continue
                created_str = meta.get("created", "")
                if not created_str:
                    continue
                try:
                    created = datetime.fromisoformat(str(created_str))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created < half_month:
                        continue
                except (ValueError, TypeError):
                    continue
                recent_pool.append(b)
                for d in as_list(meta.get("domain")):
                    if d and str(d).strip():
                        domain_counter[str(d).strip()] += 1

            # Existing ring domains (avoid semantically overlapping rings)
            existing_ring_domains = set()
            try:
                for r in await self.bucket_mgr.list_all(include_archive=False):
                    if r["metadata"].get("type") == "ring":
                        for d in as_list(r["metadata"].get("domain")):
                            if d and str(d).strip():
                                existing_ring_domains.add(str(d).strip())
            except Exception:
                pass

            for domain, cnt in domain_counter.most_common():
                if cnt < 5 or domain in existing_ring_domains:
                    continue
                domain_buckets = [
                    b for b in recent_pool
                    if domain in {str(d).strip() for d in as_list(b["metadata"].get("domain"))}
                ]
                combined = "\n".join((b["content"] or "")[:200] for b in domain_buckets[:8])
                ring_content = (
                    f"近半个月围绕「{domain}」共积累了 {cnt} 条相关记忆。"
                    f"沉淀出的认知与原则：{combined[:300]}"
                )
                try:
                    await self.bucket_mgr.create(
                        content=ring_content,
                        tags=[f"ring:{domain}"],
                        importance=6,
                        domain=[domain],
                        name=f"年轮·{domain}",
                        bucket_type="ring",
                        provenance="system_event",
                    )
                    ring_created += 1
                    logger.info(f"Ring bucket created for domain: {domain} ({cnt} memories)")
                except Exception as e:
                    logger.warning(f"Full cleanup ring creation failed for {domain}: {e}")
        except Exception as e:
            logger.warning(f"Full cleanup ring extraction failed: {e}")

        # --- 3) Update state & physically clear stale dream sandbox files ---
        self._last_full_cleanup = now
        self._save_state()
        try:
            await self.bucket_mgr.purge_dream_sandbox(keep_today=True)
        except Exception as e:
            logger.warning(f"Full cleanup sandbox purge failed: {e}")

        # --- 4) Sync in-memory index with disk: superseded/delete/purge above ---
        # --- 内存索引热重载：本次大扫除产生了 superseded 标记、模糊印象与清空操作，
        #     强制 BM25 索引与硬盘桶状态保持一致，避免主 AI 查空 ---
        try:
            self.bucket_mgr.invalidate_index()
        except Exception as e:
            logger.warning(f"Full cleanup index reload failed: {e}")

        return {
            "run": True,
            "date": now.strftime("%Y-%m-%d"),
            "faded_memories_created": faded_created,
            "ring_buckets_created": ring_created,
            "interval_days": self.cleanup_interval_days,
        }

    async def _should_create_chain(self, topic: str) -> bool:
        """Check if a new chain should be created (topic mentioned across multiple days)."""
        chains = await self.get_event_chains()
        for chain in chains:
            if HAS_RAPIDFUZZ:
                if fuzz.ratio(chain.topic, topic) >= 60:
                    return False
            else:
                if topic in chain.topic or chain.topic in topic:
                    return False
        
        return True
    
    async def _append_temporary_node(self, chain: EventChain, bucket: dict):
        """Append a temporary node to event chain timeline."""
        timeline_entry = {
            "memory_id": bucket["id"],
            "timestamp": bucket["metadata"].get("created", ""),
            "content_preview": bucket["content"][:100],
            "temporary": True,
        }
        
        chain.timeline.append(timeline_entry)
        chain.timeline.sort(key=lambda x: x.get("timestamp", ""))
        chain.updated = datetime.now(timezone.utc).isoformat()

        # --- Update chain entities from the new node / 从新节点提取并更新实体 ---
        for e in self._extract_entities_from_bucket(bucket):
            if e not in chain.entities:
                chain.entities.append(e)
        
        await self._save_event_chain(chain)
        logger.debug(f"Added temporary node to chain: {chain.chain_id}")
    
    async def _weekly_chain_merge(self) -> dict:
        """Deduplicate and merge event chains — generates proposals only, no direct execution.
        去重并合并事件链 — 仅生成提案，不直接执行。"""
        chains = await self.get_event_chains()
        if not chains:
            return {"message": "No chains to merge"}

        proposals_created = 0

        for i, c1 in enumerate(chains):
            for c2 in chains[i+1:]:
                if HAS_RAPIDFUZZ:
                    similarity = fuzz.ratio(c1.topic, c2.topic)
                    if similarity >= 70:
                        # --- Create a merge proposal — main AI must approve before actual merge ---
                        # --- 创建合并提案 — 主 AI 批准后才执行合并 ---
                        await self.echo_chamber.add_pending_action(
                            action_type="chain_merge",
                            data={
                                "primary_chain_id": c1.chain_id,
                                "primary_topic": c1.topic,
                                "secondary_chain_id": c2.chain_id,
                                "secondary_topic": c2.topic,
                                "similarity": similarity,
                                "reason": f"主题相似度 {similarity}%: '{c1.topic}' ↔ '{c2.topic}'",
                            },
                        )
                        proposals_created += 1

        # --- Rebuild cross-chain entity graph / 重建事件链实体交叉索引 ---
        graph = await self._rebuild_chain_graph()

        return {"merge_proposals_created": proposals_created, **graph}

    async def _extract_pattern_proposals(self, max_chains: int = 3) -> dict:
        """Distill reusable experience rules from event chains -> pattern_proposal to echo chamber.
        从事件链提炼可复用经验 → 生成 pattern_proposal 提案到回音壁（主 AI 审批后写入 ring 年轮）。"""
        try:
            chains = await self.get_event_chains()
        except Exception as e:
            return {"error": str(e)}

        # Eligible: chain has summary + >=3 timeline nodes, newest first
        # 候选：有摘要且时间线 ≥3 条的链，按更新时间取最新
        candidates = [
            c for c in chains
            if (c.summary or "").strip() and len(c.timeline) >= 3
        ][:max_chains]
        if not candidates:
            return {"message": "No chains eligible for pattern extraction", "proposals_created": 0}

        dehy_cfg = (self.config or {}).get("dehydration", {}) or {}
        api_key = dehy_cfg.get("api_key", "") or os.environ.get("OMBRE_API_KEY", "")
        if not api_key:
            return {"skipped": True, "reason": "no_api_key", "proposals_created": 0}

        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=dehy_cfg.get("base_url", "https://api.deepseek.com/v1"),
            timeout=60.0,
        )

        proposals_created = 0
        for chain in candidates:
            try:
                rules = await self._llm_extract_rules(client, chain, dehy_cfg)
                if not rules:
                    continue
                await self.echo_chamber.add_pending_action(
                    action_type="pattern_proposal",
                    data={
                        "chain_id": chain.chain_id,
                        "chain_topic": chain.topic,
                        "rules": rules,
                        "reason": f"从事件链「{chain.topic}」提炼的可复用经验（{len(chain.timeline)} 条事件）",
                    },
                )
                proposals_created += 1
            except Exception as e:
                logger.error(f"Pattern extraction failed for chain {chain.chain_id}: {e}")

        return {"proposals_created": proposals_created, "chains_analyzed": len(candidates)}

    async def _llm_extract_rules(self, client, chain, dehy_cfg: dict) -> list:
        """Call LLM to distill reusable rules from an event chain.
        调用 LLM 从事件链提炼可复用经验规则。"""
        prompt = (
            "你是一个经验提炼专家。根据以下事件链（主题、摘要、涉及实体、时间线），"
            "提炼 1-3 条可复用的人生经验/行为准则/规律。\n"
            "要求：\n"
            "1. 每条经验一句话，具体可操作，不空泛\n"
            "2. 输出纯 JSON 数组，每项含 rule（经验内容）和 domain（所属领域："
            "工作/学习/生活/人际关系/健康/情绪/兴趣/成长）\n"
            f"主题：{chain.topic}\n"
            f"摘要：{chain.summary}\n"
            f"涉及实体：{json.dumps(chain.entities, ensure_ascii=False)}\n"
            f"近期事件：{json.dumps([t.get('content_preview', '') for t in chain.timeline][-5:], ensure_ascii=False)}"
        )
        try:
            resp = await client.chat.completions.create(
                model=dehy_cfg.get("model", "deepseek-chat"),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=512,
                temperature=0.3,
            )
            raw = (resp.choices[0].message.content or "").strip()
            rules = safe_json_loads(raw, default=None)
            if rules is None:
                return []
            if isinstance(rules, dict):
                rules = [rules]
            if not isinstance(rules, list):
                return []
            return [
                r for r in rules
                if isinstance(r, dict) and (r.get("rule") or "").strip()
            ][:3]
        except Exception as e:
            logger.error(f"LLM rule extraction failed: {e}")
            return []
    
    async def _weekly_cleanup_scan(self) -> dict:
        """Scan for stale low-weight memories and generate cleanup proposals."""
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return {"error": str(e)}
        
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        proposals_created = 0
        
        for b in all_buckets:
            meta = b["metadata"]
            
            if meta.get("type") in ("permanent", "feel") or meta.get("pinned") or meta.get("protected"):
                continue
            
            if meta.get("resolved", False):
                continue
            
            last_accessed_str = meta.get("last_accessed", meta.get("created", ""))
            try:
                last_accessed = datetime.fromisoformat(str(last_accessed_str))
                if last_accessed.tzinfo is None:
                    last_accessed = last_accessed.replace(tzinfo=timezone.utc)
                if last_accessed < thirty_days_ago:
                    if await self._should_propose_cleanup(b):
                        await self._create_cleanup_proposal_echo(b)
                        proposals_created += 1
            except (ValueError, TypeError):
                continue
        
        return {"proposals_created": proposals_created}
    
    async def _should_propose_cleanup(self, bucket: dict) -> bool:
        """Determine if a bucket should be proposed for cleanup."""
        meta = bucket["metadata"]
        
        importance = meta.get("importance", 5)
        if importance >= 7:
            return False
        
        activation_count = meta.get("activation_count", 0)
        if activation_count >= 3:
            return False
        
        related_buckets = meta.get("related_buckets", [])
        if related_buckets:
            return False
        
        return True
    
    async def _create_cleanup_proposal_echo(self, bucket: dict):
        """Create cleanup proposal via echo chamber."""
        meta = bucket["metadata"]
        
        reasons = []
        importance = meta.get("importance", 5)
        if importance < 5:
            reasons.append(f"低重要度({importance})")
        
        activation_count = meta.get("activation_count", 0)
        if activation_count == 0:
            reasons.append("从未被激活")
        
        last_accessed_str = meta.get("last_accessed", meta.get("created", ""))
        try:
            last_accessed = datetime.fromisoformat(str(last_accessed_str))
            if last_accessed.tzinfo is None:
                last_accessed = last_accessed.replace(tzinfo=timezone.utc)
            days_since = (datetime.now(timezone.utc) - last_accessed).days
            reasons.append(f"{days_since}天未访问")
        except (ValueError, TypeError):
            reasons.append("访问时间未知")
        
        await self.echo_chamber.add_pending_action(
            action_type="cleanup",
            data={
                "bucket_id": bucket["id"],
                "reason": ", ".join(reasons),
                "bucket_info": {
                    "name": meta.get("name", bucket["id"]),
                    "domain": meta.get("domain", []),
                    "importance": importance,
                    "created": meta.get("created", ""),
                    "last_accessed": last_accessed_str,
                }
            }
        )
    
    async def _write_weekly_digest(self, results: dict):
        """Write weekly digest to echo chamber, summarizing past 7 days of daily digests."""
        
        # --- Read past 7 days of daily digests ---
        # --- 读取过去7天的每日摘要 ---
        past_7_daily = []
        cut_off = datetime.now(timezone.utc) - timedelta(days=7)
        
        try:
            for filename in os.listdir(self.echo_chamber.digests_dir):
                if not filename.startswith("daily_") or not filename.endswith(".json"):
                    continue
                file_path = os.path.join(self.echo_chamber.digests_dir, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    created = datetime.fromisoformat(data.get("created", ""))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created >= cut_off:
                        past_7_daily.append(data)
                except Exception as e:
                    logger.warning(f"Failed to load daily digest {filename}: {e}")
        except Exception as e:
            logger.warning(f"Failed to list daily digests: {e}")
        
        past_7_daily.sort(key=lambda d: d.get("created", ""))
        
        # --- Build weekly summary from daily digests ---
        # --- 基于每日摘要构建每周总结 ---
        digest_content = ""
        
        if past_7_daily and self.dehydrator and self.dehydrator.client:
            try:
                daily_summaries_text = ""
                for d in past_7_daily:
                    date_str = d.get("created", "")[:10]
                    mood = d.get("metadata", {}).get("mood_tags", [])
                    count = d.get("metadata", {}).get("bucket_count", 0)
                    content_preview = d.get("content", "")[:300]
                    daily_summaries_text += f"\n--- {date_str} (情绪: {mood}, {count}条记忆) ---\n{content_preview}\n"
                
                prompt = f"""你是一个AI记忆管家。请根据过去7天的每日摘要，生成一份有深度的每周总结报告。

要求：
1. 用中文，语气温和亲切，像朋友在回顾一周
2. 梳理本周的关键事件：哪些事很重要、哪些有进展、哪些还没做完
3. 分析情绪变化轨迹：这周整体心情如何，有什么波动
4. 列出需要持续关注的事项和待办
5. 如果发现某些反复出现的主题或模式，点出来
6. 200-400字

过去7天每日摘要：
{daily_summaries_text[:4000]}"""
                
                response = await self.dehydrator.client.chat.completions.create(
                    model=self.dehydrator.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=600,
                )
                
                ai_summary = response.choices[0].message.content.strip()
                
                # Append merge/cleanup stats
                stats = ""
                chains_merged = results.get('chain_merge', {}).get('chains_merged', 0)
                proposals = results.get('cleanup_scan', {}).get('proposals_created', 0)
                if chains_merged or proposals:
                    stats = f"\n\n【管家操作】\n"
                    if chains_merged:
                        stats += f"- 合并了 {chains_merged} 条事件链\n"
                    if proposals:
                        stats += f"- 生成了 {proposals} 条清理提案（待审阅）\n"
                
                digest_content = f"{ai_summary}{stats}"
                logger.info("Weekly digest generated by AI from daily summaries")
            except Exception as e:
                logger.error(f"AI weekly digest failed: {e}")
                digest_content = f"AI摘要生成失败，回退到模板模式"
        
        # Fallback: template-based digest (no AI or no daily digests)
        if not digest_content:
            digest_content = f"""每周管家报告

【本周概况】
过去7天共有 {len(past_7_daily)} 天的每日摘要记录。

【事件链合并】
合并了 {results.get('chain_merge', {}).get('chains_merged', 0)} 条事件链

【清理提案】
生成了 {results.get('cleanup_scan', {}).get('proposals_created', 0)} 条清理提案

请审阅并执行必要的批准操作。"""
        
        await self.echo_chamber.write_digest(
            digest_type="weekly",
            content=digest_content,
            metadata={
                "daily_digests_used": len(past_7_daily),
                "chain_merge": results.get('chain_merge', {}),
                "cleanup_scan": results.get('cleanup_scan', {}),
            }
        )
    
    async def _extract_topics(self, buckets: list) -> dict:
        """Extract topics from buckets by finding semantically similar content."""
        if not HAS_RAPIDFUZZ:
            return self._extract_topics_simple(buckets)
        
        topic_groups = {}
        
        for i, b1 in enumerate(buckets):
            content1 = b1["content"]
            assigned = False
            
            for topic, group in topic_groups.items():
                group_contents = [b["content"] for b in group]
                if group_contents:
                    best_match, score, _ = process.extractOne(content1, group_contents, scorer=fuzz.ratio)
                    if score >= 50:
                        topic_groups[topic].append(b1)
                        assigned = True
                        break
            
            if not assigned:
                topic = await self._generate_topic_name(content1)
                topic_groups[topic] = [b1]
        
        return topic_groups
    
    def _extract_topics_simple(self, buckets: list) -> dict:
        """Simple topic extraction without rapidfuzz."""
        topic_groups = {}
        
        for bucket in buckets:
            content = bucket["content"]
            tags = bucket["metadata"].get("tags", [])
            domain = bucket["metadata"].get("domain", [])
            
            topic_parts = []
            if domain:
                topic_parts.extend(domain)
            if tags:
                topic_parts.extend(tags[:3])
            
            if not topic_parts:
                topic_parts.append(content[:20].replace("\n", " "))
            
            topic = " | ".join(topic_parts)
            
            if topic not in topic_groups:
                topic_groups[topic] = []
            topic_groups[topic].append(bucket)
        
        return topic_groups
    
    async def _generate_topic_name(self, content: str) -> str:
        """Generate a concise topic name from content using AI."""
        if self.dehydrator and self.dehydrator.client:
            try:
                prompt = f"""你是一个AI记忆管家。请为以下记忆内容生成一个简短、准确的主题名称。

要求：
1. 6-12个字
2. 要具体到实际内容，不要用"日常记录""今日事件"等笼统词
3. 包含关键人物、事件或主题

记忆内容：
{content[:500]}

主题名称："""
                
                response = await self.dehydrator.client.chat.completions.create(
                    model=self.dehydrator.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=30,
                )
                
                topic = response.choices[0].message.content.strip()
                return topic[:20] if topic else content[:30].replace("\n", " ")
            except Exception as e:
                logger.error(f"AI topic generation failed: {e}")
        
        keywords = []
        symptom_patterns = [
            r'(肚子痛|胃痛|腹痛|痛经|头痛|头晕|发烧|感冒)',
            r'(生病|身体不适|不舒服|难受)',
            r'(加班|工作|学习)',
            r'(开心|难过|生气|焦虑)',
        ]
        
        import re
        for pattern in symptom_patterns:
            matches = re.findall(pattern, content)
            if matches:
                keywords.extend(matches)
        
        if keywords:
            return keywords[0]
        
        return content[:30].replace("\n", " ")
    
    async def _find_existing_chain(self, topic: str) -> EventChain | None:
        """Find an existing event chain by topic."""
        chains = await self.get_event_chains()
        for chain in chains:
            if HAS_RAPIDFUZZ:
                if fuzz.ratio(chain.topic, topic) >= 70:
                    return chain
            else:
                if topic in chain.topic or chain.topic in topic:
                    return chain
        return None
    
    async def _create_event_chain(self, topic: str, buckets: list) -> bool:
        """Create a new event chain."""
        chain_id = self._generate_id()
        
        timeline = []
        for b in sorted(buckets, key=lambda x: x["metadata"].get("created", "")):
            timeline.append({
                "memory_id": b["id"],
                "timestamp": b["metadata"].get("created", ""),
                "content_preview": b["content"][:100],
            })
        
        summary = await self._generate_chain_summary(topic, buckets)
        
        chain = EventChain(chain_id, topic)
        chain.timeline = timeline
        chain.summary = summary
        chain.source_bucket_ids = [b["id"] for b in buckets]
        # --- Seed entity graph from buckets / 从记忆桶提取并注入实体 ---
        chain.entities = list(dict.fromkeys(
            e for b in buckets for e in self._extract_entities_from_bucket(b)
        ))

        await self._save_event_chain(chain)
        logger.info(f"Created event chain: {chain_id} - {topic}")
        return True

    # ---------------------------------------------------------
    # Entity extraction & cross-chain graph / 实体提取与图谱交叉索引
    # ---------------------------------------------------------
    _ENTITY_STOPWORDS = (
        "我", "你", "他", "她", "我们", "你们", "他们", "她们",
        "这个", "那个", "今天", "昨天", "明天", "自己",
    )

    def _extract_entities_from_text(self, text: str) -> list[str]:
        """Lightweight entity extraction (persons via regex) from text.
        轻量级实体提取：从文本中用正则抽取人物实体（无需 LLM）。"""
        import re
        if not text:
            return []
        entities = set()
        text = text[:5000]
        person_patterns = [
            r'(?:跟|和|与)([\u4e00-\u9fff]{2,4}?)(?:一起|说|聊|去|吃|玩|见面|打电话|约)',
            r'([\u4e00-\u9fff]{2,4}?)(?:说|告诉|给我|让我|帮我|找我|叫我|生气|开心|难过)',
            r'(妈妈|爸爸|奶奶|爷爷|外公|外婆|舅舅|阿姨|叔叔|老师|同学|室友|朋友|同事|闺蜜|男朋友|女朋友)',
        ]
        for pattern in person_patterns:
            for m in re.findall(pattern, text):
                if isinstance(m, tuple):
                    m = m[0]
                if m and m not in self._ENTITY_STOPWORDS:
                    entities.add(m)
        return list(entities)

    def _extract_entities_from_bucket(self, bucket: dict) -> list[str]:
        """Extract entities from a bucket: persons from content + tags + [[wikilinks]].
        从记忆桶提取实体：正文人物 + 标签 + 维基链接。"""
        import re
        entities = set()
        content = bucket.get("content", "") or ""
        entities.update(self._extract_entities_from_text(content))
        meta = bucket.get("metadata", {}) or {}
        for tag in meta.get("tags", []) or []:
            if tag and isinstance(tag, str) and tag.strip():
                entities.add(tag.strip())
        for m in re.findall(r'\[\[([^\]]+)\]\]', content):
            name = m.split("|")[0].strip()
            if name:
                entities.add(name)
        return list(entities)

    async def _rebuild_chain_graph(self) -> dict:
        """Build cross-chain entity graph: chains sharing entities become related.
        Scans all active chains and mutually links related_chain_ids with dedup.
        构建事件链实体图谱：扫描所有活跃链，共享实体的链条互相建立关联（去重）。"""
        chains = await self.get_event_chains()
        if len(chains) < 2:
            return {"chains": len(chains), "links": 0}

        by_id = {c.chain_id: c for c in chains}
        changed_ids = set()
        links = 0
        seen_pairs = set()

        for i, c1 in enumerate(chains):
            e1 = {e for e in c1.entities if e}
            if not e1:
                continue
            for c2 in chains[i + 1:]:
                pair = (c1.chain_id, c2.chain_id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                e2 = {e for e in c2.entities if e}
                if not e2:
                    continue
                if e1 & e2:
                    if c2.chain_id not in c1.related_chain_ids:
                        c1.related_chain_ids.append(c2.chain_id)
                        changed_ids.add(c1.chain_id)
                    if c1.chain_id not in c2.related_chain_ids:
                        c2.related_chain_ids.append(c1.chain_id)
                        changed_ids.add(c2.chain_id)
                    links += 1

        # --- Persist only changed chains / 仅持久化发生变化的链 ---
        for cid in changed_ids:
            try:
                await self._save_event_chain(by_id[cid])
            except Exception as e:
                logger.warning(f"Failed to persist graph update for chain {cid}: {e}")

        if links:
            logger.info(f"Cross-chain graph rebuilt: {links} links among {len(chains)} chains")
        return {"chains": len(chains), "links": links}

    async def _update_event_chain(self, chain: EventChain, buckets: list) -> bool:
        """Update an existing event chain with new buckets."""
        new_bucket_ids = [b["id"] for b in buckets]
        existing_ids = set(chain.source_bucket_ids)
        
        new_buckets = [b for b in buckets if b["id"] not in existing_ids]
        if not new_buckets:
            return False
        
        for b in sorted(new_buckets, key=lambda x: x["metadata"].get("created", "")):
            chain.timeline.append({
                "memory_id": b["id"],
                "timestamp": b["metadata"].get("created", ""),
                "content_preview": b["content"][:100],
            })
        
        chain.timeline.sort(key=lambda x: x["timestamp"])
        chain.source_bucket_ids.extend(new_bucket_ids)
        chain.summary = await self._generate_chain_summary(chain.topic, buckets)
        chain.updated = datetime.now(timezone.utc).isoformat()
        
        await self._save_event_chain(chain)
        logger.info(f"Updated event chain: {chain.chain_id} - {chain.topic}")
        return True
    
    async def _generate_chain_summary(self, topic: str, buckets: list) -> str:
        """Generate a summary for the event chain using AI."""
        if not buckets:
            return ""
        
        if self.dehydrator and self.dehydrator.client:
            try:
                bucket_contents = "\n".join([f"- {b['content'][:200]}" for b in buckets[:20]])
                
                prompt = f"""你是一个AI记忆管家。请为以下事件链生成一个有信息量的摘要。

事件主题：{topic}

相关记忆内容：
{bucket_contents}

要求：
1. 用中文，语言流畅
2. 概括事件背景、关键进展和当前状态
3. 突出重要的变化节点和决策
4. 80-150字
5. 不要简单罗列条目，要整合成连贯叙述"""
                
                response = await self.dehydrator.client.chat.completions.create(
                    model=self.dehydrator.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=300,
                )

                return response.choices[0].message.content.strip()[:400]
            except Exception as e:
                logger.error(f"AI chain summary failed: {e}")
        
        dates = []
        for b in buckets:
            created = b["metadata"].get("created", "")
            if created:
                dates.append(created[:10])
        
        if dates:
            date_range = f"{min(dates)} ~ {max(dates)}"
        else:
            date_range = "最近"
        
        return f"{date_range} - {topic} 相关事件（共{len(buckets)}条）"
    
    async def _save_event_chain(self, chain: EventChain):
        """Save event chain to disk."""
        file_path = os.path.join(self.event_chains_dir, f"{chain.chain_id}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(chain.to_dict(), f, ensure_ascii=False, indent=2)
    
    async def get_event_chains(self) -> list[EventChain]:
        """Get all saved event chains."""
        chains = []
        for filename in os.listdir(self.event_chains_dir):
            if filename.endswith(".json"):
                file_path = os.path.join(self.event_chains_dir, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        chains.append(EventChain.from_dict(data))
                except Exception as e:
                    logger.warning(f"Failed to load event chain: {file_path}: {e}")
        
        chains.sort(key=lambda c: c.updated, reverse=True)
        return chains
    
    async def get_event_chain(self, chain_id: str) -> EventChain | None:
        """Get a specific event chain by ID."""
        file_path = os.path.join(self.event_chains_dir, f"{chain_id}.json")
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return EventChain.from_dict(data)
        return None
    
    async def approve_chain(self, chain_id: str) -> bool:
        """Approve an event chain - mark it as finalized."""
        chain = await self.get_event_chain(chain_id)
        if not chain:
            return False
        
        chain.status = "resolved"
        chain.updated = datetime.now(timezone.utc).isoformat()
        await self._save_event_chain(chain)
        logger.info(f"Approved event chain: {chain_id}")
        return True
    
    async def get_cleanup_proposals(self, status: str = "pending") -> list[CleanupProposal]:
        """Get cleanup proposals by status (legacy method)."""
        actions = await self.echo_chamber.get_pending_actions(action_type="cleanup")
        proposals = []
        for action in actions:
            data = action.get("data", {})
            proposal = CleanupProposal(
                action["action_id"],
                data.get("bucket_id", ""),
                data.get("reason", "")
            )
            proposal.bucket_info = data.get("bucket_info", {})
            proposals.append(proposal)
        return proposals
    
    async def update_cleanup_proposal(self, proposal_id: str, status: str):
        """Update cleanup proposal status (legacy method)."""
        return await self.echo_chamber.update_action_status(proposal_id, status)
    
    async def reject_proposal(self, proposal_id: str) -> bool:
        """Reject a cleanup proposal."""
        return await self.echo_chamber.update_action_status(proposal_id, "rejected")
    
    async def approve_proposal(self, proposal_id: str) -> bool:
        """Approve a cleanup proposal."""
        return await self.echo_chamber.update_action_status(proposal_id, "approved")

    async def execute_change_proposal(self, proposal_id: str) -> tuple:
        """
        Execute an approved change/organize proposal (P0-2).
        AI 管家修改候选的执行入口：只执行已确认（pending→executed）的提案。
        Returns (success, message).
        执行已确认的修改/整理提案。返回 (是否成功, 消息)。
        """
        file_path = os.path.join(self.echo_chamber.pending_actions_dir, f"{proposal_id}.json")
        if not os.path.exists(file_path):
            return False, f"未找到提案: {proposal_id}"

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return False, f"读取提案失败: {e}"

        if data.get("action_type") not in ("change", "organize", "enrich"):
            return False, f"提案 {proposal_id} 不是修改提案（类型: {data.get('action_type')}）。"
        if data.get("status") != "pending":
            return False, f"提案 {proposal_id} 状态为 {data.get('status')}，无法执行。"
        # --- Stale proposal 防护：过期提案禁止执行 ---
        if self.echo_chamber._is_proposal_expired(data):
            return False, f"提案 {proposal_id} 已过期（超过 {PROPOSAL_TTL_DAYS} 天未审批），禁止执行。请重新生成提案。"

        action_data = data.get("data", {}) or {}

        try:
            # --- Organize proposal: batch importance drop / 整理提案：批量降权 ---
            if data.get("action_type") == "organize":
                candidates = action_data.get("candidates", []) or []
                importance_drop = action_data.get("importance_drop", 2)
                executed = 0
                for c in candidates:
                    bid = c.get("bucket_id", "")
                    current = c.get("current_importance", 5)
                    new_importance = max(1, current - importance_drop)
                    try:
                        ok = await self.bucket_mgr.update(bid, importance=new_importance)
                        if ok:
                            executed += 1
                    except Exception as e:
                        logger.warning(f"execute_change_proposal organize failed for {bid}: {e}")
                await self.echo_chamber.update_action_status(proposal_id, "executed")
                return True, f"已执行整理提案 {proposal_id}: {executed}/{len(candidates)} 条已降权"

            # --- Change proposal: single bucket update/delete / 修改提案：单桶更新/删除 ---
            bucket_id = action_data.get("bucket_id", "")
            updates = action_data.get("updates", {}) or {}
            delete = action_data.get("delete", False)

            if delete:
                success = await self.bucket_mgr.delete(bucket_id)
                if not success:
                    return False, f"执行失败: 未找到记忆桶 {bucket_id}"
                if self.embedding_engine is not None:
                    try:
                        self.embedding_engine.delete_embedding(bucket_id)
                    except Exception as e:
                        logger.warning(f"Failed to delete embedding after apply_change: {e}")
            else:
                if not updates:
                    return False, "提案没有需要执行的修改。"
                success = await self.bucket_mgr.update(bucket_id, **updates)
                if not success:
                    return False, f"执行失败: {bucket_id}"
                if "content" in updates and self.embedding_engine is not None:
                    try:
                        await self.embedding_engine.generate_and_store(bucket_id, updates["content"])
                    except Exception as e:
                        logger.warning(f"Failed to store embedding after apply_change: {e}")

            await self.echo_chamber.update_action_status(proposal_id, "executed")
            return True, f"已执行修改提案 {proposal_id}: {bucket_id}"
        except Exception as e:
            logger.error(f"execute_change_proposal failed: {e}")
            return False, f"执行失败: {e}"

    async def propose_metadata_enrichment(self, limit: int = 20) -> dict:
        """
        Generate enrichment proposals for legacy memories (旧记忆 metadata 补全提案).

        扫描 memory_class 缺失的旧记忆，基于现有 type 推断建议的 memory_class，
        生成 enrich 提案供审批。不直接修改任何记忆（只生成提案）。
        返回 {"enrichment_proposals_created": N, "scanned": M}。
        """
        all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        candidates = []
        for b in all_buckets:
            meta = b["metadata"]
            if not meta.get("memory_class"):
                candidates.append(b)

        # 优先处理最旧的记忆（created 升序）
        candidates.sort(key=lambda b: b["metadata"].get("created", ""))
        candidates = candidates[:limit]

        proposals_created = 0
        for b in candidates:
            meta = b["metadata"]
            suggested_class = _infer_memory_class(meta.get("type", ""))
            if not suggested_class:
                continue
            await self.echo_chamber.add_pending_action(
                "enrich",
                {
                    "bucket_id": b["id"],
                    "updates": {"memory_class": suggested_class},
                    "reason": (
                        f"旧记忆 metadata 补全：type={meta.get('type', '')} "
                        f"→ memory_class={suggested_class}"
                    ),
                },
                proposed_by="ai_manage",
            )
            proposals_created += 1

        logger.info(
            f"Metadata enrichment proposals created: {proposals_created}/{len(candidates)} "
            f"(scanned {len(candidates)} legacy memories)"
        )
        return {
            "enrichment_proposals_created": proposals_created,
            "scanned": len(candidates),
        }

    async def review_digest(self) -> dict:
        """Get digest for main AI review."""
        return await self.echo_chamber.get_review_summary()
    
    async def approve_action(self, action_id: str, approved_by: str = "user") -> bool:
        """Approve a pending action and execute it.
        批准回音壁提案并自动执行落地（清理/冲突/合并/身份）。
        approved_by 记录审批者（user / main_ai），为两级权限审批留接口（修正 6）。"""
        file_path = os.path.join(self.echo_chamber.pending_actions_dir, f"{action_id}.json")
        if not os.path.exists(file_path):
            return False

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load action {action_id}: {e}")
            return False

        if data.get("status") != "pending":
            return False

        # --- Stale proposal 防护：过期提案禁止批准执行 ---
        if self.echo_chamber._is_proposal_expired(data):
            logger.warning(
                f"approve_action rejected stale proposal {action_id} "
                f"(超过 {PROPOSAL_TTL_DAYS} 天未审批)"
            )
            return False

        action_type = data.get("action_type", "")
        action_data = data.get("data", {}) or {}

        # Execute the action based on its type — only mark approved if execution succeeded
        try:
            if action_type in ("change", "organize", "enrich"):
                # --- Delegate to execute_change_proposal (P0-2 / 修正 6) ---
                # --- 委托给 execute_change_proposal 执行修改/整理/补全提案 ---
                # change/organize/enrich 提案由 execute_change_proposal 统一执行（含幂等保护），
                # approve_action 只负责审批记录（approved_by/approved_at）。
                success, message = await self.execute_change_proposal(action_id)
                if not success:
                    raise RuntimeError(message)

            elif action_type == "cleanup":
                bucket_id = action_data.get("bucket_id", "")
                if bucket_id:
                    ok = await self.bucket_mgr.delete(bucket_id)
                    if not ok:
                        raise RuntimeError(f"bucket {bucket_id} 删除失败")
                    # --- Sync-clean vector store / 同步清理向量库 ---
                    if self.embedding_engine is not None:
                        try:
                            self.embedding_engine.delete_embedding(bucket_id)
                        except Exception as e:
                            logger.warning(f"Failed to delete embedding for {bucket_id}: {e}")
                    logger.info(f"Executed cleanup: deleted bucket {bucket_id}")

            elif action_type == "conflict":
                old_id = action_data.get("old_memory_id") or action_data.get("old_bucket_id", "")
                new_id = action_data.get("new_memory_id") or action_data.get("new_bucket_id", "")
                if old_id:
                    # --- Mark old memory resolved + superseded, completing the replacement ---
                    # --- 将旧记忆标记为已解决并被新记忆取代，完成新旧替换（版本控制） ---
                    update_kwargs = {"resolved": True, "status": "superseded"}
                    if new_id:
                        update_kwargs["superseded_by"] = new_id
                    ok = await self.bucket_mgr.update(old_id, **update_kwargs)
                    if not ok:
                        raise RuntimeError(f"bucket {old_id} 冲突标记失败")
                    logger.info(f"Executed conflict resolution: marked {old_id} as resolved")

            elif action_type == "chain_merge":
                # --- Merge secondary chain into primary, then delete secondary ---
                # --- 将次要链合并到主链，然后删除次要链 ---
                primary_id = action_data.get("primary_chain_id", "")
                secondary_id = action_data.get("secondary_chain_id", "")
                if primary_id and secondary_id:
                    primary = await self.get_event_chain(primary_id)
                    secondary = await self.get_event_chain(secondary_id)
                    if primary and secondary:
                        # --- Deduplicate timeline entries by memory_id / 按 memory_id 去重时间线 ---
                        seen = {e.get("memory_id", "") for e in primary.timeline if e.get("memory_id")}
                        merged_timeline = list(primary.timeline)
                        for entry in secondary.timeline:
                            mid = entry.get("memory_id", "")
                            if mid and mid in seen:
                                continue
                            if mid:
                                seen.add(mid)
                            merged_timeline.append(entry)
                        primary.timeline = merged_timeline
                        primary.timeline.sort(key=lambda x: x.get("timestamp", ""))

                        # --- Deduplicate source bucket ids / 去重来源桶 ID ---
                        primary.source_bucket_ids = list(dict.fromkeys(
                            list(primary.source_bucket_ids) + list(secondary.source_bucket_ids)
                        ))

                        # --- Rebuild summary: primary summary + secondary topic ---
                        # --- 摘要重构：主链摘要 + 次链主题 ---
                        primary.summary = (primary.summary or "") + (
                            f"\n[合并自 {secondary.topic}]"
                            if secondary.topic and secondary.topic not in (primary.summary or "")
                            else ""
                        ).strip()
                        # --- Merge entity sets (dedup) / 合并实体集合（去重）---
                        for e in secondary.entities:
                            if e and e not in primary.entities:
                                primary.entities.append(e)
                        primary.updated = datetime.now(timezone.utc).isoformat()
                        await self._save_event_chain(primary)

                        chain_file = os.path.join(self.event_chains_dir, f"{secondary_id}.json")
                        if os.path.exists(chain_file):
                            os.remove(chain_file)
                        logger.info(f"Executed chain_merge: merged {secondary_id} into {primary_id}")

            elif action_type == "merge":
                # --- Merge duplicate memory buckets: fold secondary into primary, then delete secondary ---
                # --- 合并重复记忆桶：将次桶内容并入主桶，然后删除次桶 ---
                primary_id = action_data.get("primary_bucket_id", "")
                secondary_id = action_data.get("secondary_bucket_id", "")
                if primary_id and secondary_id and primary_id != secondary_id:
                    primary = await self.bucket_mgr.get(primary_id)
                    secondary = await self.bucket_mgr.get(secondary_id)
                    if primary and secondary:
                        # --- Merge content: append secondary content if not already present ---
                        # --- 合并正文：若主桶未包含次桶内容则追加 ---
                        p_content = primary.get("content", "") or ""
                        s_content = secondary.get("content", "") or ""
                        if s_content and s_content not in p_content:
                            merged_content = (p_content + "\n\n" + s_content).strip()
                        else:
                            merged_content = p_content
                        # --- Merge tags (dedup) / 合并标签（去重） ---
                        p_tags = list(primary.get("metadata", {}).get("tags", []) or [])
                        s_tags = list(secondary.get("metadata", {}).get("tags", []) or [])
                        merged_tags = list(dict.fromkeys(p_tags + s_tags))
                        # --- Merge domain (dedup) / 合并主题域（去重） ---
                        p_domain = list(primary.get("metadata", {}).get("domain", []) or [])
                        s_domain = list(secondary.get("metadata", {}).get("domain", []) or [])
                        merged_domain = list(dict.fromkeys(p_domain + s_domain))
                        # --- Keep higher importance / 保留更高重要度 ---
                        p_imp = safe_int(primary.get("metadata", {}).get("importance", 5), 5)
                        s_imp = safe_int(secondary.get("metadata", {}).get("importance", 5), 5)
                        merged_imp = max(p_imp, s_imp)

                        update_kwargs = {
                            "content": merged_content,
                            "tags": merged_tags,
                            "domain": merged_domain,
                            "importance": merged_imp,
                        }
                        ok = await self.bucket_mgr.update(primary_id, **update_kwargs)
                        if not ok:
                            raise RuntimeError(f"bucket {primary_id} 合并更新失败")
                        # --- Re-embed merged content / 重新向量化合并后的正文 ---
                        if self.embedding_engine is not None:
                            try:
                                await self.embedding_engine.generate_and_store(primary_id, merged_content)
                            except Exception as e:
                                logger.warning(f"Failed to re-embed merged bucket {primary_id}: {e}")
                        # --- Delete secondary bucket + its embedding ---
                        del_ok = await self.bucket_mgr.delete(secondary_id)
                        if not del_ok:
                            raise RuntimeError(f"bucket {secondary_id} 删除失败")
                        if self.embedding_engine is not None:
                            try:
                                self.embedding_engine.delete_embedding(secondary_id)
                            except Exception as e:
                                logger.warning(f"Failed to delete embedding for {secondary_id}: {e}")
                        logger.info(f"Executed merge: merged {secondary_id} into {primary_id}")

            elif action_type == "identity_proposal":
                # --- Create an identity profile in identity/ layer / 在身份层创建人物档案 ---
                person_name = action_data.get("person_name", "")
                if person_name:
                    dominant_emotion = action_data.get("dominant_emotion", "")
                    sample_context = action_data.get("sample_context", "")
                    if self.identity_mgr is not None:
                        identity_id = await self.identity_mgr.create(
                            name=person_name,
                            aliases=[],
                            core_traits=[dominant_emotion] if dominant_emotion else [],
                            relationships=[],
                            content=f"（由管家提案自动生成）\n{sample_context}" if sample_context else "（由管家提案自动生成）",
                        )
                        logger.info(f"Executed identity_proposal: created identity {identity_id} for {person_name}")
                    else:
                        # --- Fallback: create as permanent bucket (legacy path) ---
                        # --- 兜底：以永久桶方式创建（旧路径）---
                        await self.bucket_mgr.create(
                            content=f"人物档案：{person_name}（由管家提案自动生成，请主AI补充详细信息）",
                            tags=["identity", "auto-proposed"],
                            importance=5,
                            domain=["身份"],
                            name=person_name,
                            bucket_type="permanent",
                            provenance="system_event",
                        )
                        logger.info(f"Executed identity_proposal (fallback): created identity for {person_name}")

            elif action_type == "optimize":
                # --- Apply approved tag / importance / type adjustments ---
                # --- 应用已批准的标签 / 权重 / 类型调整 ---
                bucket_id = action_data.get("bucket_id", "")
                suggestions = action_data.get("suggestions", []) or []
                if bucket_id:
                    update_kwargs = {}
                    for s in suggestions:
                        field = s.get("field", "")
                        suggested = s.get("suggested")
                        if field == "primary_tags":
                            if isinstance(suggested, list):
                                update_kwargs["primary_tags"] = [str(x) for x in suggested]
                        elif field == "sub_tags":
                            if isinstance(suggested, list):
                                update_kwargs["sub_tags"] = [str(x) for x in suggested]
                        elif field == "tags":
                            if isinstance(suggested, list):
                                update_kwargs["tags"] = [str(x) for x in suggested]
                        elif field == "importance":
                            try:
                                update_kwargs["importance"] = max(1, min(10, int(suggested)))
                            except (TypeError, ValueError):
                                continue
                        elif field == "type":
                            update_kwargs["type"] = str(suggested)
                    if update_kwargs:
                        ok = await self.bucket_mgr.update(bucket_id, **update_kwargs)
                        if not ok:
                            raise RuntimeError(f"bucket {bucket_id} 优化更新失败")
                    logger.info(f"Executed optimize: updated bucket {bucket_id} -> {update_kwargs}")

            elif action_type == "pattern_proposal":
                # --- Create ring experience buckets from distilled rules ---
                # --- 按提炼的规则创建 ring 年轮经验桶 ---
                rules = action_data.get("rules", []) or []
                if isinstance(rules, str):
                    rules = [{"rule": rules, "domain": "生活"}]
                chain_topic = action_data.get("chain_topic", "")
                created = 0
                for r in rules:
                    if isinstance(r, dict):
                        rule_text = (r.get("rule") or "").strip()
                    else:
                        rule_text = str(r).strip()
                    if not rule_text:
                        continue
                    await self.bucket_mgr.save_pattern(
                        name=rule_text[:30],
                        description=rule_text,
                        triggers=f"事件链沉淀：{chain_topic}",
                    )
                    created += 1
                if created == 0:
                    raise RuntimeError("pattern_proposal 无可执行的规则")
                logger.info(f"Executed pattern_proposal: created {created} ring patterns from chain '{chain_topic}'")

            else:
                logger.warning(f"Unknown action type / 未知提案类型: {action_type} — marked approved without execution")

        except Exception as e:
            logger.warning(f"Failed to execute action {action_id} ({action_type}): {e}")
            return False

        data["status"] = "approved"
        data["approved_by"] = approved_by
        data["approved_at"] = datetime.now(timezone.utc).isoformat()
        data["executed_at"] = datetime.now(timezone.utc).isoformat()

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"Failed to persist approved action / 保存批准状态失败 {action_id}: {e}")
            return False

        logger.info(f"Updated action {action_id} → approved")
        return True
    
    async def reject_action(self, action_id: str) -> bool:
        """
        Reject a pending action and physically remove the proposal file from the
        echo chamber — unapproved proposals are cleared immediately, never kept.
        拒绝提案：立即物理清空回音壁中的提案文件，未通过的提案不保留。
        """
        file_path = os.path.join(self.echo_chamber.pending_actions_dir, f"{action_id}.json")
        if not os.path.exists(file_path):
            return False
        try:
            os.remove(file_path)
            logger.info(f"Rejected & removed pending action: {action_id}")
            return True
        except OSError as e:
            logger.error(f"Failed to remove rejected action {action_id}: {e}")
            return False
    
    def _generate_id(self) -> str:
        """Generate a unique ID."""
        return str(uuid.uuid4())[:8]
    
    async def run_pipeline(self) -> dict:
        """Run both daily and weekly jobs (for manual trigger)."""
        results = {}
        results["daily"] = await self.run_daily_job()
        if datetime.now(timezone.utc).weekday() == 6:
            results["weekly"] = await self.run_weekly_job()
        return results