# ExperimentLeo

Five continuously backlogged US-to-UK connections run **simultaneously** for
300 seconds on the shared LeoSatellites constellation. The measurement window
is 10–299 seconds. The fixed pairs are:

| Source terminal | Destination terminal | Shared terminal IDs |
| --- | --- | --- |
| Newark | Bristol | 5 → 10 |
| Jersey City | Cardiff | 6 → 11 |
| Yonkers | Manchester | 7 → 12 |
| Stamford | Edinburgh | 8 → 13 |
| White Plains | Belfast | 9 → 14 |

The layout, coordinates, gateway order and all ten saved endpoint-pair groups
come directly from `leosatellites`' shared routing generator. The other five
terminals remain in the network to preserve the saved layout but run no apps.
Generating this experiment changes no LeoSatellites source or saved routes.

## Matrix

| Controller | Transport | Requested subflows |
| --- | --- | --- |
| MPORB Alpha | `MpOrbSemiCoupledAlpha` | 1–5 |
| MPORB Delta | `MpOrbDelta` | 1–5 |
| MPORB Omega | `MpOrbOmega` | 1–5 |
| CUBIC uncoupled | `MpTcpMetaCubic` | 1–5 |
| OrbCC uncoupled | `MpOrbUncoupled` | 1–5 |
| OrbCC single path | Native `OrbtcpPintFlavour`, without MPTCP | 1 |

There are **five start-time runs**, with each connection starting between 1 and
2 seconds. These patterns are identical across controllers and subflow counts.
The simulation seed is fixed at 1; it is not varied across runs. The Python RNG
only prepares reproducible start-time patterns.

There are **130 traffic simulations** (125 multipath + five single-path) and
**one unloaded ping simulation**. The single-path result is calculated once per
start-time run and reused as the comparison row in every K heatmap.

Multipath uses only the saved **EdgeDisjoint** catalog. The first K ranks of its
ten-path catalog are selected with source/server ports `4000+rank`/`1000+rank`.
All subflows are created at the start; the scheduler is `defaultCwnd`. When fewer
than K paths exist, K remains requested; unavailable ranks drop traffic under
the existing LEO routing behaviour. No path duplication or automatic fallback
is added. Core links are edge-disjoint within each connection; the source and
destination access links are shared by that connection's subflows. Different
connections may compete on common links. Each access link is 100 Mbps, so the
number of subflows does not multiply an individual terminal's access capacity.

The native single-path comparator uses primary shortest-path forwarding. A K=1
multipath run uses the first rank of the saved edge-disjoint catalog; the paths
can differ because the catalog was optimised as a disjoint set. No independent
K-path recomputation is performed for smaller K.

Queues are PINT with 300-packet capacity and the shared 100 Mbps links. Delta
and Omega use `MpOrbDeltaQueue` with shuffle fraction 0.05. This configures
telemetry but **does not record network-internal data**. Other settings match
the existing MPORB suite: MSS 1200, PINT 8-bit encoding, normal OrbCC pacing,
AI 0.05, eta 0.95, persistent traffic, timestamps and SACK.

## Minimal recording and separate baseline

The main simulations contain **only TCP apps**. They record exactly:

- Receiving application goodput at terminals 10–14, every second.
- Sender TCP RTT at terminals 5–9.
- Sender congestion window (`cwnd`, in bytes) at terminals 5–9, including the
  individual subflows. `vector(removeRepeats)` preserves changes while removing
  consecutive duplicate values.
- Sender congestion metric `U` at terminals 5–9, where supplied by the OrbCC/
  MPORB controller; CUBIC does not emit this metric.
- Receiver meta-connection `holBlockedBytes` at terminals 10–14, in bytes,
  for the multipath transports. Native single-path OrbCC has no MPTCP HoL metric.
- Per-subflow receiver `throughput` at terminals 10–14, every second. This
  retains received transport traffic, including duplicates, separately from
  application goodput.
- Sender `retransmissionRate` at terminals 5–9, in bits/s, every second.
- Sender `mbytesInFlight` at terminals 5–9: outstanding transport data, in bytes.
- Sender meta-connection `metaReinjectedBytes` at terminals 5–9: cumulative
  MPTCP reinjected bytes. Native single-path OrbCC does not emit this metric.
- Sender `numRtos` at terminals 5–9: cumulative retransmission-timeout count.

CWND, U, HoL, bytes-in-flight and cumulative-counter vectors remove consecutive
repeats. Periodic throughput
and retransmission-rate vectors retain repeats so flat and zero-rate intervals
remain explicit.

No satellite, ground-station, queue, link, or other diagnostic
vectors are enabled. Receiver CWND and sender throughput are disabled. Unused connection-template recording and all scalar/bin
recording are disabled. No ping or monitoring apps run during traffic tests.
The diagnostics are retained in the original result vectors and plotted as
individual run figures; the aggregate heatmaps still use goodput and RTT. Results recorded
before a metric was enabled must be rerun to obtain that metric.

