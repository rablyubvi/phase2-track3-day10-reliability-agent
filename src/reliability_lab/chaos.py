from __future__ import annotations

import copy
import json
import random
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(
    config: LabConfig, provider_overrides: dict[str, float] | None = None
) -> ReliabilityGateway:
    providers = []
    for provider_config in config.providers:
        fail_rate = (
            provider_overrides.get(provider_config.name, provider_config.fail_rate)
            if provider_overrides
            else provider_config.fail_rate
        )
        providers.append(
            FakeLLMProvider(
                provider_config.name,
                fail_rate,
                provider_config.base_latency_ms,
                provider_config.cost_per_1k_tokens,
            )
        )
    breakers = {
        provider_config.name: CircuitBreaker(
            name=provider_config.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for provider_config in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def _scenario_queries(name: str, queries: list[str], request_count: int) -> list[str]:
    if name == "cache_stale_candidate":
        pair = [
            "Summarize refund policy for 2024 deadline",
            "Summarize refund policy for 2026 deadline",
        ]
        return [pair[i % len(pair)] for i in range(request_count)]
    return [random.choice(queries) for _ in range(request_count)]


def _scenario_passed(
    name: str, result: RunMetrics, cache: ResponseCache | SharedRedisCache | None
) -> bool:
    if name == "primary_timeout_100":
        return result.fallback_successes > 0 and result.circuit_open_count > 0
    if name == "primary_flaky_50":
        return result.successful_requests > 0 and result.circuit_open_count > 0
    if name == "cache_stale_candidate":
        return result.cache_hits > 0 and cache is not None and len(cache.false_hit_log) > 0
    return result.successful_requests > 0


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    scenario_config = copy.deepcopy(config)
    if scenario.name == "cache_stale_candidate":
        scenario_config.cache.enabled = True
        scenario_config.cache.similarity_threshold = min(
            scenario_config.cache.similarity_threshold,
            0.15,
        )
    gateway = build_gateway(scenario_config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    request_count = scenario_config.load_test.requests
    prompts = _scenario_queries(scenario.name, queries, request_count)
    for prompt in prompts:
        result = gateway.complete(prompt)
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        if result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
        if result.latency_ms:
            metrics.latencies_ms.append(result.latency_ms)

    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for transition in breaker.transition_log
        if transition["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    passed = _scenario_passed(scenario.name, metrics, gateway.cache)
    metrics.scenarios[scenario.name] = "pass" if passed else "fail"
    return metrics


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    scenarios = list(config.scenarios)
    existing_names = {scenario.name for scenario in scenarios}
    for scenario in (
        ScenarioConfig(
            name="primary_timeout_100",
            description="Primary provider fails 100% - all traffic should fallback",
            provider_overrides={"primary": 1.0},
        ),
        ScenarioConfig(
            name="primary_flaky_50",
            description="Primary provider fails 50% - circuit should oscillate",
            provider_overrides={"primary": 0.5},
        ),
        ScenarioConfig(
            name="cache_stale_candidate",
            description="Cache guardrail check for year-sensitive queries",
            provider_overrides={},
        ),
    ):
        if scenario.name not in existing_names:
            scenarios.append(scenario)

    combined = RunMetrics()
    for scenario in scenarios:
        result = run_scenario(config, queries, scenario)
        combined.scenarios[scenario.name] = result.scenarios[scenario.name]
        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (
                    combined.recovery_time_ms + result.recovery_time_ms
                ) / 2
    return combined
