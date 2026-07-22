# ============================================================
# Module: Memory Housekeeper (housekeeper.py)
# 模块：记忆管家
#
# Batch pipeline service that runs periodically to:
# 1. Consolidate fragmented memories into Event Chains
# 2. Flag stale memories for cleanup (proposals only)
# 
# All operations are staging-only - no direct deletion or overwriting.
# Final approval rests with the main AI.
# ============================================================

import os
import json
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


class Housekeeper:
    """
    Memory housekeeper service - batch pipeline for memory management.
    记忆管家服务 - 批量记忆管理管线。
    """
    
    def __init__(self, config: dict, bucket_mgr):
        self.bucket_mgr = bucket_mgr
        
        data_dir = config.get("buckets_dir", os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets"))
        self.event_chains_dir = os.path.join(data_dir, "event_chains")
        self.staging_dir = os.path.join(data_dir, "staging")
        self.cleanup_proposals_dir = os.path.join(self.staging_dir, "cleanup_proposals")
        
        os.makedirs(self.event_chains_dir, exist_ok=True)
        os.makedirs(self.cleanup_proposals_dir, exist_ok=True)
        
        self._task: asyncio.Task | None = None
        self._running = False
        self.run_interval_hours = config.get("housekeeper", {}).get("run_interval_hours", 24)
    
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
        logger.info(f"Housekeeper started, interval: {self.run_interval_hours}h")
    
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
        """Background loop: run pipeline → sleep → repeat."""
        while self._running:
            try:
                await self.run_pipeline()
            except Exception as e:
                logger.error(f"Housekeeper pipeline error: {e}")
            
            try:
                await asyncio.sleep(self.run_interval_hours * 3600)
            except asyncio.CancelledError:
                break
    
    async def run_pipeline(self) -> dict:
        """
        Execute one full housekeeping pipeline cycle.
        执行一轮完整的管家管线。
        """
        logger.info("Starting housekeeper pipeline...")
        results = {}
        
        try:
            results["event_chains"] = await self._task_event_chain_consolidation()
        except Exception as e:
            logger.error(f"Event chain consolidation failed: {e}")
            results["event_chains"] = {"error": str(e)}
        
        try:
            results["cleanup_proposals"] = await self._task_cleanup_scan()
        except Exception as e:
            logger.error(f"Cleanup scan failed: {e}")
            results["cleanup_proposals"] = {"error": str(e)}
        
        logger.info(f"Housekeeper pipeline complete: {results}")
        return results
    
    async def _task_event_chain_consolidation(self) -> dict:
        """
        Task 1: Consolidate fragmented memories into Event Chains.
        任务一：将分散的碎片记忆归拢到事件链中。
        """
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for consolidation: {e}")
            return {"error": str(e)}
        
        one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        recent_buckets = []
        
        for b in all_buckets:
            meta = b["metadata"]
            if meta.get("type") in ("permanent", "feel") or meta.get("pinned") or meta.get("protected"):
                continue
            
            created_str = meta.get("created", "")
            try:
                created = datetime.fromisoformat(str(created_str))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created >= one_week_ago:
                    recent_buckets.append(b)
            except (ValueError, TypeError):
                continue
        
        if not recent_buckets:
            return {"message": "No recent buckets to consolidate"}
        
        topics = self._extract_topics(recent_buckets)
        chains_updated = 0
        chains_created = 0
        
        for topic, buckets in topics.items():
            if len(buckets) < 2:
                continue
            
            existing_chain = await self._find_existing_chain(topic)
            
            if existing_chain:
                updated = await self._update_event_chain(existing_chain, buckets)
                if updated:
                    chains_updated += 1
            else:
                created = await self._create_event_chain(topic, buckets)
                if created:
                    chains_created += 1
        
        return {
            "topics_found": len(topics),
            "chains_updated": chains_updated,
            "chains_created": chains_created,
        }
    
    def _extract_topics(self, buckets: list) -> dict:
        """
        Extract topics from buckets by finding semantically similar content.
        通过语义相似性提取主题。
        """
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
    
    def _generate_topic_name(self, content: str) -> str:
        """Generate a concise topic name from content."""
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
            if fuzz.ratio(chain.topic, topic) >= 70:
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
        
        summary = self._generate_chain_summary(topic, buckets)
        
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
        chain.summary = self._generate_chain_summary(chain.topic, buckets)
        chain.updated = datetime.now(timezone.utc).isoformat()
        
        await self._save_event_chain(chain)
        logger.info(f"Updated event chain: {chain.chain_id} - {chain.topic}")
        return True
    
    def _generate_chain_summary(self, topic: str, buckets: list) -> str:
        """Generate a summary for the event chain."""
        if not buckets:
            return ""
        
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
    
    async def _task_cleanup_scan(self) -> dict:
        """
        Task 2: Scan for stale memories and generate cleanup proposals.
        任务二：扫描废旧记忆并生成清理提案。
        """
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for cleanup scan: {e}")
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
                        await self._create_cleanup_proposal(b)
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
    
    async def _create_cleanup_proposal(self, bucket: dict):
        """Create a cleanup proposal for a stale bucket."""
        proposal_id = self._generate_id()
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
        
        reason = ", ".join(reasons)
        
        proposal = CleanupProposal(proposal_id, bucket["id"], reason)
        proposal.bucket_info = {
            "name": meta.get("name", bucket["id"]),
            "domain": meta.get("domain", []),
            "importance": importance,
            "created": meta.get("created", ""),
            "last_accessed": last_accessed_str,
        }
        
        await self._save_cleanup_proposal(proposal)
        logger.info(f"Created cleanup proposal: {proposal_id} for {bucket['id']}")
    
    async def _save_cleanup_proposal(self, proposal: CleanupProposal):
        """Save cleanup proposal to staging area."""
        file_path = os.path.join(self.cleanup_proposals_dir, f"{proposal.proposal_id}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(proposal.to_dict(), f, ensure_ascii=False, indent=2)
    
    async def get_cleanup_proposals(self, status: str = "pending") -> list[CleanupProposal]:
        """Get cleanup proposals by status."""
        proposals = []
        for filename in os.listdir(self.cleanup_proposals_dir):
            if filename.endswith(".json"):
                file_path = os.path.join(self.cleanup_proposals_dir, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        proposal = CleanupProposal.from_dict(data)
                        if status == "all" or proposal.status == status:
                            proposals.append(proposal)
                except Exception as e:
                    logger.warning(f"Failed to load cleanup proposal: {file_path}: {e}")
        
        proposals.sort(key=lambda p: p.created, reverse=True)
        return proposals
    
    async def update_cleanup_proposal(self, proposal_id: str, status: str):
        """Update cleanup proposal status."""
        file_path = os.path.join(self.cleanup_proposals_dir, f"{proposal_id}.json")
        if not os.path.exists(file_path):
            return False
        
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        proposal = CleanupProposal.from_dict(data)
        proposal.status = status
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(proposal.to_dict(), f, ensure_ascii=False, indent=2)
        
        logger.info(f"Updated cleanup proposal: {proposal_id} → {status}")
        return True
    
    async def approve_chain(self, chain_id: str) -> bool:
        """
        Approve an event chain - mark it as finalized.
        批准事件链 - 标记为已结案。
        """
        chain = await self.get_event_chain(chain_id)
        if not chain:
            return False
        
        chain.status = "resolved"
        chain.updated = datetime.now(timezone.utc).isoformat()
        await self._save_event_chain(chain)
        logger.info(f"Approved event chain: {chain_id}")
        return True
    
    async def reject_proposal(self, proposal_id: str) -> bool:
        """
        Reject a cleanup proposal - mark it as rejected.
        驳回清理提案。
        """
        return await self.update_cleanup_proposal(proposal_id, "rejected")
    
    async def approve_proposal(self, proposal_id: str) -> bool:
        """
        Approve a cleanup proposal - mark it as approved.
        批准清理提案。
        """
        return await self.update_cleanup_proposal(proposal_id, "approved")
    
    def _generate_id(self) -> str:
        """Generate a unique ID."""
        return str(uuid.uuid4())[:8]