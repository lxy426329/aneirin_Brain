import logging
import re
import jieba
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder
import asyncio
import os

logger = logging.getLogger("ombre_brain.hybrid_search")


class HybridSearchEngine:
    def __init__(self, config: dict, embedding_engine=None):
        self.enabled = config.get("enabled", True)
        self.embedding_engine = embedding_engine
        
        self.exact_keywords = set(config.get("exact_query_keywords", []))
        self.exact_threshold = config.get("exact_threshold", 0.2)
        self.semantic_threshold = config.get("semantic_threshold", 0.1)
        self.vector_weight_scale = config.get("vector_weight_scale", 0.2)
        self.keyword_weight_scale = config.get("keyword_weight_scale", 2.0)
        
        self.rerank_top_k = config.get("rerank_top_k", 5)
        self.rerank_model_name = config.get("rerank_model", "BAAI/bge-reranker-base")
        
        self._bm25_index = None
        self._bm25_corpus = []
        self._bm25_bucket_ids = []
        self._rerank_model = None
        self._is_initialized = False
        
        self._lock = asyncio.Lock()
    
    async def initialize(self):
        """Initialize BM25 index and Rerank model."""
        async with self._lock:
            if self._is_initialized:
                return
            
            logger.info("Initializing Hybrid Search Engine...")
            
            try:
                from sentence_transformers import CrossEncoder
                self._rerank_model = CrossEncoder(self.rerank_model_name)
                logger.info(f"Rerank model loaded: {self.rerank_model_name}")
            except Exception as e:
                logger.warning(f"Failed to load rerank model, using simple scoring fallback: {e}")
                self._rerank_model = None
            
            self._is_initialized = True
            logger.info("Hybrid Search Engine initialized")
    
    def build_bm25_index(self, buckets: list[dict]):
        """Build BM25 index from memory buckets."""
        if not self.enabled:
            return
        
        corpus = []
        bucket_ids = []
        
        for bucket in buckets:
            meta = bucket.get("metadata", {})
            name = meta.get("name", "")
            domain = " ".join(meta.get("domain", []))
            tags = " ".join(meta.get("tags", []))
            content = bucket.get("content", "")[:2000]
            
            text = f"{name} {domain} {tags} {content}"
            tokens = self._tokenize(text)
            
            if tokens:
                corpus.append(tokens)
                bucket_ids.append(bucket["id"])
        
        if corpus:
            self._bm25_index = BM25Okapi(corpus)
            self._bm25_corpus = corpus
            self._bm25_bucket_ids = bucket_ids
            logger.info(f"BM25 index built with {len(bucket_ids)} buckets")
    
    def _tokenize(self, text: str) -> list[str]:
        """Chinese + English tokenization for BM25."""
        text = text.lower().strip()
        if not text:
            return []
        
        tokens = []
        try:
            jieba_tokens = jieba.lcut(text)
            for token in jieba_tokens:
                token = token.strip()
                if token and len(token) >= 1 and not re.match(r'^\s*$', token):
                    tokens.append(token)
        except Exception:
            tokens = text.split()
        
        return tokens
    
    def _is_exact_match_query(self, query: str) -> bool:
        """Detect if query requires exact keyword matching."""
        q = query.strip()
        
        if len(q) <= 2:
            return True
        
        if re.match(r'^\d{4}-\d{2}(-\d{2})?$', q):
            return True
        
        if re.match(r'^[\da-fA-F]{4,}$', q):
            return True
        
        if q in self.exact_keywords:
            return True
        
        if q.startswith("[[") and q.endswith("]]"):
            return True
        
        return False
    
    async def search(
        self,
        query: str,
        buckets: list[dict],
        limit: int = 10,
        force_keyword: bool = False,
    ) -> list[dict]:
        """
        Hybrid search: BM25 + Vector + Rerank.
        
        Returns buckets sorted by relevance.
        """
        if not query or not query.strip() or not buckets:
            return []
        
        is_exact_query = force_keyword or self._is_exact_match_query(query)
        
        if not self._is_initialized:
            await self.initialize()
        
        self.build_bm25_index(buckets)
        
        bm25_results = []
        if self._bm25_index:
            query_tokens = self._tokenize(query)
            if query_tokens:
                scores = self._bm25_index.get_scores(query_tokens)
                bm25_results = [
                    (self._bm25_bucket_ids[i], float(scores[i]))
                    for i in range(len(scores))
                    if scores[i] > 0
                ]
                bm25_results.sort(key=lambda x: x[1], reverse=True)
                bm25_results = bm25_results[:limit * 3]
        
        vector_results = []
        if self.embedding_engine and self.embedding_engine.enabled and not is_exact_query:
            try:
                vector_results = await self.embedding_engine.search_similar(query, top_k=limit * 3)
            except Exception as e:
                logger.warning(f"Vector search failed: {e}")
        
        combined_results = {}
        bucket_map = {b["id"]: b for b in buckets}
        
        for bid, score in bm25_results:
            if bid not in combined_results:
                combined_results[bid] = {"bm25": 0, "vector": 0}
            combined_results[bid]["bm25"] = score
        
        for bid, score in vector_results:
            if bid not in combined_results:
                combined_results[bid] = {"bm25": 0, "vector": 0}
            combined_results[bid]["vector"] = score
        
        candidate_bucket_ids = list(combined_results.keys())[:limit * 4]
        candidate_buckets = [bucket_map.get(bid) for bid in candidate_bucket_ids if bucket_map.get(bid)]
        
        if not candidate_buckets:
            return []
        
        if self._rerank_model and len(candidate_buckets) > 1:
            try:
                pairs = [[query, self._get_bucket_text(b)] for b in candidate_buckets]
                scores = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._rerank_model.predict,
                    pairs
                )
                
                scored = list(zip(candidate_buckets, scores))
                scored.sort(key=lambda x: x[1], reverse=True)
                
                final_results = [b for b, _ in scored[:limit]]
                
                for i, b in enumerate(final_results[:3]):
                    b["score"] = round(scored[i][1], 4)
                    b["search_mode"] = "hybrid_rerank"
                
                return final_results
            except Exception as e:
                logger.warning(f"Rerank failed, falling back to weighted scoring: {e}")
        
        final_scored = []
        for bid in candidate_bucket_ids[:limit * 2]:
            bucket = bucket_map.get(bid)
            if not bucket:
                continue
            
            meta = bucket.get("metadata", {})
            bm25_score = combined_results[bid]["bm25"]
            vector_score = combined_results[bid]["vector"]
            
            bm25_normalized = min(1.0, bm25_score / 10.0)
            
            if is_exact_query:
                keyword_weight = self.keyword_weight_scale
                vector_weight = self.vector_weight_scale
            else:
                keyword_weight = 1.0
                vector_weight = 1.0
            
            time_score = self._calc_time_score(meta)
            emotion_score = self._calc_emotion_score(meta)
            priority = 1.0 if (meta.get("pinned") or meta.get("protected")) else 0.0
            
            total_weight = keyword_weight + vector_weight + 0.5 + 0.3 + 0.2
            raw_score = (
                bm25_normalized * keyword_weight +
                vector_score * vector_weight +
                time_score * 0.5 +
                emotion_score * 0.3 +
                priority * 0.2
            )
            final_score = raw_score / total_weight if total_weight > 0 else 0.0
            
            if is_exact_query:
                threshold = self.exact_threshold
            else:
                threshold = self.semantic_threshold
            
            if final_score >= threshold:
                bucket["score"] = round(final_score, 4)
                bucket["search_mode"] = "exact" if is_exact_query else "semantic_hybrid"
                bucket["dimensions"] = {
                    "bm25": round(bm25_normalized, 3),
                    "vector": round(vector_score, 3),
                    "time": round(time_score, 3),
                    "emotion": round(emotion_score, 3),
                    "priority": priority,
                }
                final_scored.append(bucket)
        
        final_scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        return final_scored[:limit]
    
    def _get_bucket_text(self, bucket: dict) -> str:
        """Get full text content of a bucket for reranking."""
        meta = bucket.get("metadata", {})
        name = meta.get("name", "")
        domain = " ".join(meta.get("domain", []))
        tags = " ".join(meta.get("tags", []))
        content = bucket.get("content", "")[:1000]
        return f"{name} {domain} {tags} {content}".strip()
    
    def _calc_time_score(self, meta: dict) -> float:
        """Calculate time proximity score (0~1)."""
        from datetime import datetime
        try:
            created = meta.get("created", "")
            if created:
                created_time = datetime.fromisoformat(created.replace('Z', '+00:00'))
                now = datetime.now(datetime.timezone.utc)
                hours_diff = (now - created_time).total_seconds() / 3600
                
                if hours_diff < 24:
                    return 1.0
                elif hours_diff < 168:
                    return 0.7
                elif hours_diff < 720:
                    return 0.4
                else:
                    return 0.1
        except Exception:
            pass
        return 0.5
    
    def _calc_emotion_score(self, meta: dict) -> float:
        """Calculate emotion arousal score (0~1)."""
        try:
            arousal = meta.get("arousal", 0.0)
            if isinstance(arousal, (int, float)) and 0 <= arousal <= 1:
                return arousal
        except Exception:
            pass
        return 0.5
