# Cost dashboard and spend controls

(Moved from README §Cost dashboard (B2 / Layer X) incl. balance read-out, caching, Prometheus textfile, Replicate throttle on 2026-06-27. See [../README.md](../README.md).)

## Cost dashboard (B2 / Layer X)

`kinoforge cost` reads the ledger, classifies each entry against the
Layer V verdict set, and renders a cost view in one of three modes:

```bash
pixi run kinoforge cost -c ../examples/configs/cost.yaml              # human table
pixi run kinoforge cost -c ../examples/configs/cost.yaml --json       # stable JSON
pixi run kinoforge cost -c ../examples/configs/cost.yaml --prom       # Prometheus textfile
```

### Balance read-out (RunPod only today)

When `compute.provider: runpod` is configured and `RUNPOD_API_KEY` is
set, the dashboard hits the RunPod GraphQL `{ myself { clientBalance } }`
query once per provider and renders:

```
Burn rate: $0.79/hr
Per-provider:
  runpod: $0.79/hr  spend $1.58  balance $42.18  [LIVE=1]
```

Other providers render `balance: N/A` until a satisfier ships
(Replicate, Runway, Luma do not expose a balance API; Bedrock /
Vertex / SkyPilot deferred — see
`docs/superpowers/specs/2026-06-12-b2-cost-dashboard-design.md` §13).

### Caching

Balance reads cache to `<store>/_cost_cache/cost/balance_<provider>.json`
with a 15-second default TTL so `watch -n 2 kinoforge cost` does not
burn the RunPod GraphQL rate limit. Override with `--cache-ttl=N` or
disable with `--no-cache`. The cached value is rendered as the source
of truth and a `(transport (using cache), ...)` annotation appears
when a fresh fetch fails but a cache entry still exists.

### Prometheus textfile-collector cron pattern

```cron
*/30 * * * * pixi run kinoforge cost -c .../cost.yaml --prom \
    > /var/lib/node_exporter/textfile/kinoforge.prom
```

Five gauges + one counter, all `kinoforge_*`-prefixed, with `provider`
and (where appropriate) `verdict` labels:

```
kinoforge_burn_rate_usd_per_hr{provider="runpod"}
kinoforge_balance_usd{provider="runpod"}
kinoforge_balance_as_of_seconds{provider="runpod"}
kinoforge_pod_count{provider="runpod", verdict="LIVE"}
kinoforge_spend_usd_total{provider="runpod"}
kinoforge_cost_scrape_errors_total{provider="runpod", reason="transport"}
```

### Replicate throttle warning

Set `KINOFORGE_REPLICATE_THROTTLE_AT_USD=N` to warn when Replicate
spend exceeds 90% of `N`. Default `4.50` (90% of Replicate's
documented $5 free-tier soft-throttle); set `0` to disable. Note: until
B10 (per-prediction hosted spend capture) ships, hosted-engine spend is
not in the ledger and the footer reads `replicate spend tracking
pending B10`.
