# MpORB multipath experiments

This suite contains only four experiments: two-path responsiveness, responsiveness
with loss, inter-connection RTT, and intra-connection RTT. Every connection has
**two subflows**, one bound to each physical path. The controller order is
**Delta, Omega, Alpha, CUBIC uncoupled**. Delta selects `MpOrbDelta`, the
consolidated former Theta controller; Omega adds its underutilisation boost.

| Experiment | Setup | Default runs |
| --- | --- | ---: |
| 1 | One two-subflow connection; independent path RTT/bandwidth and brief outages every 15 s. | 200 |
| 2 | Experiment 1 plus independent forward packet loss on each path. | 200 |
| 3 | Two connections sharing both bottlenecks throughout; A RTTs=(50,50) ms, B=(R,R). | 220 |
| 4 | Two connections sharing both bottlenecks throughout; A=(20,20) ms, B=(20,R), with mirrored B=(R,20). | 380 |

All four controllers use **50 paired trials each in experiments 1/2**, matching
OrbTCP's responsiveness trial count. Each controller's CDF therefore contains
50 per-run averages when all trials complete. Experiments 3/4 retain **five
paired seeds per controller and RTT/layout case**. Experiment 3 sweeps
R=20,40,50,60,80,100,120,140,160,180,200 ms;
50 ms provides an equal-RTT control. Experiment 4 sweeps R=20,40,...,200 ms;
the symmetric R=20 control runs once per seed/controller, while the other nine
values have both path assignments. Total: **1,000 simulations**, not launched by
input generation. The default duration is 300 s, measuring [10 s, 299 s].

## Traffic and paths

`simulations/networks/twoPathDumbbell.ned` supplies two independent bottlenecks.
The RTT experiments have two separate sender/receiver pairs, with one subflow
from each connection on each bottleneck. Client/server PPP index p is path p.
Paths share neither core links nor access links; the two connections contend at
each core link. Access capacity is 10 Gbps, so aggregate traffic is not limited
by one common sender access link. End hosts do not forward other hosts' traffic.

Both subflows start at connection establishment and use `defaultCwnd`. Their
source interfaces and destination interface addresses are pinned to their
respective paths. Starts are independently drawn in [1 s, 2 s] per connection,
with identical draws across controllers and RTT cases. An initial 1 MiB write
enables the existing transport's persistent queue refill. Applications close
after the simulation horizon. No congestion-control code was changed.

Experiments 3/4 isolate two different effects. In inter-RTT, each connection is
internally symmetric, so the difference is between connections. In intra-RTT,
one connection has heterogeneous subflows and the other is the symmetric
reference. B's mean RTT also changes, so this test does not hold connection-mean
RTT equal. The mirrored case checks whether the conclusion depends on which
physical path has the longer RTT. Both connections compete for the full run;
there are no 100/200 s route-sharing phases. With two saturated 100 Mbps links,
100 Mbps per connection is an ideal equal-share reference, not a prediction of
application goodput or a property assumed of any controller.

## Responsiveness conditions

Each path draws bandwidth uniformly as integer 50–100 Mbps and propagation RTT
as integer 1–100 ms, initialized at t=0. At t=15,30,...,285 s, **both paths are
disconnected**. Each independently reconnects after a random 45–120 ms outage,
with its own new RTT/bandwidth. Thus both paths can be unavailable together;
this experiment measures that recovery case as well as adaptation after a step.
Only bottleneck PPP modules crash/restart; application and access interfaces stay up.

Experiment 2 adds independently sampled forward packet-error probability
0–0.01, rounded to four decimals; reverse/ACK channel error probability is zero.
RTT, bandwidth, outage timing, and transport starts match experiment 1 for every
seed. The paths use independent random streams, shared by all controllers.
One-way propagation delay is RTT/6 on each of the three physical links in both
directions. Reconnect events update channels, restart core PPP, and refresh routes.

