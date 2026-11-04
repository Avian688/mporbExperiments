// Read saved ExperimentLeo routes without starting OMNeT++ or finding new paths.
#include "LeoKPathSnapshot.h"
#include "LeoRouteSnapshot.h"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace fs = std::filesystem;
namespace routing = inet::leoRouting;

namespace {

constexpr int64_t MAX_INTERVAL_US = 100001;
constexpr int32_t CATALOG_PATH_COUNT = 10;
constexpr size_t PROGRESS_INTERVAL = 100;

struct Options {
    fs::path kDirectory;
    fs::path primaryDirectory;
    int64_t durationUs = 0;
};

void usage(std::ostream& output)
{
    output << "Usage: exportLeoRouteFrames --k-dir DIR [--primary-dir DIR] --duration-us INTEGER\n"
              "Emit validated saved-route frames as JSON lines for ExperimentLeo UT pairs 5->10 through 9->14.\n"
              "No paths are recomputed. Omit --primary-dir to skip native single-path route replay.\n";
}

Options parseOptions(int argc, char **argv)
{
    Options options;
    bool hasDuration = false;
    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        if (argument == "--help" || argument == "-h") {
            usage(std::cout);
            std::exit(0);
        }
        if (argument != "--k-dir" && argument != "--primary-dir" && argument != "--duration-us")
            throw std::runtime_error("Unknown argument: " + argument);
        if (++index == argc)
            throw std::runtime_error("Missing value for " + argument);
        const std::string value = argv[index];
        if (argument == "--k-dir") {
            if (!options.kDirectory.empty() || value.empty())
                throw std::runtime_error("--k-dir must be supplied once with a nonempty directory");
            options.kDirectory = value;
        }
        else if (argument == "--primary-dir") {
            if (!options.primaryDirectory.empty() || value.empty())
                throw std::runtime_error("--primary-dir must be supplied at most once with a nonempty directory");
            options.primaryDirectory = value;
        }
        else {
            if (hasDuration || value.empty() ||
                value.find_first_not_of("0123456789") != std::string::npos)
                throw std::runtime_error("--duration-us must be supplied once as a positive integer");
            size_t consumed = 0;
            options.durationUs = std::stoll(value, &consumed);
            if (consumed != value.size() || options.durationUs <= 0)
                throw std::runtime_error("--duration-us must be a positive integer");
            hasDuration = true;
        }
    }
    if (options.kDirectory.empty() || !hasDuration)
        throw std::runtime_error("--k-dir and --duration-us are required");
    return options;
}

std::vector<fs::path> intervalFiles(const fs::path& directory, int64_t durationUs)
{
    std::vector<fs::path> files;
    for (const fs::path& path : routing::listSnapshotFilesNumeric(directory)) {
        if (routing::timestampMicrosFromPath(path) < durationUs)
            files.push_back(path);
    }
    if (files.empty() || routing::timestampMicrosFromPath(files.front()) != 0)
        throw std::runtime_error("Snapshot coverage must begin at 0 in " + directory.string());
    int64_t previous = 0;
    for (size_t index = 1; index < files.size(); ++index) {
        const int64_t current = routing::timestampMicrosFromPath(files[index]);
        if (current <= previous || current - previous > MAX_INTERVAL_US)
            throw std::runtime_error("Snapshot coverage gap before " + files[index].string());
        previous = current;
    }
    if (durationUs - previous > MAX_INTERVAL_US)
        throw std::runtime_error("Snapshot coverage ends before the requested duration in " + directory.string());
    return files;
}

using EndpointPairs = std::vector<std::pair<int32_t, int32_t>>;

EndpointPairs endpointPairs(const routing::KPathSnapshot& snapshot)
{
    EndpointPairs pairs;
    pairs.reserve(snapshot.groups.size());
    for (const auto& group : snapshot.groups)
        pairs.emplace_back(group.sourceNodeId, group.destinationNodeId);
    std::sort(pairs.begin(), pairs.end());
    return pairs;
}

