// SPDX-License-Identifier: LGPL-3.0-or-later
#ifndef MPORBEXPERIMENTS_MPORBLEOOSGVISUALIZER_H_
#define MPORBEXPERIMENTS_MPORBLEOOSGVISUALIZER_H_

#include <omnetpp.h>
#include "LeoViewRoutes.h"

#include <cstdint>
#include <filesystem>
#include <string>
#include <utility>
#include <vector>

#ifdef WITH_OSG
#include <osg/Camera>
#include <osg/Geode>
#include <osg/Geometry>
#include <osg/Group>
#include <osg/Vec3>
#include <osg/ref_ptr>
#include <osgText/Text>

#include "mobility/INorad.h"
#include "networklayer/configurator/ipv4/LeoKPathSnapshot.h"

#endif

namespace inet {

class MpOrbLeoOsgVisualizer : public omnetpp::cSimpleModule
{
  private:
    omnetpp::cMessage *updateTimer = nullptr;

#ifdef WITH_OSG
    struct SnapshotFile {
        omnetpp::simtime_t time;
        std::filesystem::path path;
    };

    int numSatellites = 0;
    int numGroundStations = 0;
    int numClients = 0;
    int numUserTerminals = 0;
    int satellitesPerPlane = 0;
    int pathCount = 0;
    int displayedPathCount = 0;
    std::vector<int> selectedPairs;
    std::vector<osg::ref_ptr<osgText::Text>> pairTexts;
    omnetpp::simtime_t fixedTime;
    bool highlightAccessLinks = true;
    int maxSharedLinks = -1;
    bool edgeDisjoint = false;
    bool showInterSatelliteLinks = true;
    omnetpp::simtime_t updateInterval;
    std::string policyLabel;
    std::vector<std::pair<int32_t, int32_t>> terminalPairs;
    std::vector<SnapshotFile> snapshotFiles;

    std::vector<::INorad *> satelliteOrbits;
    std::vector<osg::Vec3d> satellitePositions;
    std::vector<std::pair<int, int>> interSatelliteLinks;
    std::vector<osg::Vec3d> groundStationPositions;
    std::vector<osg::Vec3d> userTerminalPositions;

    osg::ref_ptr<osg::Group> scene;
    osg::ref_ptr<osg::Geode> pathGeode;
    osg::ref_ptr<osg::Geode> selectedEndpointGeode;
    osg::ref_ptr<osg::Geometry> satelliteGeometry;
    // OSG triangle visitors require float render arrays; world positions above stay double.
    osg::ref_ptr<osg::Vec3Array> satelliteVertices;
    osg::ref_ptr<osg::Geometry> interSatelliteLinkGeometry;
    osg::ref_ptr<osg::Vec3Array> interSatelliteLinkVertices;
    osg::ref_ptr<osgText::Text> headerText;
    std::vector<osg::ref_ptr<osgText::Text>> rankTexts;

    void initializeScene();
    void initializeConstellation();
    void initializeInterSatelliteLinks();
    void initializeStaticNodes();
    void initializeLegend();
    void discoverSnapshots();
    void parseEndpointPairs();
    void parseSelectedPairs();
    void updateVisualization();
    void updateInterSatelliteLinks();
    int findSnapshotIndex(omnetpp::simtime_t time) const;
    osg::Vec3d positionForNode(int32_t nodeId) const;
    osg::ref_ptr<osg::Geometry> createPointGeometry(osg::Vec3Array *vertices,
                                                    const osg::Vec4& color,
                                                    float pointSize,
                                                    bool dynamic,
                                                    bool square = false) const;
    osg::ref_ptr<osg::Geometry> createLinkGeometry(const leoView::EdgeUse& edge) const;
    void updateEndpointMarkers();
    void updateLegend(const std::vector<leoRouting::KShortestPathGroup>& groups,
                      omnetpp::simtime_t snapshotTime);
    osg::ref_ptr<osg::Geometry> createEarthGeometry() const;
    static osg::Vec3d geodeticToWorld(double longitudeDegrees,
                                     double latitudeDegrees,
                                     double altitudeMeters);
    static osg::Vec4 pairColor(int index);
#endif

  protected:
    virtual int numInitStages() const override { return 2; }
    virtual void initialize(int stage) override;
    virtual void handleMessage(omnetpp::cMessage *message) override;

  public:
    virtual ~MpOrbLeoOsgVisualizer();
};

} // namespace inet

#endif
