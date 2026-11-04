package mporbexperiments.visualizer;

import leosatellites.simulations.examples.routing.KPathOsgSatellite;
import leosatellites.simulations.examples.routing.KPathOsgGeoNode;
import mporbexperiments.visualizer.MpOrbLeoOsgVisualizer;

network ExperimentLeoOsg
{
    parameters:
        int numOfSats = default(1584);
        int numOfGS = default(100);
        int numOfClients = default(0);
        int numOfUserTerminals = default(15);
        double incl = default(53);
        double alt = default(550);
        int satsPerPlane = default(22);
        int numOfPlanes = default(72);
        @display("bgb=1200,700");
    submodules:
        satellite[numOfSats]: KPathOsgSatellite {
            parameters:
                satIndex = index;
                altitude = parent.alt;
                inclination = parent.incl * 0.0174532925199433;
                planes = parent.numOfPlanes;
                satPerPlane = parent.satsPerPlane;
        }
        groundStation[numOfGS]: KPathOsgGeoNode;
        userTerminal[numOfUserTerminals]: KPathOsgGeoNode;
        pathVisualizer: MpOrbLeoOsgVisualizer {
            parameters:
                numSatellites = parent.numOfSats;
                numGroundStations = parent.numOfGS;
                numClients = parent.numOfClients;
                numUserTerminals = parent.numOfUserTerminals;
                satellitesPerPlane = parent.satsPerPlane;
        }
    connections allowunconnected:
}
