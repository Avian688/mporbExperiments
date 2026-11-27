package mporbexperiments.evaluation;

import inet.common.scenario.ScenarioManager;
import inet.node.inet.Router;
import inet.node.inet.StandardHost;
import tcppaced.networklayer.configurator.ipv4.AutoNetworkConfigurator;
import ned.DatarateChannel;

network EvaluationDumbbell
{
    parameters:
        int numConnections;
        int bottlenecks;
        int rankSlots;
        object paths; // Zero-based bottleneck indices, indexed by connection and rank.
        object rttMs; // One base RTT for all ranks of each connection.
    submodules:
        client[numConnections]: StandardHost;
        server[numConnections]: StandardHost;
        left[bottlenecks]: Router {
            parameters:
                numPppInterfaces = 1 + parent.numConnections * parent.rankSlots;
        }
        right[bottlenecks]: Router {
            parameters:
                numPppInterfaces = 1 + parent.numConnections * parent.rankSlots;
        }
        configurator: AutoNetworkConfigurator;
        scenarioManager: ScenarioManager;
        recorder: EvaluationRecorder;
    connections allowunconnected:
        for b=0..bottlenecks-1 {
            left[b].pppg[0] <--> DatarateChannel { datarate = 100Mbps; delay = 0.5ms; } <--> right[b].pppg[0];
        }
        for c=0..numConnections-1, for p=0..size(get(paths,c))-1 {
            client[c].pppg++ <--> DatarateChannel { datarate = 10Gbps; delay = (get(parent.rttMs,c) - 1) * 0.25ms; }
                <--> left[get(get(paths,c),p)].pppg[1+c*rankSlots+p];
            right[get(get(paths,c),p)].pppg[1+c*rankSlots+p]
                <--> DatarateChannel { datarate = 10Gbps; delay = (get(parent.rttMs,c) - 1) * 0.25ms; } <--> server[c].pppg++;
        }
}