void validateCatalog(const routing::KPathSnapshot& snapshot,
                     const routing::KPathSnapshotHeader& initialHeader,
                     const EndpointPairs& initialPairs, size_t sequence)
{
    const auto& header = snapshot.header;
    if (sequence > static_cast<size_t>(std::numeric_limits<int32_t>::max()) ||
        header.sequence != static_cast<int32_t>(sequence))
        throw std::runtime_error("K-path snapshots must have consecutive sequences beginning at 0");
    if (header.maxPathCount != CATALOG_PATH_COUNT || !header.edgeDisjoint ||
        header.algorithm != routing::KPathAlgorithm::EdgeDisjointMinCostFlow ||
        header.maxSharedCoreLinks != -1)
        throw std::runtime_error("Expected the ten-path edge-disjoint ExperimentLeo catalog");
    if (static_cast<int64_t>(header.totalNodeCount) - header.routableNodeCount < 15)
        throw std::runtime_error("The saved topology does not contain ExperimentLeo user terminals 0 through 14");
    if (header.routableNodeCount != initialHeader.routableNodeCount ||
        header.totalNodeCount != initialHeader.totalNodeCount ||
        header.maxPathCount != initialHeader.maxPathCount ||
        header.maxRttSpreadMs != initialHeader.maxRttSpreadMs ||
        header.edgeDisjoint != initialHeader.edgeDisjoint ||
        header.algorithm != initialHeader.algorithm ||
        header.maxSharedCoreLinks != initialHeader.maxSharedCoreLinks ||
        endpointPairs(snapshot) != initialPairs)
        throw std::runtime_error("K-path snapshot dimensions, policy, or endpoint catalog changed during replay");
}

const routing::KShortestPathGroup& findGroup(const routing::KPathSnapshot& snapshot, int32_t source)
{
    const int32_t sourceNode = snapshot.header.routableNodeCount + source;
    const int32_t destinationNode = sourceNode + 5;
    for (const auto& group : snapshot.groups) {
        if (group.sourceNodeId == sourceNode && group.destinationNodeId == destinationNode)
            return group;
    }
    throw std::runtime_error("Missing ExperimentLeo user-terminal pair " + std::to_string(source) +
                             "->" + std::to_string(source + 5));
}

struct PrimaryPath {
    const char *status = "not_requested";
    std::vector<int32_t> nodes;
};

PrimaryPath primaryPath(const routing::KShortestPathGroup& group,
                        const routing::StableRouteState& state)
{
    PrimaryPath result;
    if (group.coreSourceNodeId < 0 || group.coreDestinationNodeId < 0) {
        result.status = "disconnected";
        return result;
    }
    result.status = "unreachable";
    std::vector<uint8_t> visited(static_cast<size_t>(state.sourceCount()), 0);
    int32_t current = group.coreSourceNodeId;
    const int32_t destination = group.coreDestinationNodeId;
    result.nodes.push_back(group.sourceNodeId);
    while (current >= 0 && current < state.sourceCount() && !visited[current]) {
        visited[current] = 1;
        result.nodes.push_back(current);
        if (current == destination) {
            result.nodes.push_back(group.destinationNodeId);
            result.status = "available";
            return result;
        }
        current = state.get(current, destination);
    }
    result.nodes.clear();
    return result;
}

void writeNodes(const std::vector<int32_t>& nodes)
{
    std::cout << '[';
    for (size_t index = 0; index < nodes.size(); ++index) {
        if (index != 0)
            std::cout << ',';
        std::cout << nodes[index];
    }
    std::cout << ']';
}

void writeFrame(const routing::KPathSnapshot& snapshot, int64_t endUs,
                const routing::StableRouteState *primary)
{
    std::cout << "{\"start_us\":" << snapshot.header.timestampMicros
              << ",\"end_us\":" << endUs
              << ",\"core_nodes\":" << snapshot.header.routableNodeCount << ",\"routes\":[";
    for (int32_t source = 5; source < 10; ++source) {
        const auto& group = findGroup(snapshot, source);
        if (source != 5)
            std::cout << ',';
        std::cout << "{\"source\":" << source << ",\"destination\":" << source + 5 << ",\"paths\":[";
        for (size_t index = 0; index < group.paths.size(); ++index) {
            if (index != 0)
                std::cout << ',';
            writeNodes(group.paths[index].nodeIds);
        }
        const PrimaryPath route = primary != nullptr ? primaryPath(group, *primary) : PrimaryPath{};
        std::cout << "],\"primary\":";
        if (route.nodes.empty())
            std::cout << "null";
        else
            writeNodes(route.nodes);
        std::cout << ",\"primary_status\":\"" << route.status << "\"}";
    }
    std::cout << "]}\n";
    if (!std::cout)
        throw std::runtime_error("Cannot write route-frame JSON output");
}

