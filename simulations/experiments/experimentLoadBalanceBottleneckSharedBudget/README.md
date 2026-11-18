# ExperimentLoadBalanceBottleneckSharedBudget

This variant uses the same traffic, 400 Mbps access links, and saved routes
as [ExperimentLoadBalanceBottleneck](../experimentLoadBalanceBottleneck/README.md).
The shared ranks split a 100 Mbps budget equally. Their configured rates are
100, 50, 33⅓, and 25 Mbps per rank for K=2, 3, 4, and 5 respectively. Private
ranks keep their 100 Mbps core links.

The rate cap applies on the first directed ISL of each shared rank on the
New York–London route. It follows the saved paths at topology updates and
terminal reconnects. An unavailable rank leaves its share unused. The K=1
baseline uses its ordinary 100 Mbps core route.

From the folder containing `runExperimentLoadBalanceBottleneckSharedBudget.py`:

```sh
python3 runExperimentLoadBalanceBottleneckSharedBudget.py --cores 30
```

Results, CSVs, and plots use `experimentLoadBalanceBottleneckSharedBudget` as
their experiment directory name. Plot settings and rerun options are the same
as in the per-rank version.
