package mporbexperiments.workload;

import inet.applications.contract.IApp;

simple WorkloadEndpoint like IApp
{
    parameters:
        @class(mporbexperiments::WorkloadEndpoint);
        string workloadFile;
        string metricsModule = default("^.^.workloadMetrics");
        int activePortBase = default(33000);
        int passivePortBase = default(1000);
        int portStride = default(6);
        bool fullMssPadding = default(false);
    gates:
        input socketIn;
        output socketOut;
}
