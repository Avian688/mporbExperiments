package mporbexperiments.simulations.experiments.experimentTestBug;

import inet.common.scenario.ScenarioManager;
import inet.node.inet.Router;
import inet.node.inet.StandardHost;
import ned.DatarateChannel;
import tcppaced.networklayer.configurator.ipv4.AutoNetworkConfigurator;

network ThreePathBug
{
    types:
        channel Core extends DatarateChannel
        {
            datarate = 100Mbps;
            delay = 25ms;
        }
        channel Access extends DatarateChannel
        {
            datarate = 10Gbps;
        }
    submodules:
        client[2]: StandardHost { @display("p=80,160,column,180"); }
        server[2]: StandardHost { @display("p=720,160,column,180"); }
        left[3]: Router { @display("p=280,100,column,120"); }
        right[3]: Router { @display("p=520,100,column,120"); }
        configurator: AutoNetworkConfigurator { @display("p=350,425"); }
        scenarioManager: ScenarioManager { @display("p=450,425"); }
    connections:
        // Core links use router PPP 0. The three-subflow connection uses endpoint PPP 0–2.
        for p=0..2 {
            left[p].pppg++ <--> Core <--> right[p].pppg++;
            client[0].pppg++ <--> Access <--> left[p].pppg++;
            right[p].pppg++ <--> Access <--> server[0].pppg++;
        }
        // The competing single-subflow connection shares only SF3's core link.
        client[1].pppg++ <--> Access <--> left[2].pppg++;
        right[2].pppg++ <--> Access <--> server[1].pppg++;
}
