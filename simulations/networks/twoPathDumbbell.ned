package mporbexperiments.simulations.networks;

import inet.node.inet.StandardHost;
import inet.node.inet.Router;
import inet.common.scenario.ScenarioManager;
import tcppaced.networklayer.configurator.ipv4.AutoNetworkConfigurator;
import ned.DatarateChannel;

network twoPathDumbbell
{
    parameters:
        int numberOfConnections = default(1);
        @display("bgb=900,500");
    submodules:
        client[numberOfConnections]: StandardHost { @display("p=80,120,column,120"); }
        server[numberOfConnections]: StandardHost { @display("p=820,120,column,120"); }
        left[2]: Router { @display("p=320,100,column,230"); }
        right[2]: Router { @display("p=580,100,column,230"); }
        configurator: AutoNetworkConfigurator { @display("p=400,440"); }
        scenarioManager: ScenarioManager { @display("p=520,440"); }
    connections:
        // Allocate core PPP 0 first. Client/server PPP p is always path p.
        for p=0..1 {
            left[p].pppg++ <--> DatarateChannel { datarate = 100Mbps; delay = 0.5ms; } <--> right[p].pppg++;
        }
        for p=0..1, for c=0..numberOfConnections-1 {
            client[c].pppg++ <--> DatarateChannel { datarate = 10Gbps; delay = 0ms; } <--> left[p].pppg++;
            right[p].pppg++ <--> DatarateChannel { datarate = 10Gbps; delay = 0ms; } <--> server[c].pppg++;
        }
}
