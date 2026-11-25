// SPDX-License-Identifier: LGPL-3.0-or-later
#ifndef __MPORBEXPERIMENTS_WORKLOADMETRICS_H
#define __MPORBEXPERIMENTS_WORKLOADMETRICS_H

#include <omnetpp.h>

#include <cstdint>
#include <memory>
#include <unordered_map>
#include <utility>
#include <vector>

namespace mporbexperiments {

class WorkloadTcpDiagnostics;

class WorkloadMetrics : public omnetpp::cSimpleModule, public omnetpp::cListener
{
  private:
    struct Flow {
        int64_t expectedBytes;
        omnetpp::simtime_t arrival;
        int server;
        int client;
        int64_t deliveredBytes = 0;
        omnetpp::simtime_t established = -1;
        omnetpp::simtime_t firstByte = -1;
        omnetpp::simtime_t completion = -1;
        bool socketFailed = false;
        int hotspotPair = -1;
        // Keep only nonempty one-second buckets for the large-flow fairness aggregate.
        std::vector<std::pair<int64_t, int64_t>> deliveredBySecond;
    };

    struct Isl {
        double capacityBps = 0;
        int64_t lastPacketBits = 0;
        omnetpp::simtime_t lastStart;
        omnetpp::simtime_t lastDuration;
        int sourceSatellite = -1;
        int targetSatellite = -1;
        size_t second = 0;
        int64_t busyTicks = 0;
        int64_t totalBusyTicks = 0;
        uint64_t wireBitsStarted = 0;
        uint64_t queueDrops = 0;
    };

    struct CoreSecond {
        int64_t peakBusyTicks = 0;
        int peakSourceSatellite = -1;
        int peakTargetSatellite = -1;
        uint32_t linksAt80Percent = 0;
        uint32_t linksAt95Percent = 0;
    };

    struct AccessDirection {
        uint64_t wireBits = 0;
        std::vector<uint64_t> bitsBySecond;
    };

    struct TerminalAccess {
        AccessDirection uplink;
        AccessDirection downlink;
        uint64_t uplinkQueueDrops = 0;
        uint64_t uplinkQueueOverflow = 0;
        uint64_t downlinkQueueDrops = 0;
        uint64_t downlinkQueueOverflow = 0;
    };

    struct HotspotBin {
        std::vector<uint64_t> receivedBytesByRank;
        uint64_t appDeliveredBytes = 0;
    };

    struct HotspotPair {
        int server;
        int client;
        omnetpp::cModule *receiverTcp;
        std::vector<HotspotBin> bins;
    };

    struct HotspotPort {
        size_t pairIndex;
        size_t rankIndex;
    };

    std::unordered_map<int, Flow> flows;
    std::unique_ptr<WorkloadTcpDiagnostics> diagnostics;
    std::vector<HotspotPair> hotspotPairs;
    std::unordered_map<const omnetpp::cModule *, std::unordered_map<int, HotspotPort>> hotspotPorts;
    int64_t hotspotStartTick = 0;
    int64_t hotspotEndTick = 0;
    int64_t hotspotBinTicks = 0;
    int hotspotRanks = 0;
    omnetpp::simsignal_t hotspotPayloadSignal = SIMSIGNAL_NULL;
    std::vector<int64_t> payloadBySecond;
    omnetpp::simtime_t measurementEnd;
    std::unordered_map<omnetpp::cComponent *, Isl> isls;
    std::unordered_map<omnetpp::cComponent *, Isl *> islQueues;
    std::vector<CoreSecond> coreBySecond;
    std::vector<TerminalAccess> terminalAccess;
    int64_t ticksPerSecond = 0;
    double islCapacityBps = 0;
    long double islCapacityBits = 0;
    omnetpp::simtime_t lastCapacityUpdate;
    uint64_t islWireBits = 0;
    uint64_t islQueueAttempts = 0;
    uint64_t islQueueDrops = 0;
    uint64_t islQueueOverflow = 0;
    uint64_t islChannelAttempts = 0;
    uint64_t islChannelDiscards = 0;
    uint64_t islChannelCorrupt = 0;

    omnetpp::simsignal_t tcpRtoSignal;
    omnetpp::simsignal_t reinjectedBytesSignal;
    uint64_t tcpRtos = 0;
    uint64_t metaReinjections = 0;
    std::unordered_map<int, uint64_t> reinjectedBytesByConnection;

    omnetpp::cMessage *discoverIsls = nullptr;
    omnetpp::simsignal_t queueAttemptSignal;
    omnetpp::simsignal_t queueDropSignal;
    omnetpp::simsignal_t channelSentSignal;
    omnetpp::simsignal_t channelDiscardSignal;
    omnetpp::simsignal_t accessUplinkSignal;
    omnetpp::simsignal_t accessDownlinkSignal;

    void watchFixedIsls();
    void watchTransportRecovery();
    void watchTerminalAccess();
    void watchHotspot(const char *pairs);
    void accumulateCapacity(omnetpp::simtime_t until);
    void recordIslBusyInterval(Isl& isl, omnetpp::simtime_t until);
    void finishIslSecond(Isl& isl, int64_t ticksInSecond);
    void recordAccessFrame(TerminalAccess& terminal, omnetpp::simsignal_t signal,
                           const omnetpp::cPacket *packet);
    void recordAccessQueueDrop(omnetpp::cComponent *source, bool overflow);
    void writeLinkMetrics(std::ostream& out, omnetpp::simtime_t end);
    void writeDiagnosticFlows();
    void writeHotspotBytes();

  protected:
    void initialize() override;
    void handleMessage(omnetpp::cMessage *message) override;
    void finish() override;
    using omnetpp::cListener::receiveSignal;
    void receiveSignal(omnetpp::cComponent *source, omnetpp::simsignal_t signal,
                       omnetpp::intval_t value, omnetpp::cObject *details) override;
    void receiveSignal(omnetpp::cComponent *source, omnetpp::simsignal_t signal,
                       omnetpp::uintval_t value, omnetpp::cObject *details) override;
    void receiveSignal(omnetpp::cComponent *source, omnetpp::simsignal_t signal,
                       omnetpp::cObject *value, omnetpp::cObject *details) override;

  public:
    ~WorkloadMetrics() override;
    void registerFlow(int id, int64_t expectedBytes, omnetpp::simtime_t arrival, int server, int client,
                      int passivePort, int portStride);
    void connectionEstablished(int id);
    void watchDiagnosticConnection(int id, omnetpp::cModule *terminal, int socketId, bool sender);
    void socketFailed(int id);
    void receiveBytes(int id, int64_t receivedBytes);
};

} // namespace mporbexperiments

#endif
