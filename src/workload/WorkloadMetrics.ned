// SPDX-License-Identifier: LGPL-3.0-or-later
package mporbexperiments.workload;

simple WorkloadMetrics
{
    parameters:
        string outputFile = default("workload-metrics.json");
        string diagnosticDir = default(""); // Empty disables the endpoint diagnostic CSVs.
        string diagnosticFlowIds = default("");
        @class(mporbexperiments::WorkloadMetrics);
}
