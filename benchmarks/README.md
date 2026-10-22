# Reproducible performance measurements

These are local synthetic processing measurements, not live Asana export speeds. No customer records or identifiers are included.

```sh
python -m asana_export_kit benchmark --out ../benchmark-small --tasks 1000 --comments 5 --attachment-bytes 1074790400
python -m asana_export_kit benchmark --out ../benchmark-medium --tasks 10000 --comments 5
python -m asana_export_kit profile ../private-archive
```

The fixture has one project per 100 tasks, five comments per task, description text, and some parent references. It is a simple serialization workload, not a calibrated model of all customer graphs. The profile command prints aggregate counts, task-description sizes, archive size and coverage without task text or identifiers. Even aggregate customer statistics should stay private unless publication is authorized.

## Measured 2026-10-22

| Tasks / comments | Generate | Render + manifest | Verify | Peak Python allocations | Archive |
| --- | --- | --- | --- | --- | --- |
| 1,000 / 5,000 | 0.293 s | 1.658 s | 0.027 s | 15.13 MiB | 3.52 MiB |
| 10,000 / 50,000 | 4.481 s | 18.591 s | 0.084 s | 152.31 MiB | 35.17 MiB |

Both archives verified with zero integrity errors. A separate 1 GiB + 1 MiB sparse-file hash read 1,074,790,400 bytes in 3.262 seconds with 2.01 MiB peak Python allocations. This checks the hashing path above the old cap, not an actual network download or physical storage throughput. The attachment downloader's above-cap behavior is separately covered by mocked stream tests.

Environment: Python 3.12.14, Linux x86-64. Each size ran once; the two runs were launched concurrently, and filesystem caches and shared-host contention were uncontrolled. Timings include tracemalloc overhead. Memory means Python allocation peak, not process RSS. The raw reports are in [results/](results/). These are preliminary smoke benchmarks; use isolated repeated runs for performance comparisons.

Metadata memory grew roughly linearly in these two samples. Million-task workloads need further measurements and likely a disk-backed object store plus streaming output before claiming support at that scale. Streaming attachment hashes does not fix whole-dataset metadata memory.

## Live benchmark inputs

A live read benchmark needs a PAT or registered OAuth app plus user consent, a workspace scope, its free/paid tier, attachment-byte access, a host with sufficient storage, and a desired completion window. Supply secrets only locally through the supported prompt/environment. A recovery benchmark additionally needs an isolated writable destination, identity mappings, and explicit acceptable data-loss and recovery-time targets.

Measure object counts by kind, comments per task, relationship fan-out, nesting depth, custom-field counts, attachment counts/size distribution, API requests, pagination, 429s, retries, bytes, wall-clock duration, process RSS, interrupted-run overhead, observed source changes, and coverage errors. For restoration, measure semantic fidelity and duplicate-free resume as well as elapsed time.

Use 1k/10k/100k/1M tasks as proposed stress points, with sparse/typical/comment-heavy histories, multi-homing and deep nesting, no/small/large external attachments, and zero/low/high concurrent mutation. These are test cases, not claimed customer percentiles. Calibrate them against a real account profile before calling them representative.

## API time can dominate

The current REST path makes at least eight requests per successfully hydrated task when time entries are enabled, before extra pages, global/project operations, attachment details/downloads, or retries. For 100,000 tasks that means at least 800,000 requests:

| Request budget | Rate-only lower bound |
| --- | --- |
| Exporter default, 120/min | 111.1 hours |
| Asana documented free domain, 150/min | 88.9 hours |
| Asana documented paid domain, 1,500/min | 8.9 hours |

These are arithmetic lower bounds, not predictions. The collector is serial, and latency, cost quotas, changing permissions, and throttling can extend them. See Asana's [rate limits](https://developers.asana.com/docs/rate-limits). A future faster path should reduce duplicate detail requests and use controlled concurrency within all documented limits; a batching transport does not make underlying API operations free.
