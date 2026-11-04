package mporbexperiments.visualizer;
@namespace(inet);

import leosatellites.visualizer.osg.LeoKPathOsgVisualizer;

// Reuse the parameter contract, with an independent local C++ implementation.
simple MpOrbLeoOsgVisualizer extends LeoKPathOsgVisualizer
{
    parameters:
        @class(MpOrbLeoOsgVisualizer);
        int displayedK = default(5);
        string selectedPairIndices = default("5 6 7 8 9");
        double snapshotTime @unit(s) = default(-1s); // negative: animate the full replay
        bool highlightAccessLinks = default(true);
        showInterSatelliteLinks = default(false);
}
