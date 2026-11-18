# ExperimentLoadBalanceBottleneck

Two continuously backlogged New York–London connections use independent
400 Mbps access links. Their private and shared core routes have 100 Mbps
links. Five single-path OrbCC connections offer 60 Mbps each on rank 1
during 20–40 s and on rank 3 during 60–80 s. The first burst competes with
A and the second competes with B. A and B can redistribute traffic on the
ranks they share. Both use the controller under test.

| K | A's saved ranks | B's saved ranks |
|---|---|---|
| 2 | 1, 2 | 2, 3 |
| 3 | 1, 2, 4 | 2, 3, 4 |
| 4 | 1, 2, 4, 5 | 2, 3, 4, 5 |
| 5 | 1, 2, 4, 5, 6 | 2, 3, 4, 5, 6 |

The default matrix is 100 runs per variant. It uses five start-time patterns
for Omega, Delta, Alpha, OrbCC uncoupled, and CUBIC uncoupled at K=2–5. K=1
is available explicitly via `--ks 1`. Both bursts then target rank 1. Add
`--include-omega10` for the optional 10% Omega variant.

Rebuild LeoSatellites and MPTCP and use an OMNeT++ environment with the other
experiment libraries built. The default simulation covers original times
0–100 s and reuses the `LeoLoadBalanceBackground` endpoint catalog and primary
core routes when present. Missing endpoint saves can be generated from the
primary saves. Both variants report rank availability including reconnects.

From the folder containing `runExperimentLoadBalanceBottleneck.py`:

```sh
python3 runExperimentLoadBalanceBottleneck.py --cores 30
```

The plot directory is `simulations/plots/experimentLoadBalanceBottleneck`.
`goodput_fairness.pdf` shows mean per-second Jain fairness of A and B against
their aggregate application goodput in Mbps during the bursts. Each panel
shows one K and each controller marker has a spread ellipse when applicable.
Individual plots show A, B, and background goodput over time. The CSVs include
per-burst metrics and measurement coverage. To plot again from extracted CSVs,
pass `--start-step 4 --skip-generate`. Reruns use fixed paths for each run.

The [shared-budget variant](../experimentLoadBalanceBottleneckSharedBudget/README.md)
uses the same traffic and route saves with a fixed aggregate ISL budget.