`experimentLeoPing` runs first, once, using the same saved geometry and fixed
seed. It sends small, staggered 64-byte probes every 100 ms for each of the five
pairs: ordinary shortest-path ping plus ranks 1–10 of the EdgeDisjoint catalog.
Only RTT and catalog availability are recorded, at those source terminals.
This prepass supplies both unloaded delay baselines and path availability for
all traffic runs. Availability depends on the replayed topology, not traffic;
its 100 ms samples describe catalog coverage rather than guaranteeing packet
delivery or capturing every shorter handover outage.
The legacy parameter `kPathPingRouting=true` also enables the TCP path selector;
that parameter alone creates no ping app or packet.

## Heatmaps

Each requested K produces **two heatmaps**, with the six controllers as rows
and the five terminal pairs as columns:

- `goodput_K1` … `goodput_K5`: numbers and colours are mean receiver goodput in Mbps.
- `delay_K1` … `delay_K5`: numbers are **raw RTT in ms**; green-to-red colours
  encode **excess RTT above the matching unloaded ping baseline**, also in ms.

Plots are written as PNG and PDF under `simulations/plots/experimentLeo/`.
All K figures use the same colour scale for a given metric. Each cell averages
the selected start-time runs. `heatmap_data.csv` includes sample SD, actual run
counts, RTT coverage and path availability; missing cells are never replaced
with zero. Raw RTT is still labelled when baseline matching is inadequate, but
its cell is grey with an asterisk rather than coloured as low delay.

Incomplete goodput intervals do not stop extraction. Goodput integrates only
recorded intervals and divides by the full 10–299 s measurement duration, as in
the other experiments; rates are not extrapolated before the first sample or
after the last. Thus unrecorded boundary time contributes no recorded traffic,
rather than increasing the mean by shortening the denominator. Coverage is
reported as `goodput_observed_pct` and `goodput_complete`; partial cells have an
asterisk. A wholly absent goodput recording remains missing, distinct from a
recorded zero rate. Per-thread bounds and contributions are written to
`*_goodput_coverage.csv` and the summary JSON for diagnosis.

RTT samples are averaged within one-second bins per sender subflow, then across
observed subflows in each second, then across observed seconds. Baseline matching
uses the same pair, path rank and time bin. The single-path row uses ordinary
shortest-path ping. A colour requires at least 95% of observed subflow/time bins
to have a baseline in every included run. Signed excess values are retained in
CSV; colours put zero and negative excess at the green end. Excess includes
packet-size/serialization and time-binning effects as well as queueing; it is
not a pure queue-delay measurement. No RTT is extrapolated through outages.

Sender rank identity uses the active-side construction order: one meta
connection followed by K synchronously created slots in ascending order. Empty
registered RTT vectors remain in the identity check. The extractor requires
exactly K+1 sender RTT vectors and fails if this contract changes. It never
assigns ranks using receiver arrival order or observed RTT magnitude. Per-run
`*_subflows.csv` files expose the mapping and coverage.

## Running

From `mporbExperiments/simulations/pythonScripts`:

```sh
# Inspect the 130 traffic cases and single prepass; never simulate.
python3 experimentLeo/runExperimentLeo.py --dry-run

# Check saved snapshot headers and complete time coverage; never simulate.
python3 experimentLeo/runExperimentLeo.py --check-routes

# Ping first, then traffic, extraction, heatmaps and terminal plots.
python3 experimentLeo/runExperimentLeo.py --cores 30

# Only the unloaded ping prepass.
python3 experimentLeoPing/runExperimentLeoPing.py

# Smaller selected traffic matrix; still five simultaneous connections per run.
python3 experimentLeo/runExperimentLeo.py --protocols Alpha Delta --ks 2 4 --runs 2 --cores 4

# Re-extract/replot existing outputs, or only replot.
python3 experimentLeo/runExperimentLeo.py --start-step 3 --skip-generate
python3 experimentLeo/runExperimentLeo.py --start-step 4 --skip-generate --cores 30 --plots all

# Only individual traces; no heatmap extraction or baseline is needed.
python3 experimentLeo/plotExperimentLeo.py --cores 30

# One run and selected metrics; reads the recorded vectors directly.
python3 experimentLeo/plotExperimentLeo.py --configs Delta_EdgeDisjoint_K3_Run1 --plots cwnd U holBlockedBytes

# Aggregate heatmaps only.
python3 experimentLeo/runExperimentLeo.py --start-step 4 --skip-generate --plots summary

# Explicitly replace the selected simulations and dependent plots.
python3 experimentLeo/runExperimentLeo.py --rerun --cores 30
```

