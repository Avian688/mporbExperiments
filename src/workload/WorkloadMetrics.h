// SPDX-License-Identifier: LGPL-3.0-or-later
#ifndef __MPORBEXPERIMENTS_WORKLOADMETRICS_H
#define __MPORBEXPERIMENTS_WORKLOADMETRICS_H

#include <omnetpp.h>

#include <cstdint>
#include <unordered_map>
#include <utility>
#include <vector>

namespace mporbexperiments {

class WorkloadMetrics : public omnetpp::cSimpleModule, public omnetpp::cListener
{
  private:
    struct Flow {
        int64_t expectedBytes;
        omnetpp::simtime_t arrival;
        int64_t deliveredBytes = 0;
        omnetpp::simtime_t completion = -1;
        // Keep only nonempty one-second buckets for the large-flow fairness aggregate.
        std::vector<std::pair<int64_t, int64_t>> deliveredBySecond;
    };

    struct Isl {
        double capacityBps = 0;
        int64_t lastPacketBits = 0;
        omnetpp::simtime_t lastStart;
        omnetpp::simtime_t lastDuration;
    };

    std::unordered_map<int, Flow> flows;
    std::vector<int64_t> payloadBySecond;
    omnetpp::simtime_t measurementEnd;
    std::unordered_map<omnetpp::cComponent *, Isl> isls;
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

    omnetpp::cMessage *discoverIsls = nullptr;
    omnetpp::simsignal_t queueAttemptSignal;
    omnetpp::simsignal_t queueDropSignal;
    omnetpp::simsignal_t channelSentSignal;
    omnetpp::simsignal_t channelDiscardSignal;

    void watchFixedIsls();
    void accumulateCapacity(omnetpp::simtime_t until);

  protected:
    void initialize() override;
    void handleMessage(omnetpp::cMessage *message) override;
    void finish() override;
    using omnetpp::cListener::receiveSignal;
    void receiveSignal(omnetpp::cComponent *source, omnetpp::simsignal_t signal,
                       omnetpp::cObject *value, omnetpp::cObject *details) override;

  public:
    ~WorkloadMetrics() override;
    void registerFlow(int id, int64_t expectedBytes, omnetpp::simtime_t arrival);
    void receiveBytes(int id, int64_t receivedBytes);
};

} // namespace mporbexperiments

#endif
