// SPDX-License-Identifier: LGPL-3.0-or-later
package mporbexperiments.workload;

simple WorkloadMetrics
{
    parameters:
        string outputFile = default("workload-metrics.json");
        @class(mporbexperiments::WorkloadMetrics);
}
