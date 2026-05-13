# Day 10 Reliability Report

## 1. Architecture summary

The gateway checks cache first, then routes through a circuit-breaker-protected provider chain, and falls back to a static message if every provider fails. The cache uses guardrails for privacy-sensitive prompts and year/date mismatch detection; the circuit breaker prevents retry storms and allows recovery probes after a timeout.

```
User Request
    |
    v
[Gateway] ---> [Cache check] ---> HIT? return cached
    |                                 |
    v                                 v MISS
[Circuit Breaker: Primary] -------> Provider A
    |  (OPEN? skip)
    v
[Circuit Breaker: Backup] --------> Provider B
    |  (OPEN? skip)
    v
[Static fallback message]
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Trips fast on repeated provider failures without overreacting to one-off jitter. |
| reset_timeout_seconds | 2 | Short enough to recover quickly, long enough to avoid immediate retry storms. |
| success_threshold | 1 | One successful probe is enough to close the circuit in this lab setup. |
| cache TTL | 300 | Keeps FAQ-like answers fresh while still allowing reuse across the load run. |
| similarity_threshold | 0.92 | High enough to block obvious false hits on date-sensitive prompts. |
| load_test requests | 100 | Matches the lab default and keeps the chaos run reproducible. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 99.5% | Yes |
| Latency P95 | < 2500 ms | 320.61 ms | Yes |
| Fallback success rate | >= 95% | 96.36% | Yes |
| Cache hit rate | >= 10% | 80.75% | Yes |
| Recovery time | < 5000 ms | 4653.42 ms | Yes |

## 4. Metrics

Paste or summarize `reports/metrics.json`.

| Metric | Value |
|---|---:|
| availability | 0.995 |
| error_rate | 0.005 |
| latency_p50_ms | 3.26 |
| latency_p95_ms | 320.61 |
| latency_p99_ms | 517.87 |
| fallback_success_rate | 0.9636 |
| cache_hit_rate | 0.8075 |
| estimated_cost_saved | 0.323 |
| circuit_open_count | 7 |
| recovery_time_ms | 4653.423309326172 |

## 5. Cache comparison

Run simulation with cache enabled vs disabled. Fill in both columns:

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 231.31 | 3.26 | -228.05 ms |
| latency_p95_ms | 517.68 | 320.61 | -197.07 ms |
| estimated_cost | 0.126434 | 0.031242 | -0.095192 |
| cache_hit_rate | 0 | 0.8075 | +0.8075 |

## 6. Redis shared cache

Explain why shared cache matters for production:

- Why in-memory cache is insufficient for multi-instance deployments: each process gets its own cache, so scale-out instances do not share hits and one instance cannot benefit from another instance's warm cache.
- How `SharedRedisCache` solves this: it stores query/response pairs in Redis with a shared key namespace, so all gateway instances read and write the same cache entries.

### Evidence of shared state

Show that two separate cache instances can see the same data:

```
c1 ('shared response', 1.0)
c2 ('shared response', 1.0)
keys ['rl:demo:11956e8badb2']
```

### Redis CLI output

```bash
# docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:demo:11956e8badb2
```

### In-memory vs Redis latency comparison (optional)

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms | 0.05 | 3.26 | Redis adds network overhead. |
| latency_p95_ms | 480.4 | 320.61 | Lower here due to run-to-run variance, not a guaranteed trend. |

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | All traffic fallback to backup, circuit opens | Primary opened, backup served traffic, availability stayed high | Pass |
| primary_flaky_50 | Circuit oscillates, mix of primary and fallback | Mixed provider use, but this run did not meet the pass criteria consistently | Fail |
| all_healthy | All requests via primary, no circuit opens | Primary served requests normally | Pass |
| cache_stale_candidate | Date-sensitive queries should not false-hit the cache | Guardrails still allowed this scenario to fail in at least one run | Fail |

## 8. Failure analysis

The main remaining weakness is cache safety for date-sensitive prompts. The system still fails the `cache_stale_candidate` scenario, which means similarity plus numeric mismatch guardrails are not strict enough in every case.

Before production, I would tighten cache rules for date-bearing prompts, add explicit date/year parsing before cache return, and log false-hit decisions with the cached source query and score. I would also move circuit state into shared storage if the gateway is deployed across multiple instances.

## 9. Next steps

1. Add date-aware cache filtering so year-sensitive prompts never reuse stale answers.
2. Move circuit breaker state to Redis so failures and recovery are shared across instances.
3. Add a concurrent load test and compare latency/cost with the current sequential run.
