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
from pathlib import Path

try:
    from rapidfuzz import fuzz, process
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

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
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "EventChain":
        chain = cls(data["chain_id"], data["topic"], data.get("status", "in_progress"))
        chain.timeline = data.get("timeline", [])
        chain.summary = data.get("summary", "")
        chain.created = data.get("created", datetime.now(timezone.utc).isoformat())
        chain.updated = data.get("updated", chain.created)
        chain.source_bucket_ids = data.get("source_bucket_ids", [])
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
    
    async def add_pending_action(self, action_type: str, data: dict):
        """
        Add a pending action to echo chamber.
        action_type: cleanup/merge/chain_update
        """
        action_id = str(uuid.uuid4())[:8]
        file_path = os.path.join(self.pending_actions_dir, f"{action_id}.json")
        
        action = {
            "action_id": action_id,
            "action_type": action_type,
            "status": "pending",
            "data": data,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(action, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Added pending action: {action_type} - {action_id}")
    
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
    
    async def update_action_status(self, action_id: str, status: str):
        """Update action status (approve/reject/executed)."""
        file_path = os.path.join(self.pending_actions_dir, f"{action_id}.json")
        if not os.path.exists(file_path):
            return False
        
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        data["status"] = status
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Updated action {action_id} → {status}")
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
    
    def __init__(self, config: dict, bucket_mgr, dehydrator=None):
        self.bucket_mgr = bucket_mgr
        self.dehydrator = dehydrator
        
        data_dir = config.get("buckets_dir", os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets"))
        self.event_chains_dir = os.path.join(data_dir, "event_chains")
        self.echo_chamber_dir = os.path.join(data_dir, "echo_chamber")
        self._state_file = os.path.join(self.echo_chamber_dir, ".housekeeper_state.json")
        
        os.makedirs(self.event_chains_dir, exist_ok=True)
        os.makedirs(self.echo_chamber_dir, exist_ok=True)
        
        self.echo_chamber = EchoChamber(self.echo_chamber_dir)
        
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_daily_run = None
        self._last_weekly_run = None
        
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
                
                logger.info(f"Loaded housekeeper state: daily={self._last_daily_run}, weekly={self._last_weekly_run}")
            except Exception as e:
                logger.error(f"Failed to load housekeeper state: {e}")
    
    def _save_state(self):
        """Save last run times to state file."""
        try:
            state = {
                "last_daily_run": self._last_daily_run.isoformat() if self._last_daily_run else None,
                "last_weekly_run": self._last_weekly_run.isoformat() if self._last_weekly_run else None,
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
        """Check if daily/weekly jobs should run."""
        now = datetime.now(timezone.utc)
        
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
        
        logger.info(f"Daily job complete: {results}")
        return results
    
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
            await self._write_weekly_digest(results)
        except Exception as e:
            logger.error(f"Weekly digest write failed: {e}")
        
        logger.info(f"Weekly job complete: {results}")
        return results
    
    async def _daily_summary(self) -> dict:
        """Generate daily summary of today's buckets with mood analysis using AI."""
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return {"error": str(e)}
        
        today_buckets = []
        for b in all_buckets:
            created_str = b["metadata"].get("created", "")
            try:
                created = datetime.fromisoformat(str(created_str))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created >= today_start:
                    today_buckets.append(b)
            except (ValueError, TypeError):
                continue
        
        if not today_buckets:
            return {"message": "No buckets from today"}
        
        all_contents = []
        bucket_info = []
        for b in today_buckets:
            meta = b["metadata"]
            all_contents.append(b["content"])
            bucket_info.append(f"- [{meta.get('created', '')}] {meta.get('name', '')}: {b['content'][:100]}")
        
        summary = ""
        
        if self.dehydrator and self.dehydrator.client:
            try:
                combined_content = "\n\n".join(all_contents[:50])
                
                prompt = f"""你是一个AI记忆管家，负责总结用户今日的记忆内容。请根据以下记忆内容，生成一份简洁、全面的每日摘要。

要求：
1. 用中文撰写，语气温和亲切
2. 总结今日的主要事件和话题
3. 识别用户的情绪状态
4. 提取关键事实和待办事项
5. 保持在200字以内

今日记忆内容：
{combined_content}
"""
                
                response = await self.dehydrator.client.chat.completions.create(
                    model=self.dehydrator.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=300,
                )
                
                summary = response.choices[0].message.content.strip()
                logger.info("Daily summary generated by AI")
            except Exception as e:
                logger.error(f"AI daily summary failed: {e}")
                summary = "\n".join(bucket_info)
        else:
            summary = "\n".join(bucket_info)
        
        mood_tags = self._analyze_daily_mood(today_buckets, all_contents)
        
        await self.echo_chamber.write_digest(
            digest_type="daily",
            content=summary,
            metadata={
                "bucket_count": len(today_buckets),
                "mood_tags": mood_tags["tags"],
                "mood_score": mood_tags["score"],
                "mood_level": mood_tags["level"],
            }
        )
        
        return {"buckets_processed": len(today_buckets), "mood_tags": mood_tags["tags"], "mood_level": mood_tags["level"]}
    
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
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        
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
        
        return {"chains_updated": chains_updated}
    
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
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        
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
            
            for history_bucket in history_buckets:
                history_content = history_bucket["content"]
                
                conflict_result = self._detect_conflict(today_content, history_content)
                if conflict_result:
                    conflicts_found += 1
                    
                    await self.echo_chamber.add_pending_action(
                        action_type="conflict",
                        data={
                            "new_bucket_id": today_bucket["id"],
                            "old_bucket_id": history_bucket["id"],
                            "new_content": today_content[:200],
                            "old_content": history_content[:200],
                            "conflict_type": conflict_result["type"],
                            "conflict_reason": conflict_result["reason"],
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
        
        return {"conflicts_found": conflicts_found}
    
    def _detect_conflict(self, new_content: str, old_content: str) -> dict | None:
        """
        Detect semantic conflicts between two memory contents.
        Returns conflict info dict or None if no conflict.
        """
        import re
        
        conflict_patterns = [
            {
                "type": "preference",
                "patterns": [
                    (r'(不喜欢|讨厌|不想|不要|不爱)', r'(喜欢|爱|想|要)'),
                    (r'(不吃|不喝|不用)', r'(吃|喝|用)'),
                    (r'(不买|不想要)', r'(买|想要)'),
                    (r'(太甜|太咸|太辣|太苦)', r'(全糖|很甜|很甜)'),
                    (r'(清淡|少油|少盐|无糖)', r'(重口味|油腻|全糖|很甜)'),
                ],
                "reason_template": "偏好冲突：之前说过'{old_match}'，但今天说'{new_match}'",
            },
            {
                "type": "health",
                "patterns": [
                    (r'(病好了|康复了|不痛了|没事了)', r'(生病|不舒服|痛|难受)'),
                    (r'(痊愈|恢复正常)', r'(发烧|感冒|咳嗽|胃痛)'),
                    (r'(已经好了|不难受了)', r'(痛经|头痛|头晕)'),
                ],
                "reason_template": "健康状态冲突：之前记录'{old_match}'，但今天记录'{new_match}'",
            },
            {
                "type": "status",
                "patterns": [
                    (r'(不在|走了|离开了)', r'(在|来了|到达)'),
                    (r'(完成了|做完了|结束了)', r'(开始|正在做|进行中)'),
                    (r'(放弃|取消|不做了)', r'(计划|打算|准备)'),
                ],
                "reason_template": "状态冲突：之前记录'{old_match}'，但今天记录'{new_match}'",
            },
            {
                "type": "fact",
                "patterns": [
                    (r'(没有|从未|从没)', r'(有|曾经|以前)'),
                    (r'(不是|并非)', r'(是|确实是)'),
                    (r'(不知道|不清楚)', r'(知道|清楚|了解)'),
                ],
                "reason_template": "事实冲突：之前说'{old_match}'，但今天说'{new_match}'",
            },
        ]
        
        for conflict_type_info in conflict_patterns:
            for old_pattern, new_pattern in conflict_type_info["patterns"]:
                old_match = re.search(old_pattern, old_content)
                new_match = re.search(new_pattern, new_content)
                
                if old_match and new_match:
                    return {
                        "type": conflict_type_info["type"],
                        "reason": conflict_type_info["reason_template"].format(
                            old_match=old_match.group(0),
                            new_match=new_match.group(0)
                        ),
                    }
                
                old_match_rev = re.search(old_pattern, new_content)
                new_match_rev = re.search(new_pattern, old_content)
                
                if old_match_rev and new_match_rev:
                    return {
                        "type": conflict_type_info["type"],
                        "reason": conflict_type_info["reason_template"].format(
                            old_match=new_match_rev.group(0),
                            new_match=old_match_rev.group(0)
                        ),
                    }
        
        return None
    
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
        chain.timeline.sort(key=lambda x: x["timestamp"])
        chain.updated = datetime.now(timezone.utc).isoformat()
        
        await self._save_event_chain(chain)
        logger.debug(f"Added temporary node to chain: {chain.chain_id}")
    
    async def _weekly_chain_merge(self) -> dict:
        """Deduplicate and merge event chains."""
        chains = await self.get_event_chains()
        if not chains:
            return {"message": "No chains to merge"}
        
        merged_count = 0
        to_merge = []
        
        for i, c1 in enumerate(chains):
            for j, c2 in enumerate(chains[i+1:]):
                if HAS_RAPIDFUZZ:
                    if fuzz.ratio(c1.topic, c2.topic) >= 70:
                        to_merge.append((c1, c2))
        
        for c1, c2 in to_merge:
            c1.timeline.extend(c2.timeline)
            c1.timeline.sort(key=lambda x: x["timestamp"])
            c1.source_bucket_ids.extend(c2.source_bucket_ids)
            c1.updated = datetime.now(timezone.utc).isoformat()
            
            await self._save_event_chain(c1)
            
            chain_file = os.path.join(self.event_chains_dir, f"{c2.chain_id}.json")
            if os.path.exists(chain_file):
                os.remove(chain_file)
            
            merged_count += 1
        
        return {"chains_merged": merged_count}
    
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
        """Write weekly digest to echo chamber."""
        digest_content = f"""每周管家报告

【事件链合并】
合并了 {results.get('chain_merge', {}).get('chains_merged', 0)} 条事件链

【清理提案】
生成了 {results.get('cleanup_scan', {}).get('proposals_created', 0)} 条清理提案

请审阅并执行必要的批准操作。"""
        
        await self.echo_chamber.write_digest(
            digest_type="weekly",
            content=digest_content,
            metadata=results
        )
    
    def _extract_topics(self, buckets: list) -> dict:
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
                topic = self._generate_topic_name(content1)
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
                prompt = f"""你是一个AI记忆管家。请为以下记忆内容生成一个简短、准确的主题名称（4-8个字）。

记忆内容：
{content[:500]}

主题名称："""
                
                response = await self.dehydrator.client.chat.completions.create(
                    model=self.dehydrator.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=20,
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
        
        await self._save_event_chain(chain)
        logger.info(f"Created event chain: {chain_id} - {topic}")
        return True
    
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
                
                prompt = f"""你是一个AI记忆管家。请为以下事件链生成一个简洁的摘要，概括事件的背景、进展和关键信息。

事件主题：{topic}

相关记忆内容：
{bucket_contents}

请用中文撰写，保持在100字以内："""
                
                response = await self.dehydrator.client.chat.completions.create(
                    model=self.dehydrator.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=150,
                )
                
                return response.choices[0].message.content.strip()[:200]
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
    
    async def review_digest(self) -> dict:
        """Get digest for main AI review."""
        return await self.echo_chamber.get_review_summary()
    
    async def approve_action(self, action_id: str) -> bool:
        """Approve a pending action."""
        return await self.echo_chamber.update_action_status(action_id, "approved")
    
    async def reject_action(self, action_id: str) -> bool:
        """Reject a pending action."""
        return await self.echo_chamber.update_action_status(action_id, "rejected")
    
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