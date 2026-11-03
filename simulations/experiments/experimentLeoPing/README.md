# ExperimentLeoPing

One unloaded prepass supplies ordinary shortest-path and all ten saved
EdgeDisjoint-rank RTT baselines for the five NY/UK pairs. It also records catalog
availability. This result is reused across the traffic runs; no ping apps run
in ExperimentLeo itself.

See [ExperimentLeo](../experimentLeo/README.md) for route requirements, the
full pipeline, recording policy and heatmap definitions. From `pythonScripts`:

```sh
python3 experimentLeoPing/runExperimentLeoPing.py
```
