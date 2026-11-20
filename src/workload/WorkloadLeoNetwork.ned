package mporbexperiments.workload;

import leosatellites.simulations.examples.routing.LeoRoutingNetwork;

network WorkloadLeoNetwork extends LeoRoutingNetwork
{
    submodules:
        workloadMetrics: WorkloadMetrics;
}
