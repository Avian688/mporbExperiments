# ExperimentLeoLoadBalanceBackground

Two continuously backlogged foreground connections run between co-located New
York and London user terminals, with independent 400 Mbps access links and
100 Mbps core links. Both foreground connections run the controller under
test. The third pair runs five concurrent single-path OrbCC background flows
on A's private rank 1 during local 20–40 s and on B's private rank 3 during
local 60–80 s. Each flow offers 60 Mbps and has its own TCP port pair. The five
flows share the background pair's access links. The sender opens each session
0.5 s early and stops it at the burst end. The receiver's recorded goodput
shows the delivered burst rate.
The default run starts at original time zero and lasts 100 s, with the usual
scheduled hard handovers every 15 s. The co-located pairs share the same
ranked core routes as their satellite attachments change.

| Requested subflows | A's saved ranks | B's saved ranks |
| --- | --- | --- |
| 1 | 1 | 1 |
| 2 | 1, 2 | 2, 3 |
| 3 | 1, 2, 4 | 2, 3, 4 |
| 4 | 1, 2, 4, 5 | 2, 3, 4, 5 |
| 5 | 1, 2, 4, 5, 6 | 2, 3, 4, 5, 6 |

At K=2, A and B compete on shared rank 2 during both bursts. At K=3–5, the
additional ranks are shared across connections and edge-disjoint within each.
The runner keeps the requested K when a route is unavailable and reports the
available duration of each rank in `route_availability.json`, including
reconnects. Five configured subflows require six distinct core routes to be
fully usable for both foreground pairs.

The default matrix has 100 runs. MPORB Alpha, Delta, Omega, uncoupled OrbCC,
and uncoupled CUBIC each run at K=2–5 with five start-time patterns. Select
`--ks 1` to run the one-subflow configurations with both bursts on rank 1.
Select `--ks 1 --protocols OrbSingle` for native single-path OrbCC. All runs
use simulation seed 1. Omega with 10% shuffling is opt-in.

## Run

Rebuild LeoSatellites and MPTCP and use the existing OMNeT++ environment and
built experiment dependencies. Run these commands
from the folder containing `runExperimentLeoLoadBalanceBackground.py`.

```sh
# Prepare, simulate, extract and plot the whole matrix.
python3 runExperimentLeoLoadBalanceBackground.py --cores 30

# Pilot one run per controller at K=2.
python3 runExperimentLeoLoadBalanceBackground.py --cores 2 --ks 2 --runs 1 --protocols Omega OrbUncoupled

# Rerun with the full set of endpoint vectors, then extract and plot.
python3 runExperimentLeoLoadBalanceBackground.py --start-step 2 --cores 30

# Regenerate all plots from existing CSVs.
python3 runExperimentLeoLoadBalanceBackground.py --start-step 4 --skip-generate

# Regenerate just the individual run plots from existing CSVs.
python3 runExperimentLeoLoadBalanceBackground.py --start-step 4 --skip-generate --plots individual --cores 30
```

The first step reuses the existing primary core saves and the
`LeoLoadBalanceBackground` endpoint catalog. If endpoint saves are missing, it
generates them in an unloaded run. The primary saves must cover the original
timeline through 100 s. Time-zero runs skip checkpoint capture. Pass
`--replay-start 115` to use the original 115–215 s window, which prepares a
matching checkpoint and needs primary saves through 215 s. Pass
`--route-store PATH` if the primary saves are outside the default LeoSatellites
store. Use `--end-step 1` for route preparation alone, or `--check-routes` to
check an existing catalog and report rank availability. Traffic results,
extracted CSVs and figures have fixed names.
`--rerun` replaces the selected traffic runs. This experiment measures goodput
and needs no ping prepass.

## Results

Figures are stored under `simulations/plots/experimentLeoLoadBalanceBackground`.
Each run's directory, `individual/<controller>/K<k>/run<n>/`, contains individual
metric plots and a `combined.pdf` for each foreground terminal. The `aggregate/`
folder contains side-by-side terminal comparisons and its own `combined.pdf`.
The run-level goodput overview shows A and B alongside aggregate background
goodput, with the burst windows shaded. Goodput-only runs can supply the
overview. Rerun them with the updated recording settings for the full PDFs.

`goodput_fairness.pdf` has a panel for each K. It plots mean per-second Jain
fairness of the two foreground receivers against their aggregate application
goodput in Mbps during the two burst intervals. Each controller has a mean
marker and an ellipse when runs have nonzero spread.
The summary and per-run CSVs also give both fairness measures for each burst
and the whole measurement window (local 10–99 s), with measurement coverage.
Use `--include-omega10` to include the 10% variant and write to the separate
`experimentLeoLoadBalanceBackgroundOmega10` plot folder.

The [shared-budget variant](../experimentLoadBalanceBottleneckSharedBudget/README.md)
uses the same traffic and saved routes with a fixed aggregate ISL budget.
