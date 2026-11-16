# ExperimentLeoLoadBalanceBackground

Two backlogged foreground connections run between co-located New York and
London user terminals, with an independent access link for each terminal. Both
foreground connections run the controller under test. The third pair runs five
concurrent single-path OrbCC background flows on saved rank 1 during local
20–40 s and 60–80 s. Each flow offers 60 Mbps and has its own TCP port pair.
The five flows share the background pair's access links. The sender opens
each session 0.5 s early and stops it at the burst end. The receiver's
recorded goodput shows the delivered burst rate.
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

At K=2, the burst limits A's private rank 1. B can move traffic from shared
rank 2 to rank 3. Compare controllers at a fixed K. B changes its first route
when moving from K=1 to K=2. At K=3–5, the additional ranks are shared across
connections and edge-disjoint within each. The runner keeps the requested K
when a route is unavailable and reports the available duration of each rank
in `route_availability.json`, including reconnects. Five configured subflows
require six distinct core routes to be fully usable for both foreground pairs.

The default matrix has 130 runs. MPORB Alpha, Delta, Omega, uncoupled OrbCC,
and uncoupled CUBIC each run at K=1–5 with five start-time patterns. Native
single-path OrbCC supplies five reference runs on rank 1 using the same burst.
All runs use simulation seed 1. Omega with 10% shuffling is opt-in.

## Run

Rebuild LeoSatellites so it supports `kPathTcpRankMap`, and use the existing
OMNeT++ environment and built experiment dependencies. Run these commands
from the folder containing `runExperimentLeoLoadBalanceBackground.py`.

```sh
# Prepare, simulate, extract and plot the whole matrix.
python3 runExperimentLeoLoadBalanceBackground.py --cores 30

# Pilot one run per controller at K=2.
python3 runExperimentLeoLoadBalanceBackground.py --cores 2 --ks 2 --runs 1 --protocols Omega OrbUncoupled

# Regenerate only the plots from existing CSVs.
python3 runExperimentLeoLoadBalanceBackground.py --start-step 4 --skip-generate
```

The first step loads the existing primary core saves and generates a separate
`LeoLoadBalanceBackground` endpoint catalog in an unloaded run. It reuses the
catalog on matching subsequent runs. The primary saves must cover the original
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

Receiver goodput CSVs include A, B and the background flows. Figures are stored
under `simulations/plots/experimentLeoLoadBalanceBackground`. The K-specific
fairness–efficiency plot averages per-second Jain fairness across the two
foreground receivers during the two burst intervals. Efficiency is their
aggregate application goodput divided by the 200 Mbps combined access ceiling.
Each controller has a mean marker and an ellipse when runs have nonzero spread.
The summary and per-run CSVs also give both fairness measures for each burst
and the whole measurement window (local 10–99 s), with measurement coverage.
Use `--include-omega10` to include the 10% variant and write to the separate
`experimentLeoLoadBalanceBackgroundOmega10` plot folder.