void exportFrames(const Options& options)
{
    const auto kFiles = intervalFiles(options.kDirectory, options.durationUs);
    const bool includePrimary = !options.primaryDirectory.empty();
    std::vector<fs::path> primaryFiles;
    if (includePrimary) {
        primaryFiles = intervalFiles(options.primaryDirectory, options.durationUs);
        if (primaryFiles.size() != kFiles.size())
            throw std::runtime_error("Primary and K-path snapshots have different timestamp coverage");
        for (size_t index = 0; index < kFiles.size(); ++index) {
            if (routing::timestampMicrosFromPath(kFiles[index]) !=
                routing::timestampMicrosFromPath(primaryFiles[index]))
                throw std::runtime_error("Primary and K-path snapshot timestamps are not aligned");
        }
    }

    routing::KPathSnapshotHeader initialHeader;
    EndpointPairs initialPairs;
    routing::StableRouteState primary;
    std::cerr << "exportLeoRouteFrames: reading " << kFiles.size() << " saved-route snapshots"
              << (includePrimary ? " including primary routes" : "") << '\n';
    for (size_t index = 0; index < kFiles.size(); ++index) {
        const auto snapshot = routing::readKPathSnapshot(kFiles[index]);
        if (index == 0) {
            initialHeader = snapshot.header;
            initialPairs = endpointPairs(snapshot);
        }
        validateCatalog(snapshot, initialHeader, initialPairs, index);
        // Check all five required pairs before emitting any part of this frame.
        for (int32_t source = 5; source < 10; ++source)
            findGroup(snapshot, source);

        if (includePrimary) {
            const auto parsed = routing::readSnapshot(primaryFiles[index]);
            if (parsed.format != routing::SnapshotFormat::V3)
                throw std::runtime_error("Primary replay requires LEO3 base/delta snapshots");
            if (parsed.header.sourceCount != snapshot.header.routableNodeCount ||
                parsed.header.destinationCount != snapshot.header.routableNodeCount)
                throw std::runtime_error("Primary route dimensions do not match the K-path core topology");
            // Some saved corpora contain independent sequence-0 bases at every
            // timestamp. A primary sequence is local to its own base/delta chain;
            // it need not equal the companion K-path catalog's sequence.
            if (parsed.header.kind == routing::SnapshotKind::Base)
                primary = routing::StableRouteState::fromFullSnapshot(parsed,
                    snapshot.header.routableNodeCount, snapshot.header.routableNodeCount);
            else {
                const auto preview = primary.validateDelta(parsed);
                primary.applyValidatedDelta(parsed, preview);
            }
            if (primary.timestampMicros() != snapshot.header.timestampMicros ||
                primary.stateHash() != snapshot.header.routeStateHash)
                throw std::runtime_error("K-path snapshot does not match its same-time primary route state");
        }

        const int64_t endUs = index + 1 < kFiles.size() ?
            routing::timestampMicrosFromPath(kFiles[index + 1]) : options.durationUs;
        writeFrame(snapshot, endUs, includePrimary ? &primary : nullptr);
        if ((index + 1) % PROGRESS_INTERVAL == 0 && index + 1 < kFiles.size())
            std::cerr << "exportLeoRouteFrames: decoded " << index + 1 << '/' << kFiles.size()
                      << " snapshots (through " << endUs << " us)\n";
    }
    std::cerr << "exportLeoRouteFrames: decoded " << kFiles.size() << " snapshots; complete coverage [0,"
              << options.durationUs << ") us\n";
}

} // namespace

int main(int argc, char **argv)
{
    try {
        exportFrames(parseOptions(argc, argv));
        return 0;
    }
    catch (const std::exception& error) {
        std::cerr << "exportLeoRouteFrames: " << error.what() << '\n';
        return 1;
    }
}
