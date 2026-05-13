from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit\.card|ssn|social\.security|user\s*\d+|account\s*\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory response cache with TTL and false-hit guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        best_value: str | None = None
        best_key: str | None = None
        best_score = 0.0
        now = time.time()
        self._entries = [
            entry for entry in self._entries if now - entry.created_at <= self.ttl_seconds
        ]
        for entry in self._entries:
            if entry.key == query:
                return entry.value, 1.0
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key
        if best_key is not None and _looks_like_false_hit(query, best_key):
            self.false_hit_log.append({"query": query, "cached_query": best_key, "score": best_score})
            return None, best_score
        if best_score >= self.similarity_threshold:
            return best_value, best_score
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        metadata = metadata or {}
        if _is_uncacheable(query):
            return
        if any(v.lower() in {"high", "critical"} for v in metadata.values()):
            return
        self._entries = [entry for entry in self._entries if entry.key != query]
        self._entries.append(CacheEntry(query, value, time.time(), metadata))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        left = ResponseCache._normalize_tokens(a)
        right = ResponseCache._normalize_tokens(b)
        if not left or not right:
            return 0.0
        if a.strip().lower() == b.strip().lower():
            return 1.0
        token_score = len(left & right) / len(left | right)
        char_score = ResponseCache._ngram_similarity(a.lower(), b.lower(), n=3)
        score = (0.7 * token_score) + (0.3 * char_score)
        if _looks_like_false_hit(a, b):
            score *= 0.25
        return max(0.0, min(1.0, score))

    @staticmethod
    def _normalize_tokens(text: str) -> set[str]:
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        stopwords = {"a", "an", "and", "for", "in", "of", "the", "to"}
        return {token for token in tokens if token not in stopwords}

    @staticmethod
    def _ngram_similarity(a: str, b: str, n: int) -> float:
        if len(a) < n or len(b) < n:
            return 0.0
        left = {a[i : i + n] for i in range(len(a) - n + 1)}
        right = {b[i : i + n] for i in range(len(b) - n + 1)}
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0
        try:
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            exact_response = self._redis.hget(exact_key, "response")
            if exact_response is not None:
                return exact_response, 1.0

            best_value: str | None = None
            best_key: str | None = None
            best_score = 0.0
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(key, "query")
                cached_response = self._redis.hget(key, "response")
                if cached_query is None or cached_response is None:
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_score = score
                    best_value = cached_response
                    best_key = cached_query
            if best_key is not None and _looks_like_false_hit(query, best_key):
                self.false_hit_log.append({"query": query, "cached_query": best_key, "score": best_score})
                return None, best_score
            if best_score >= self.similarity_threshold:
                return best_value, best_score
            return None, best_score
        except Exception:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        metadata = metadata or {}
        if _is_uncacheable(query):
            return
        if any(v.lower() in {"high", "critical"} for v in metadata.values()):
            return
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            return

    def flush(self) -> None:
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
