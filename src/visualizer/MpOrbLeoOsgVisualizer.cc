// SPDX-License-Identifier: LGPL-3.0-or-later
// Scene setup adapted from LeoSatellites; all extensions live in this project.
#include "MpOrbLeoOsgVisualizer.h"

#include <algorithm>
#include <cmath>
#include <ctime>
#include <set>
#include <iomanip>
#include <sstream>
#include <stdexcept>

#ifdef WITH_OSG
#include <osg/LineWidth>
#include <osg/Matrix>
#include <osg/Point>
#include <osg/PrimitiveSet>
#include <osg/StateSet>
#include <osg/Texture2D>
#include <osgDB/ReadFile>
#endif

namespace inet {

Define_Module(MpOrbLeoOsgVisualizer);

MpOrbLeoOsgVisualizer::~MpOrbLeoOsgVisualizer()
{
    if (updateTimer != nullptr)
        cancelAndDelete(updateTimer);
}

void MpOrbLeoOsgVisualizer::initialize(int stage)
{
#ifndef WITH_OSG
    if (stage == 0)
        throw omnetpp::cRuntimeError(
            "MpOrbLeoOsgVisualizer requires an OMNeT++ build with WITH_OSG=yes");
#else
    if (stage == 0) {
        numSatellites = par("numSatellites");
        numGroundStations = par("numGroundStations");
        numClients = par("numClients");
        numUserTerminals = par("numUserTerminals");
        satellitesPerPlane = par("satellitesPerPlane");
        pathCount = par("pathCount");
        fixedTime = par("snapshotTime");
        highlightAccessLinks = par("highlightAccessLinks");
        maxSharedLinks = par("maxSharedLinks");
        edgeDisjoint = par("edgeDisjoint");
        showInterSatelliteLinks = par("showInterSatelliteLinks");
        updateInterval = par("updateInterval");
        policyLabel = par("policyLabel").stdstringValue();

        if (pathCount < 1 || pathCount > 10)
            throw omnetpp::cRuntimeError("pathCount must be between 1 and 10");
        // Catalog identity stays at ten even when displaying a smaller prefix.
        displayedPathCount = par("displayedK");
        if (displayedPathCount < 1 || displayedPathCount > pathCount)
            throw omnetpp::cRuntimeError("displayedK must be between 1 and the saved catalog size %d", pathCount);
        if (showInterSatelliteLinks && satellitesPerPlane < 1)
            throw omnetpp::cRuntimeError("satellitesPerPlane must be positive when ISLs are shown");
        if (updateInterval <= omnetpp::SimTime::ZERO)
            throw omnetpp::cRuntimeError("updateInterval must be positive");
        if (edgeDisjoint && maxSharedLinks != -1)
            throw omnetpp::cRuntimeError("maxSharedLinks must be -1 for the edge-disjoint policy");

        parseEndpointPairs();
        parseSelectedPairs();

        initializeScene();
        discoverSnapshots();
    }
    else if (stage == 1) {
        initializeConstellation();
        initializeInterSatelliteLinks();
        initializeStaticNodes();
        initializeLegend();
        updateVisualization();

        if (fixedTime < omnetpp::SimTime::ZERO) {
            updateTimer = new omnetpp::cMessage("update K-path visualization");
            scheduleAt(simTime() + updateInterval, updateTimer);
        }
    }
#endif
}

void MpOrbLeoOsgVisualizer::handleMessage(omnetpp::cMessage *message)
{
#ifdef WITH_OSG
    if (message != updateTimer)
        throw omnetpp::cRuntimeError("MpOrbLeoOsgVisualizer only accepts its update timer");

    updateVisualization();
    scheduleAt(simTime() + updateInterval, updateTimer);
#else
    throw omnetpp::cRuntimeError("MpOrbLeoOsgVisualizer is unavailable without OpenSceneGraph");
#endif
}

#ifdef WITH_OSG

void MpOrbLeoOsgVisualizer::initializeScene()
{
    osg::ref_ptr<osg::Image> earthImage = osgDB::readImageFile(par("earthTexture").stringValue());
    if (!earthImage)
        throw omnetpp::cRuntimeError("Could not load Earth texture '%s'",
                                    par("earthTexture").stringValue());

    scene = new osg::Group();
    osg::ref_ptr<osg::Geode> earth = new osg::Geode();
    earth->addDrawable(createEarthGeometry());
    osg::ref_ptr<osg::Texture2D> texture = new osg::Texture2D();
    texture->setImage(earthImage);
    texture->setFilter(osg::Texture::MIN_FILTER, osg::Texture::LINEAR_MIPMAP_LINEAR);
    texture->setFilter(osg::Texture::MAG_FILTER, osg::Texture::LINEAR);
    texture->setWrap(osg::Texture::WRAP_S, osg::Texture::REPEAT);
    earth->getOrCreateStateSet()->setTextureAttributeAndModes(0, texture, osg::StateAttribute::ON);
    earth->getOrCreateStateSet()->setMode(GL_LIGHTING, osg::StateAttribute::OFF);
    scene->addChild(earth);

    osg::StateSet *stateSet = scene->getOrCreateStateSet();
    stateSet->setMode(GL_BLEND, osg::StateAttribute::ON);
    stateSet->setMode(GL_CULL_FACE, osg::StateAttribute::ON);

    omnetpp::cOsgCanvas *canvas = getParentModule()->getOsgCanvas();
    canvas->setCameraManipulatorType(omnetpp::cOsgCanvas::CAM_TRACKBALL);
    canvas->setViewerStyle(omnetpp::cOsgCanvas::STYLE_GENERIC);
    canvas->setClearColor(omnetpp::cOsgCanvas::Color("black"));
    canvas->setZNear(1000);
    canvas->setZFar(1000000000);
    const double distance = par("cameraDistance").doubleValueInUnit("m");
    osg::Vec3d eyeDirection;
    if (par("autoCenterSelectedPair")) {
        eyeDirection.set(0, 0, 0);
        for (int index : selectedPairs) {
            for (int terminal : {terminalPairs[index].first, terminalPairs[index].second}) {
                auto *node = getParentModule()->getSubmodule("userTerminal", terminal);
                osg::Vec3d direction = geodeticToWorld(node->par("longitude"), node->par("latitude"), 0);
                direction.normalize();
                eyeDirection += direction;
            }
        }
        if (eyeDirection.length2() < 1e-12)
            eyeDirection.set(1, 0, 0);
        eyeDirection.normalize();
    }
    else {
        constexpr double PI_VALUE = 3.14159265358979323846;
        const double longitude = par("cameraLongitude").doubleValue() * PI_VALUE / 180.0;
        const double latitude = par("cameraLatitude").doubleValue() * PI_VALUE / 180.0;
        eyeDirection.set(std::cos(latitude) * std::cos(longitude),
                         std::cos(latitude) * std::sin(longitude),
                         std::sin(latitude));
    }
    const omnetpp::cOsgCanvas::Vec3d eye(
        distance * eyeDirection.x(), distance * eyeDirection.y(), distance * eyeDirection.z());
    canvas->setGenericViewpoint(omnetpp::cOsgCanvas::Viewpoint(
        eye, omnetpp::cOsgCanvas::Vec3d(0, 0, 0), omnetpp::cOsgCanvas::Vec3d(0, 0, 1)));
    canvas->setScene(scene);

    getParentModule()->getCanvas()->setAnimationSpeed(par("animationSpeed").doubleValue(), this);
}

osg::ref_ptr<osg::Geometry> MpOrbLeoOsgVisualizer::createEarthGeometry() const
{
    constexpr int LATITUDE_SEGMENTS = 64;
    constexpr int LONGITUDE_SEGMENTS = 128;

    osg::ref_ptr<osg::Vec3Array> vertices = new osg::Vec3Array();
    osg::ref_ptr<osg::Vec3Array> normals = new osg::Vec3Array();
    osg::ref_ptr<osg::Vec2Array> textureCoordinates = new osg::Vec2Array();
    vertices->reserve((LATITUDE_SEGMENTS + 1) * (LONGITUDE_SEGMENTS + 1));
    normals->reserve(vertices->capacity());
    textureCoordinates->reserve(vertices->capacity());

    for (int latitudeIndex = 0; latitudeIndex <= LATITUDE_SEGMENTS; ++latitudeIndex) {
        const double latitude = -90.0 + 180.0 * latitudeIndex / LATITUDE_SEGMENTS;
        for (int longitudeIndex = 0; longitudeIndex <= LONGITUDE_SEGMENTS; ++longitudeIndex) {
            const double longitude = -180.0 + 360.0 * longitudeIndex / LONGITUDE_SEGMENTS;
            const osg::Vec3d position = geodeticToWorld(longitude, latitude, 0);
            vertices->push_back(osg::Vec3(
                static_cast<float>(position.x()),
                static_cast<float>(position.y()),
                static_cast<float>(position.z())));
            osg::Vec3d normal = position;
            normal.normalize();
            normals->push_back(osg::Vec3(normal.x(), normal.y(), normal.z()));
            textureCoordinates->push_back(osg::Vec2(
                static_cast<float>(longitudeIndex) / LONGITUDE_SEGMENTS,
                static_cast<float>(latitudeIndex) / LATITUDE_SEGMENTS));
        }
    }

    osg::ref_ptr<osg::DrawElementsUInt> indices = new osg::DrawElementsUInt(GL_TRIANGLES);
    indices->reserve(LATITUDE_SEGMENTS * LONGITUDE_SEGMENTS * 6);
    for (int latitudeIndex = 0; latitudeIndex < LATITUDE_SEGMENTS; ++latitudeIndex) {
        for (int longitudeIndex = 0; longitudeIndex < LONGITUDE_SEGMENTS; ++longitudeIndex) {
            const unsigned int lowerLeft = latitudeIndex * (LONGITUDE_SEGMENTS + 1) + longitudeIndex;
            const unsigned int lowerRight = lowerLeft + 1;
            const unsigned int upperLeft = lowerLeft + LONGITUDE_SEGMENTS + 1;
            const unsigned int upperRight = upperLeft + 1;
            indices->push_back(lowerLeft);
            indices->push_back(lowerRight);
            indices->push_back(upperRight);
            indices->push_back(lowerLeft);
            indices->push_back(upperRight);
            indices->push_back(upperLeft);
        }
    }

    osg::ref_ptr<osg::Geometry> geometry = new osg::Geometry();
    geometry->setVertexArray(vertices);
    geometry->setNormalArray(normals, osg::Array::BIND_PER_VERTEX);
    geometry->setTexCoordArray(0, textureCoordinates);
    geometry->addPrimitiveSet(indices);
    return geometry;
}

void MpOrbLeoOsgVisualizer::parseEndpointPairs()
{
    const std::string specification = par("endpointPairs").stdstringValue();
    omnetpp::cStringTokenizer tokenizer(specification.c_str(), ",");
    while (tokenizer.hasMoreTokens()) {
        const std::string token = tokenizer.nextToken();
        const size_t separator = token.find('-');
        if (separator == std::string::npos)
            throw omnetpp::cRuntimeError("Invalid endpoint pair '%s'; expected terminalIndex-terminalIndex",
                                        token.c_str());

        const int source = std::stoi(token.substr(0, separator));
        const int destination = std::stoi(token.substr(separator + 1));
        if (source < 0 || source >= numUserTerminals || destination < 0 || destination >= numUserTerminals)
            throw omnetpp::cRuntimeError("Endpoint pair '%s' refers to a missing user terminal", token.c_str());

        terminalPairs.emplace_back(source, destination);
    }
    if (terminalPairs.empty())
        throw omnetpp::cRuntimeError("endpointPairs must contain at least one pair");
}


void MpOrbLeoOsgVisualizer::parseSelectedPairs()
{
    const std::string specification = par("selectedPairIndices").stdstringValue();
    omnetpp::cStringTokenizer tokenizer(specification.c_str(), ", \t");
    std::set<int> seen;
    while (tokenizer.hasMoreTokens()) {
        const std::string token = tokenizer.nextToken();
        size_t used = 0;
        int index;
        try { index = std::stoi(token, &used); }
        catch (const std::exception&) { throw omnetpp::cRuntimeError("Invalid selected pair index '%s'", token.c_str()); }
        if (used != token.size() || index < 0 || index >= static_cast<int>(terminalPairs.size()))
            throw omnetpp::cRuntimeError("Selected pair index '%s' is outside the catalog", token.c_str());
        if (seen.insert(index).second)
            selectedPairs.push_back(index);
    }
    if (selectedPairs.empty())
        throw omnetpp::cRuntimeError("Select at least one endpoint pair");
}

void MpOrbLeoOsgVisualizer::discoverSnapshots()
{
    const int32_t endpointStart = numSatellites + numGroundStations + 2 * numClients;
    std::vector<std::pair<int32_t, int32_t>> graphPairs;
    graphPairs.reserve(terminalPairs.size());
    for (const auto& [source, destination] : terminalPairs)
        graphPairs.emplace_back(endpointStart + source, endpointStart + destination);

    const leoRouting::KPathAlgorithm algorithm =
        leoRouting::kPathAlgorithmForPolicy(edgeDisjoint, maxSharedLinks);
    const std::string profile = leoRouting::makeKPathSnapshotProfileName(
        algorithm, pathCount, par("maxRttSpread").doubleValueInUnit("ms"), maxSharedLinks, graphPairs);
    const std::filesystem::path directory =
        std::filesystem::path(par("routingDirectory").stringValue()) /
        "kpaths" / par("snapshotSet").stringValue() / profile;

    if (!std::filesystem::is_directory(directory))
        throw omnetpp::cRuntimeError(
            "K-path catalog directory '%s' does not exist; generate this policy before opening the viewer",
            directory.string().c_str());

    for (const auto& entry : std::filesystem::directory_iterator(directory)) {
        if (!entry.is_regular_file() || entry.path().extension() != ".bin")
            continue;
        try {
            const std::string stem = entry.path().stem().string();
            snapshotFiles.push_back({omnetpp::SimTime::parse(stem.c_str()), entry.path()});
        }
        catch (const std::exception&) {
            EV_WARN << "Ignoring K-path file with a non-time filename: " << entry.path() << std::endl;
        }
    }

    std::sort(snapshotFiles.begin(), snapshotFiles.end(), [](const SnapshotFile& left, const SnapshotFile& right) {
        return left.time < right.time;
    });
    if (snapshotFiles.empty())
        throw omnetpp::cRuntimeError("K-path catalog directory '%s' contains no readable snapshot filenames",
                                    directory.string().c_str());

    EV_INFO << "K-path 3D viewer loaded " << snapshotFiles.size()
            << " snapshot filenames from " << directory << std::endl;
}

void MpOrbLeoOsgVisualizer::initializeConstellation()
{
    satelliteOrbits.reserve(numSatellites);
    satellitePositions.resize(numSatellites);
    satelliteVertices = new osg::Vec3Array();
    satelliteVertices->resize(numSatellites);

    std::time_t epoch = 1619119189;
    std::tm *epochUtc = std::gmtime(&epoch);
    if (epochUtc == nullptr)
        throw omnetpp::cRuntimeError("Could not initialize the constellation epoch");

    for (int index = 0; index < numSatellites; ++index) {
        omnetpp::cModule *satellite = getParentModule()->getSubmodule("satellite", index);
        if (satellite == nullptr)
            throw omnetpp::cRuntimeError("Missing satellite[%d]", index);
        auto *orbit = omnetpp::check_and_cast<::INorad *>(satellite->getSubmodule("NoradModule"));
        orbit->setJulian(epochUtc);
        orbit->initializeMobility(omnetpp::SimTime::ZERO);
        satelliteOrbits.push_back(orbit);
    }

    if (par("showSatellites")) {
        satelliteGeometry = createPointGeometry(
            satelliteVertices, osg::Vec4(0.90f, 0.95f, 1.0f, 1.0f),
            static_cast<float>(par("satellitePointSize").doubleValue()), true);
        osg::ref_ptr<osg::Geode> satelliteGeode = new osg::Geode();
        satelliteGeode->addDrawable(satelliteGeometry);
        scene->addChild(satelliteGeode);
    }
}

void MpOrbLeoOsgVisualizer::initializeInterSatelliteLinks()
{
    if (!showInterSatelliteLinks)
        return;

    interSatelliteLinks.reserve(2 * numSatellites);
    for (int satellite = 0; satellite < numSatellites; ++satellite) {
        const int plane = satellite / satellitesPerPlane;
        int nextInPlane = (satellite + 1) % (satellitesPerPlane * (plane + 1));
        if (nextInPlane == 0)
            nextInPlane = plane * satellitesPerPlane;
        if (nextInPlane < numSatellites)
            interSatelliteLinks.emplace_back(satellite, nextInPlane);

        const int nextPlane = satellite + satellitesPerPlane;
        if (nextPlane < numSatellites)
            interSatelliteLinks.emplace_back(satellite, nextPlane);
    }

    interSatelliteLinkVertices = new osg::Vec3Array();
    interSatelliteLinkVertices->resize(2 * interSatelliteLinks.size());
    interSatelliteLinkGeometry = new osg::Geometry();
    interSatelliteLinkGeometry->setVertexArray(interSatelliteLinkVertices);
    interSatelliteLinkGeometry->addPrimitiveSet(
        new osg::DrawArrays(GL_LINES, 0, interSatelliteLinkVertices->size()));

    osg::ref_ptr<osg::Vec4Array> colors = new osg::Vec4Array();
    colors->push_back(osg::Vec4(0.55f, 0.75f, 0.95f, 1.0f));
    interSatelliteLinkGeometry->setColorArray(colors, osg::Array::BIND_OVERALL);
    interSatelliteLinkGeometry->setDataVariance(osg::Object::DYNAMIC);
    interSatelliteLinkGeometry->setUseDisplayList(false);
    interSatelliteLinkGeometry->setUseVertexBufferObjects(true);

    osg::StateSet *stateSet = interSatelliteLinkGeometry->getOrCreateStateSet();
    stateSet->setAttributeAndModes(new osg::LineWidth(1.5f), osg::StateAttribute::ON);
    stateSet->setMode(GL_LIGHTING, osg::StateAttribute::OFF | osg::StateAttribute::OVERRIDE);
    stateSet->setMode(GL_BLEND, osg::StateAttribute::ON);
    stateSet->setRenderingHint(osg::StateSet::TRANSPARENT_BIN);
    stateSet->setRenderBinDetails(10, "RenderBin");

    osg::ref_ptr<osg::Geode> geode = new osg::Geode();
    geode->addDrawable(interSatelliteLinkGeometry);
    scene->addChild(geode);
}

void MpOrbLeoOsgVisualizer::initializeStaticNodes()
{
    const double groundAltitude = par("groundMarkerAltitude").doubleValueInUnit("m");
    const double terminalAltitude = par("terminalMarkerAltitude").doubleValueInUnit("m");

    groundStationPositions.reserve(numGroundStations);
    osg::ref_ptr<osg::Vec3Array> groundVertices = new osg::Vec3Array();
    for (int index = 0; index < numGroundStations; ++index) {
        omnetpp::cModule *node = getParentModule()->getSubmodule("groundStation", index);
        if (node == nullptr)
            throw omnetpp::cRuntimeError("Missing groundStation[%d]", index);
        const osg::Vec3d position = geodeticToWorld(
            node->par("longitude"), node->par("latitude"), groundAltitude);
        groundStationPositions.push_back(position);
        groundVertices->push_back(static_cast<osg::Vec3>(position));
    }

    userTerminalPositions.reserve(numUserTerminals);
    osg::ref_ptr<osg::Vec3Array> terminalVertices = new osg::Vec3Array();
    for (int index = 0; index < numUserTerminals; ++index) {
        omnetpp::cModule *node = getParentModule()->getSubmodule("userTerminal", index);
        if (node == nullptr)
            throw omnetpp::cRuntimeError("Missing userTerminal[%d]", index);
        const osg::Vec3d position = geodeticToWorld(
            node->par("longitude"), node->par("latitude"), terminalAltitude);
        userTerminalPositions.push_back(position);
        terminalVertices->push_back(static_cast<osg::Vec3>(position));
    }

    if (par("showGroundStations")) {
        osg::ref_ptr<osg::Geode> geode = new osg::Geode();
        geode->addDrawable(createPointGeometry(
            groundVertices, osg::Vec4(1.0f, 0.5f, 0.0f, 1.0f),
            static_cast<float>(par("groundStationPointSize").doubleValue()), false));
        scene->addChild(geode);
    }

    if (par("showUserTerminals")) {
        osg::ref_ptr<osg::Geode> geode = new osg::Geode();
        geode->addDrawable(createPointGeometry(
            terminalVertices, osg::Vec4(1.0f, 0.15f, 0.15f, 1.0f),
            static_cast<float>(par("userTerminalPointSize").doubleValue()), false, true));
        scene->addChild(geode);
    }

    pathGeode = new osg::Geode();
    selectedEndpointGeode = new osg::Geode();
    scene->addChild(pathGeode);
    scene->addChild(selectedEndpointGeode);
}

osg::ref_ptr<osg::Geometry> MpOrbLeoOsgVisualizer::createPointGeometry(
    osg::Vec3Array *vertices, const osg::Vec4& color, float pointSize, bool dynamic, bool square) const
{
    osg::ref_ptr<osg::Geometry> geometry = new osg::Geometry();
    geometry->setVertexArray(vertices);
    geometry->addPrimitiveSet(new osg::DrawArrays(GL_POINTS, 0, vertices->size()));

    osg::ref_ptr<osg::Vec4Array> colors = new osg::Vec4Array();
    colors->push_back(color);
    geometry->setColorArray(colors, osg::Array::BIND_OVERALL);
    geometry->setDataVariance(dynamic ? osg::Object::DYNAMIC : osg::Object::STATIC);
    geometry->setUseDisplayList(!dynamic);
    geometry->setUseVertexBufferObjects(dynamic);

    osg::StateSet *stateSet = geometry->getOrCreateStateSet();
    stateSet->setAttributeAndModes(new osg::Point(pointSize), osg::StateAttribute::ON);
    // Unsmooth points are screen-facing squares; smooth points are round blips.
    stateSet->setMode(GL_POINT_SMOOTH,
        (square ? osg::StateAttribute::OFF : osg::StateAttribute::ON) | osg::StateAttribute::OVERRIDE);
    stateSet->setMode(GL_LIGHTING, osg::StateAttribute::OFF | osg::StateAttribute::OVERRIDE);
    stateSet->setMode(GL_BLEND, osg::StateAttribute::ON);
    return geometry;
}

void MpOrbLeoOsgVisualizer::initializeLegend()
{
    osg::ref_ptr<osg::Camera> hud = new osg::Camera();
    hud->setReferenceFrame(osg::Transform::ABSOLUTE_RF);
    hud->setProjectionMatrix(osg::Matrix::ortho2D(0, 1920, 0, 1080));
    hud->setViewMatrix(osg::Matrix::identity());
    hud->setClearMask(0);
    hud->setRenderOrder(osg::Camera::POST_RENDER);
    hud->setAllowEventFocus(false);
    auto *state = hud->getOrCreateStateSet();
    state->setMode(GL_DEPTH_TEST, osg::StateAttribute::OFF);
    state->setMode(GL_LIGHTING, osg::StateAttribute::OFF);
    state->setMode(GL_BLEND, osg::StateAttribute::ON);
    osg::ref_ptr<osg::Geode> textGeode = new osg::Geode();
    auto text = [&](int y, const osg::Vec4& color, const std::string& value) {
        osg::ref_ptr<osgText::Text> label = new osgText::Text();
        label->setPosition(osg::Vec3(25, y, 0));
        label->setAlignment(osgText::Text::LEFT_TOP);
        label->setCharacterSize(22);
        label->setColor(color);
        label->setBackdropType(osgText::Text::OUTLINE);
        label->setBackdropColor(osg::Vec4(0, 0, 0, 1));
        label->setText(value);
        textGeode->addDrawable(label);
        return label;
    };
    headerText = text(1050, osg::Vec4(1, 1, 1, 1), "");
    int y = 990;
    for (int index : selectedPairs) {
        pairTexts.push_back(text(y, pairColor(index), ""));
        y -= 34;
    }
    y -= 10;
    text(y, osg::Vec4(1, 1, 1, 1), "Per link: 1 subflow = pair colour | 2 = yellow | 3 = orange | 4+ = red");
    text(y - 30, osg::Vec4(1, 1, 1, 1), highlightAccessLinks ?
            "Overlap counts physical links, including shared terminal access" : "Overlap highlighting: core links only");
    if (selectedPairs.size() == 1) {
        for (int rank = 0; rank < displayedPathCount; ++rank)
            rankTexts.push_back(text(y - 80 - rank * 30, osg::Vec4(1, 1, 1, 1), ""));
    }
    hud->addChild(textGeode);
    scene->addChild(hud);
}

int MpOrbLeoOsgVisualizer::findSnapshotIndex(omnetpp::simtime_t time) const
{
    const auto iterator = std::upper_bound(
        snapshotFiles.begin(), snapshotFiles.end(), time,
        [](omnetpp::simtime_t value, const SnapshotFile& snapshot) { return value < snapshot.time; });
    if (iterator == snapshotFiles.begin())
        return 0;
    return static_cast<int>(std::distance(snapshotFiles.begin(), iterator) - 1);
}

void MpOrbLeoOsgVisualizer::updateVisualization()
{
    const omnetpp::simtime_t requested = fixedTime >= omnetpp::SimTime::ZERO ? fixedTime : simTime();
    const auto cadence = snapshotFiles.size() > 1 ?
            snapshotFiles.back().time - snapshotFiles[snapshotFiles.size() - 2].time : updateInterval;
    if (requested < snapshotFiles.front().time || requested > snapshotFiles.back().time + cadence)
        throw omnetpp::cRuntimeError("Requested view time %s is outside the saved catalog coverage", requested.str().c_str());
    const int snapshotIndex = findSnapshotIndex(requested);
    const auto& file = snapshotFiles[snapshotIndex];
    leoRouting::KPathSnapshot snapshot;
    try {
        snapshot = leoRouting::readKPathSnapshot(file.path);
    }
    catch (const std::exception& error) {
        throw omnetpp::cRuntimeError("Could not read K-path snapshot '%s': %s", file.path.string().c_str(), error.what());
    }
    const auto& header = snapshot.header;
    const int coreNodes = numSatellites + numGroundStations + 2 * numClients;
    if (header.maxPathCount != pathCount || header.edgeDisjoint != edgeDisjoint ||
            header.maxSharedCoreLinks != maxSharedLinks || header.routableNodeCount != coreNodes ||
            header.totalNodeCount != coreNodes + numUserTerminals ||
            std::abs(header.maxRttSpreadMs - par("maxRttSpread").doubleValueInUnit("ms")) > 1e-6)
        throw omnetpp::cRuntimeError("Saved K-path header does not match this viewer's topology/catalog");
    const omnetpp::simtime_t snapshotTime(header.timestampMicros, omnetpp::SIMTIME_US);
    if (snapshotTime != file.time)
        throw omnetpp::cRuntimeError("Saved K-path filename/header times disagree: %s", file.path.string().c_str());
    for (int index = 0; index < numSatellites; ++index) {
        auto *orbit = satelliteOrbits[index];
        orbit->updateTime(snapshotTime);
        satellitePositions[index] = geodeticToWorld(orbit->getLongitude(), orbit->getLatitude(), orbit->getAltitude() * 1000.0);
        (*satelliteVertices)[index] = static_cast<osg::Vec3>(satellitePositions[index]);
    }
    satelliteVertices->dirty();
    if (satelliteGeometry) {
        satelliteGeometry->dirtyBound();
        satelliteGeometry->dirtyGLObjects();
    }
    updateInterSatelliteLinks();
    std::vector<leoRouting::KShortestPathGroup> groups;
    std::vector<leoView::Route> routes;
    for (int index : selectedPairs) {
        const auto [source, destination] = terminalPairs[index];
        const int32_t sourceNode = coreNodes + source, destinationNode = coreNodes + destination;
        const auto key = leoRouting::kPathEndpointPairKey(sourceNode, destinationNode);
        auto group = std::find_if(snapshot.groups.begin(), snapshot.groups.end(), [&](const auto& candidate) {
            return leoRouting::kPathEndpointPairKey(candidate.sourceNodeId, candidate.destinationNodeId) == key;
        });
        if (group == snapshot.groups.end())
            throw omnetpp::cRuntimeError("Saved catalog has no endpoint group %d-%d", source, destination);
        groups.push_back(leoRouting::orientKShortestPathGroup(*group, sourceNode, destinationNode, displayedPathCount));
        for (const auto& path : groups.back().paths)
            routes.push_back({index, path.nodeIds});
    }
    pathGeode->removeDrawables(0, pathGeode->getNumDrawables());
    for (const auto& edge : leoView::countEdges(routes))
        pathGeode->addDrawable(createLinkGeometry(edge));
    updateEndpointMarkers();
    updateLegend(groups, snapshotTime);
}

void MpOrbLeoOsgVisualizer::updateInterSatelliteLinks()
{
    if (!interSatelliteLinkVertices)
        return;

    for (size_t index = 0; index < interSatelliteLinks.size(); ++index) {
        const auto [sourceIndex, destinationIndex] = interSatelliteLinks[index];
        const osg::Vec3d& source = satellitePositions[sourceIndex];
        const osg::Vec3d& destination = satellitePositions[destinationIndex];
        (*interSatelliteLinkVertices)[2 * index].set(
            static_cast<float>(source.x()),
            static_cast<float>(source.y()),
            static_cast<float>(source.z()));
        (*interSatelliteLinkVertices)[2 * index + 1].set(
            static_cast<float>(destination.x()),
            static_cast<float>(destination.y()),
            static_cast<float>(destination.z()));
    }
    interSatelliteLinkVertices->dirty();
    interSatelliteLinkGeometry->dirtyBound();
}

osg::ref_ptr<osg::Geometry> MpOrbLeoOsgVisualizer::createLinkGeometry(const leoView::EdgeUse& edge) const
{
    const auto stroke = leoView::edgeStroke(edge, numSatellites + numGroundStations + 2 * numClients, highlightAccessLinks);
    osg::ref_ptr<osg::Vec3Array> vertices = new osg::Vec3Array();
    vertices->push_back(static_cast<osg::Vec3>(positionForNode(edge.first)));
    vertices->push_back(static_cast<osg::Vec3>(positionForNode(edge.second)));
    osg::ref_ptr<osg::Geometry> geometry = new osg::Geometry();
    geometry->setVertexArray(vertices);
    geometry->addPrimitiveSet(new osg::DrawArrays(GL_LINES, 0, 2));
    osg::ref_ptr<osg::Vec4Array> colors = new osg::Vec4Array();
    colors->push_back(osg::Vec4(stroke.color[0], stroke.color[1], stroke.color[2], stroke.color[3]));
    geometry->setColorArray(colors, osg::Array::BIND_OVERALL);
    auto *state = geometry->getOrCreateStateSet();
    state->setAttributeAndModes(new osg::LineWidth(stroke.width), osg::StateAttribute::ON);
    state->setMode(GL_LIGHTING, osg::StateAttribute::OFF | osg::StateAttribute::OVERRIDE);
    state->setMode(GL_BLEND, osg::StateAttribute::ON);
    state->setRenderBinDetails(100 + std::min(20u, edge.paths), "RenderBin");
    return geometry;
}

void MpOrbLeoOsgVisualizer::updateEndpointMarkers()
{
    selectedEndpointGeode->removeDrawables(0, selectedEndpointGeode->getNumDrawables());
    const int endpointStart = numSatellites + numGroundStations + 2 * numClients;
    for (int index : selectedPairs) {
        osg::ref_ptr<osg::Vec3Array> vertices = new osg::Vec3Array();
        vertices->push_back(static_cast<osg::Vec3>(positionForNode(endpointStart + terminalPairs[index].first)));
        vertices->push_back(static_cast<osg::Vec3>(positionForNode(endpointStart + terminalPairs[index].second)));
        selectedEndpointGeode->addDrawable(createPointGeometry(vertices, pairColor(index),
                static_cast<float>(par("selectedEndpointPointSize").doubleValue()), false, true));
    }
}

void MpOrbLeoOsgVisualizer::updateLegend(const std::vector<leoRouting::KShortestPathGroup>& groups,
        omnetpp::simtime_t snapshotTime)
{
    std::ostringstream header;
    header << "ExperimentLeo | " << policyLabel << " | K=" << displayedPathCount
           << " | " << selectedPairs.size() << " pair(s) | snapshot " << snapshotTime << " s";
    headerText->setText(header.str());
    for (size_t i = 0; i < selectedPairs.size(); ++i) {
        const auto [source, destination] = terminalPairs[selectedPairs[i]];
        const char *sourceName = getParentModule()->getSubmodule("userTerminal", source)->par("label");
        const char *destinationName = getParentModule()->getSubmodule("userTerminal", destination)->par("label");
        std::ostringstream line;
        line << sourceName << " -> " << destinationName << " | " << groups[i].paths.size() << "/"
             << displayedPathCount << " paths";
        pairTexts[i]->setText(line.str());
    }
    if (selectedPairs.size() == 1) {
        for (int rank = 0; rank < displayedPathCount; ++rank) {
            std::ostringstream line;
            line << "Rank " << rank + 1 << ": ";
            if (rank < static_cast<int>(groups.front().paths.size()))
                line << std::fixed << std::setprecision(2) << groups.front().paths[rank].rttMs << " ms RTT";
            else
                line << "unavailable";
            rankTexts[rank]->setText(line.str());
        }
    }
    getDisplayString().setTagArg("t", 0, header.str().c_str());
}

osg::Vec3d MpOrbLeoOsgVisualizer::positionForNode(int32_t nodeId) const
{
    if (nodeId >= 0 && nodeId < numSatellites)
        return satellitePositions[nodeId];

    const int32_t groundIndex = nodeId - numSatellites;
    if (groundIndex >= 0 && groundIndex < numGroundStations)
        return groundStationPositions[groundIndex];

    const int32_t endpointStart = numSatellites + numGroundStations + 2 * numClients;
    const int32_t terminalIndex = nodeId - endpointStart;
    if (terminalIndex >= 0 && terminalIndex < numUserTerminals)
        return userTerminalPositions[terminalIndex];

    throw omnetpp::cRuntimeError("K-path node ID %d cannot be mapped by the visualization network", nodeId);
}

osg::Vec3d MpOrbLeoOsgVisualizer::geodeticToWorld(
    double longitudeDegrees, double latitudeDegrees, double altitudeMeters)
{
    constexpr double PI_VALUE = 3.14159265358979323846;
    constexpr double WGS84_A = 6378137.0;
    constexpr double WGS84_E2 = 6.69437999014e-3;
    const double longitude = longitudeDegrees * PI_VALUE / 180.0;
    const double latitude = latitudeDegrees * PI_VALUE / 180.0;
    const double sinLatitude = std::sin(latitude);
    const double cosLatitude = std::cos(latitude);
    const double radius = WGS84_A / std::sqrt(1.0 - WGS84_E2 * sinLatitude * sinLatitude);

    return osg::Vec3d(
        (radius + altitudeMeters) * cosLatitude * std::cos(longitude),
        (radius + altitudeMeters) * cosLatitude * std::sin(longitude),
        (radius * (1.0 - WGS84_E2) + altitudeMeters) * sinLatitude);
}

osg::Vec4 MpOrbLeoOsgVisualizer::pairColor(int index)
{
    const auto color = leoView::pairColor(index);
    return osg::Vec4(color[0], color[1], color[2], color[3]);
}

#endif

} // namespace inet
