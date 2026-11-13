# ExperimentLeoLoadBalance

This is ExperimentLeo's five-connection controller/K/start-time matrix on a
70 s replay of the original 115–185 s topology window. Local traffic starts
at the usual 1–2 s. The analysis uses local 10–69 s, preserving ExperimentLeo's
10 s warm-up and one-second end guard.

Use an OMNeT++ environment with the replay-enabled LeoSatellites library built.
The saved primary corpus must cover the prefix and window. The link-sharing
export also reads the existing EdgeDisjoint K-path corpus through 185 s.

From `simulations/pythonScripts`:

```sh
# Optional: capture and cache the topology checkpoint before running traffic.
python3 experimentLeoLoadBalance/runExperimentLeoLoadBalance.py --capture-only

# Capture if needed, run the matching unloaded ping, then simulate, extract and plot.
python3 experimentLeoLoadBalance/runExperimentLeoLoadBalance.py --cores 30

# Check one run before the whole matrix.
python3 experimentLeoLoadBalance/runExperimentLeoLoadBalance.py --protocols Omega --ks 2 --runs 1

# Regenerate plots from existing extracted results.
python3 experimentLeoLoadBalance/runExperimentLeoLoadBalance.py --start-step 4 --skip-generate --cores 30
```

Pass `--route-store PATH` on the initial command if the saves are stored outside
LeoSatellites' default `simulations/leoSaves` directory. A later launch can reuse
that generated configuration with `--skip-generate`. The checkpoint is shared
by the ping and traffic runs. Capture is topology-only and runs once per matching
input set. Each replay starts with fresh transport connections.

Results and CSVs live in this experiment directory. The matching ping baseline
has its own `experimentLeoLoadBalancePing` directory. The usual goodput and RTT
heatmaps, per-terminal plots, all-terminal comparisons and combined PDFs go
under `simulations/plots/experimentLeoLoadBalance`. Simulation vectors and plots
use local time. Link-sharing CSVs use the original route timeline.

## Fairness and efficiency

The summary includes a fairness–efficiency scatter for each K. Each controller
has one circle. For each run, compute Jain's index across the five connection
goodputs and divide their sum by the 500 Mbps combined terminal access ceiling.
Average both metrics across runs over the existing measurement window. The
access ceiling is a fixed reference across K. Shared core links and handovers
can reduce the attainable goodput. OrbCC single path supplies the same
reference point in each K plot.

To generate just these figures from existing extracted summaries, run from
the folder containing `runExperimentLeoLoadBalance.py`:

```sh
python3 runExperimentLeoLoadBalance.py --start-step 4 --skip-generate --plots fairness-efficiency
```

The output includes `fairness_efficiency_K*.pdf`, matching PNGs, and
`fairness_efficiency_data.csv` with run counts and partial-goodput coverage.
Runs without a usable rate for every connection or with zero total goodput
are omitted from the plotted averages. Add `--include-omega10` to plot the
separate Omega10 comparison when all matching summaries are available.

## Omega with 10% shuffling

Select `Omega10` to run the existing Omega controller with a 10% shuffle
fraction for K=1–5 and the same five start-time patterns. Use
`--include-omega10` to add it to the standard matrix. From
`simulations/pythonScripts`:

```sh
python3 experimentLeoLoadBalance/runExperimentLeoLoadBalance.py --protocols Omega10 --end-step 3 --cores 30
python3 experimentLeoLoadBalance/runExperimentLeoLoadBalance.py --protocols Omega10 --start-step 4 --skip-generate --plots individual --cores 30
python3 experimentLeoLoadBalance/runExperimentLeoLoadBalance.py --protocols Omega10 --start-step 4 --skip-generate --plots summary
```

Both plot commands write to `simulations/plots/experimentLeoLoadBalanceOmega10`.
The goodput and RTT heatmaps compare all seven controllers for each K using
the matching extracted runs from the standard matrix.