The XML scenarios and adjacent JSON condition histories are generated under
each experiment's `scenarios/` directory. No LEO constellation, saved routes,
ground stations, or terminal-specific catalogs are involved.

## Queues and telemetry

Only forward bottleneck queues `left[0].ppp[0].queue` and
`left[1].ppp[0].queue` use PINT. Alpha and the CUBIC comparison use `PintQueue`;
Delta and Omega use `MpOrbDeltaQueue` with coefficient 0.05 and
RTT-based interval selection. Access and reverse queues are ordinary DropTail,
so they issue no shuffle. Base `PintQueue` is not changed.

PINT values/counts are exact, feedback probability is one, and the sketch is
disabled; all controllers retain the same base gains. MSS is 1200 bytes on all
controllers to leave space for TCP/MPTCP/PINT options. Bottleneck buffers stay
fixed while conditions change: 395 packets for responsiveness (75 Mbps × 50.5 ms
reference BDP), and 521 packets for RTT comparisons (100 Mbps × 50 ms). The RTT
core links contribute 1 ms propagation RTT; four access directions contribute
the remainder. Buffer size does not change with the swept RTT.

The vector set matches the enabled OrbTCP experiment 1–4 set:
`goodput`, `cwnd`, `rtt`, `srtt`, `throughput`, `mbytesInFlight`, `queueLength`,
`bandwidth`, and `retransmissionRate` (experiments 1–3, matching OrbTCP).
Exactly two multipath additions are retained: receiver `holBlockedBytes` and
sender `metaReinjectedBytes`. No U, tau, controller-specific correction/rate
vectors, scheduler internals, or other multipath counters are enabled.

TCP vectors retain separate meta/subflow connection IDs. Queue length/bandwidth
recording is restricted to the two bottleneck links' forward/reverse egresses;
access and loopback queues are excluded. Native bandwidth signals are retained
where the queue provides them; the JSON conditions remain authoritative for the
time-varying capacity used by the responsiveness comparison.

Goodput/throughput samples stay periodic at 100 ms, including repeated values
and zeros for correct integration. Other vectors use `vector(removeRepeats)`.
One `bytesRcvd` scalar per receiver validates delivery. All other statistic,
vector, scalar and histogram recording defaults to false. Application goodput
is never calculated by summing subflow throughput. The sampling does not support
precise millisecond-scale recovery-time claims.

## Running

From `/Users/av288/omnetpp-6.4.0`, with the normal built INET, TCP, MPTCP, and
MpORB libraries and the workspace Python environment:

```sh
# Generate all INIs, scenarios, and manifests; no simulations.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/experiments.py

# Complete simulation/export/extraction/plots for each experiment in order.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/runExperiments.py --experiments 1 2 3 4 --cores 30 --plots all

# Inspect a small selection without executing it.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/runExperiments.py --experiments 1 2 --runs 1 --dry-run

# Run one responsiveness configuration, then export, extract and plot it.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/experiment1/runExperiment1.py --configs Alpha_Responsiveness_Run1 --cores 1

# Run only the two RTT experiments, selecting selected RTT values and controllers.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/runExperiments.py --experiments 3 4 --protocols Delta Omega Alpha CubicUncoupled --rtts 20 50 100 200 --cores 10

# A bounded smoke check that crosses the first 15 s outage.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/experiment2/runExperiment2.py --configs Delta_ResponsivenessLoss_Run1 --sim-time 18 --cores 1

# Re-extract and plot existing full-duration exports.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/experiment3/runExperiment3.py --start-step 3 --skip-generate --cores 10

# Replot selected individual metrics without rerunning simulations.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/experiment3/runExperiment3.py --start-step 4 --skip-generate --plots cwnd rtt queueLength holBlockedBytes metaReinjectedBytes

# Standalone per-run plot command; reads duration from the extracted summary.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/experiment1/plotCwnd.py --configs Alpha_Responsiveness_Run1

# Separate no-loss/loss CDFs, using existing extracted results only.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/experiment1/plotGoodputCumulativeDistribution.py
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/experiment2/plotGoodputCumulativeDistribution.py

# RTT goodput-ratio plots, using existing extracted results only.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/experiment3/plotGoodputRatio.py
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/experiment4/plotGoodputRatio.py

# Direct plotting of particular extracted CSV files is also supported.
.venv/bin/python samples/mporbExperiments/simulations/pythonScripts/experiment1/plotQueueLength.py path/to/left_queue.csv path/to/right_queue.csv --output /tmp/bottleneck_queues
```

