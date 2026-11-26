// SPDX-License-Identifier: LGPL-3.0-or-later
package mporbexperiments.workload;

simple WorkloadMetrics
{
    parameters:
        string outputFile = default("workload-metrics.json");
        string diagnosticDir = default(""); // Empty disables the endpoint diagnostic CSVs.
        string diagnosticFlowIds = default("");
        bool diagnosticDetailedSender = default(false);
        bool diagnosticWindowUpdates = default(false);
        string hotspotPairs = default(""); // Whitespace-separated server and client terminal indices.
        double hotspotStart @unit(s) = default(0s);
        double hotspotEnd @unit(s) = default(0s);
        int hotspotRanks = default(0);
        @class(mporbexperiments::WorkloadMetrics);
}
