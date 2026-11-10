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
