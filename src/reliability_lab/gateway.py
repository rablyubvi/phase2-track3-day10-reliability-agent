from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None
    route_reason: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        cost_budget: float | None = 0.05,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cost_budget = cost_budget
        self.cumulative_cost = 0.0

    def complete(self, prompt: str) -> GatewayResponse:
        started_at = time.perf_counter()
        if self.cache is not None:
            cached, score = self.cache.get(prompt)
            if cached is not None:
                latency_ms = (time.perf_counter() - started_at) * 1000
                return GatewayResponse(
                    text=cached,
                    route="cache_hit",
                    provider=None,
                    cache_hit=True,
                    latency_ms=latency_ms,
                    estimated_cost=0.0,
                    route_reason=f"cache_hit:{score:.2f}",
                )

        last_error: str | None = None
        for index, provider in enumerate(self.providers):
            breaker = self.breakers.get(provider.name)
            if breaker is None:
                last_error = f"missing_breaker:{provider.name}"
                continue
            if (
                self.cost_budget is not None
                and self.cumulative_cost >= self.cost_budget
                and index > 0
            ):
                last_error = "cost_budget_exceeded"
                continue
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                self.cumulative_cost += response.estimated_cost
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                route = "primary" if index == 0 else "fallback"
                latency_ms = (time.perf_counter() - started_at) * 1000
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=latency_ms,
                    estimated_cost=response.estimated_cost,
                    route_reason=f"{route}:{provider.name}",
                )
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                continue

        latency_ms = (time.perf_counter() - started_at) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=latency_ms,
            estimated_cost=0.0,
            error=last_error,
            route_reason=f"static_fallback:{last_error or 'all_providers_failed'}",
        )