Stages are 1: simulate/extract the ping prepass, 2: simulate traffic,
3: extract traffic, 4: plot. The default `--plots all` creates the aggregate
heatmaps and individual figures; named metrics create only those individual
figures. `--protocols`, `--ks`, `--runs`, and `--configs` restrict the selected
cases. Traffic extraction uses up to `--cores` separate worker processes, one
run per process, so Python sample parsing can use multiple CPU cores. The
single unloaded-ping baseline is checked/extracted first, before those workers
start. Each simulation attempt, including the ping prepass,
has a **12-hour wall-clock timeout** by default. Override it with
`--sim-timeout-seconds`; the timeout applies per attempt, not to the whole suite.
An already-running runner retains the timeout with which it was launched.
The runner normally reuses successful simulations
whose input, library and saved-route fingerprints match. Output names are fixed;
reruns and retries replace their selected files rather than adding timestamped
copies. Step 2 refuses a baseline from different routes or inputs. Step 3/4
refuses missing/stale extraction. There are no large CSV-R intermediate exports:
selected vector blocks are read directly through their `.vci` index, with small
summary CSV/JSON files under each experiment's `csvs/` directory.

## Individual user-terminal figures

Each recorded metric has an independent figure for its user terminal under
`simulations/plots/experimentLeo/individual/<configuration>/userTerminal<ID>_<city>/<metric>.png`
and `.pdf`. Each terminal directory also has **one `combined.pdf`**, a single-page
grid containing all selected, recorded metrics for that terminal and run.
Sender terminals 5–9 have sender diagnostics; receiving terminals 10–14 have
goodput, throughput and HoL plots. Metrics from different terminals are never
combined in one figure. The ten supported metrics are goodput, RTT, CWND, U, HoL blocked bytes,
receiver throughput, sender retransmission rate, bytes in flight, cumulative
MPTCP reinjected bytes, and RTO count. Figures use the existing experiments'
17-by-5-inch trace style for each independent plot. Sender
subflow curves identify their path rank and connection ID. Receiver throughput
curves retain their connection IDs; passive-side IDs are not assumed to be
path-rank order. CWND and bytes in flight are displayed in MSS units (1200 B),
RTT in ms, rates in Mbps, and the MPTCP buffer/reinjection measurements in bytes.

Plots read the existing `.vec`/`.vci` files directly, including diagnostics that
are absent from the summary CSVs. No new simulation or full CSV-R export is
needed when those metrics were already recorded. Plotting uses up to `--cores`
worker processes, one run per job, because Matplotlib rendering is not thread
safe. Output names are fixed and are overwritten on replotting.

Dense traces retain the first, last, minimum and maximum sample in each of
3000 time bins, preserving spikes with bounded plotting memory. Aggregate
statistics continue to use the original samples. State traces hold their last
recorded state; rates stop at the last recorded interval. RTT is plotted as
observations rather than extended through unobserved periods. Registered empty
cumulative RTO/reinjection counters represent zero events; absent recordings
remain missing. U is inapplicable to CUBIC, and native single-path OrbCC has no
MPTCP HoL/reinjection metrics; these figures are skipped.

The per-run `vectors.csv` index lists source modules, ranks, raw/retained point
counts, displayed curves, and missing/not-applicable recordings. `plots.json`
records the source result's size/mtime and output list. A combined index is
written to `individual/vectors.csv`. Other missing diagnostics are skipped or
labelled in their figure rather than substituted with zero. RTO and
retransmission curves also show meta/control-connection events when nonzero.

This is an explicit separate suite; `runExperiments.py` still defaults to the
existing experiments 1–4. Build INET, OS3, LeoSatellites and the transport
dependencies before simulating. No build is performed by this runner.

## Route preparation

The default shared store is `leosatellites/simulations/leoSaves`. A relocated
corpus can be selected with `--route-store /path/to/leoSaves`. Required stores:

```text
1584_550_72_22_53_100_ISL/
1584_550_72_22_53_100_ISL/kpaths/SharedLeoTenPairs/
  v3-edge-disjoint-mincost-k10-rtt1000ms-pairs-c7a3ba1aa7e3e1c7/
```

If the saves are missing, generate them using the shared saver (also from the
Python scripts directory):

```sh
python3 ../../../leosatellites/simulations/pythonScripts/routingExamples/saveRoutes.py --mode multiple --policies EdgeDisjoint --sim-time 300 --cores 1
```

If matching primary saves already cover the duration, add `--start-step 2` to
generate only the missing EdgeDisjoint catalog. Saving can overwrite that
generator's existing route set; do not run concurrent writers for the same
corpus. The ExperimentLeo runner never generates or overwrites route saves.

The preflight checks each expected timestamp/header and the K-policy/profile.
Full payload/hash, core geometry and endpoint compatibility are validated by
the existing simulator loader. A profile JSON alone is not evidence that saves
were generated. The present checkout has no shared route corpus; full LEO
simulation validation therefore remains outstanding.

Static/synthetic regression checks, from the project root:

```sh
python3 tests/test_experiment_leo.py
```

These checks do not launch simulations.