Steps are 1=simulate, 2=export, 3=extract, 4=plot. By default, all 50 trials per
controller are selected for experiments 1/2 and all five seeds for experiments
3/4. `--runs N` limits selection to seeds 1..N, capped at each experiment's
default; `--runs 50` never increases RTT experiments beyond five seeds.
`--protocols`, `--configs`, `--rtts`, and `--experiments` restrict the matrix.
Selected experiments run in ascending order. Each completes all selected steps
before the next starts: experiment 1 simulate → export → extract → plot, then
experiment 2, then 3, then 4. Runs within an experiment use the requested worker
count. Aggregate and requested individual plots are available before the next
experiment's simulations start. `--start-step` / `--end-step` apply separately
to each experiment, and failures stop later experiments while keeping completed
outputs. The console identifies each experiment and stage.
`--skip-generate` preserves manual input edits. Simulations resume automatically
only when configuration, input content, library metadata, and result-file
metadata still match the completion marker. `--rerun` forces selected repeats.
The runner never builds libraries. The project adds no C++ model, so its empty
project library is not needed by the command-line runner.

`--sim-time` supports shorter smoke runs, and is part of completion metadata;
an 18 s result is not reused for a 300 s run. Keep the same duration when
resuming later processing steps. Extraction needs a nonempty window after 10 s;
shorter checks can stop at step 1. Five-hour per-simulation timeout and one retry
are defaults; failed processes are not treated as zero-goodput successes.
`--cores` bounds simulation/export workers, and extraction workers subject to
the RAM budget. CSV-R extraction spools bounded chunks, retains original
connection/thread IDs, and uses disk-backed arrays. Generic streaming/resource
helpers were copied from the existing MPTCP suite; runtime analysis does not
import another experiment's configurations.

## Outputs

Each experiment has `results/`, `csvs/raw/`, and `csvs/extracted/<config>/`.
Extracted runs include the retained per-module vectors, `connections.csv`,
`timeseries.csv`, and `summary.json`; `csvs/summary.csv` covers selected runs.
Missing receiver counters cause extraction failure. A late first goodput sample
leaves the steady-window mean missing. All-zero goodput has undefined fairness.

Experiments 1/2 have **two separate CDF figures**, one without loss and one with
loss: `simulations/plots/experiment1/goodput_cdf.png` / `.pdf` and
`simulations/plots/experiment2/goodput_cdf.png` / `.pdf`. All controllers appear
on each figure with consistent colours. Each observation is **one run's
time-weighted average application goodput** over the measurement window. The
empirical CDF uses the actual number of valid runs in each curve. Its black
capacity reference is the time-weighted sum of both paths' raw capacities,
including outages, once per seed. This reference does not model protocol
overhead or loss. Each directory's `plot_data.csv` contains trial-level means
and inclusion flags; `capacity_reference.csv` contains the reference observations.
Each experiment plots independently as it finishes, using only its own trials.
The earlier combined `experiment1and2/joined_goodput_cdf.*` outputs are retired
when a new CDF is generated.

Plot presentation follows the OrbTCP scripts: nominal **5 × 2 inch CDFs**, with
`Average Goodput (Mbps)` / `% of Trials`, and **4.5 × 1.2 inch RTT ratio plots**,
with `RTT (ms)` / `Goodput Ratio`. Aggregate titles are concise: Responsiveness,
Responsiveness with Loss, Inter-RTT Fairness, and Intra-RTT Fairness (aligned or
mirrored). Serif fonts, inward ticks, thin lines/error bars, and controller
markers follow OrbTCP's scientific style; controller colours stay consistent.
The shared style is self-contained, following the
[SciencePlots base settings](https://github.com/garrettj403/SciencePlots/blob/master/src/scienceplots/styles/science.mplstyle).
It uses LaTeX when available and falls back to Matplotlib's native serif text,
so plotting requires no new package or mandatory TeX installation. Aggregate
exports use tight bounds, with room for titles and legends.

Experiments 3/4 plot **min(A goodput, B goodput) / max(A goodput, B goodput)**
against swept RTT, using each connection's application goodput across both
subflows. As in OrbTCP, ratios are calculated from aligned samples before
averaging, rather than dividing the overall mean goodputs. Samples where both
connections are idle are excluded; a sample with only one idle connection has
ratio zero. Each run contributes its mean ratio over the measurement window;
points/error bars show the mean/sample SD across seeds. A ratio of one denotes
equal goodput. These figures use continuous competition with no phase split.
Outputs are `simulations/plots/experiment3/inter_rtt.*` and
`simulations/plots/experiment4/intra_rtt_aligned.*` / `intra_rtt_mirrored.*`.
The equal-RTT control appears in both intra-RTT layouts without extra simulations.
Each RTT directory includes aggregate `plot_data.csv` with actual run counts
and `ratio_runs.csv` with individual run means/sample counts. Undefined results
are omitted from statistics, rather than treated as zero or perfect fairness.

Each experiment directory under `pythonScripts/` has standalone `plotCwnd.py`,
`plotRtt.py`, `plotSrtt.py`, `plotQueueLength.py`, `plotGoodput.py`,
`plotThroughput.py`, `plotBytesInFlight.py`, `plotBandwidth.py`,
`plotHolBlocking.py`, and `plotReinjectedBytes.py`. Experiments 1–3 also have
`plotRetransmissions.py`. They support the same controller/run/config/RTT
selection and never launch simulations. `plotExperimentN.py` runs the summary
plotting stage. The main runner's `--plots all` includes summary and all retained
individual metrics; default plotting remains the summary comparison.
The standalone CDF/ratio commands also support those selections and infer the
duration from extracted summaries. Use `--sim-time` to select one duration when
both smoke and full-duration results exist; different windows are never pooled.

Individual PNG/PDF figures are written to
`simulations/plots/experimentN/individual/<config>/<metric>.*`, retaining each
connection/thread as a separate curve. Bounded time-bin reduction preserves
first/last values and extrema when plotting long vectors. Missing/no-event
vectors are labelled as having no recorded samples, rather than invented zeros.
Individual traces use OrbTCP's **17 × 5 inch** layout, short metric/controller
titles, upper-left legends, and grids. CWND and bytes in flight are displayed
in **MSS units using this suite's 1200-byte MSS**; extracted vectors remain in
their original units. Responsiveness goodput traces also show the summed
available bandwidth of both paths, with zero contribution during outages.
PNG exports use 300 dpi and PDFs retain vector curves. Styling does not change
the trial averages, CDF construction, capacity reference, or RTT statistics.

Eighteen tests cover matrix membership, experiment-specific trial counts and
CLI/plot selections, generation/pairing of all 50 responsiveness scenarios,
independent/matched path conditions,
outage/loss XML, RTT arithmetic/queue scope, the exact recording set,
application-only goodput accounting, bounded individual plotting, trial-level
CDF counts, capacity integration, aligned goodput ratios, selected result
freshness, separate loss/no-loss CDFs, per-experiment stage ordering, resumed
processing, and stopping after failure while retaining earlier plots.
Two additional 18 s checks with the final recording set completed simulation,
export, extraction, summary plots, and all 22 individual metric figures. Their
raw vectors contain only the requested metrics; queue vectors come only from
bottleneck queues. These bounded runs are not full-duration controller comparisons.
The complete 1,000-run matrix has not been executed.
