// SPDX-License-Identifier: LGPL-3.0-or-later
#include "WorkloadMetrics.h"

#include "inet/common/Simsignals_m.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <locale>
#include <numeric>

namespace mporbexperiments {

using namespace omnetpp;

Define_Module(WorkloadMetrics);

namespace {

constexpr std::array<int64_t, 4> sizeBounds = {100000, 1000000, 10000000, std::numeric_limits<int64_t>::max()};
constexpr int64_t longFlowMinExclusive = sizeBounds[1];
constexpr int quantileIntervals = 100;

struct SizeAggregate {
    uint64_t offered = 0;
    uint64_t bytesOffered = 0;
    uint64_t bytesDelivered = 0;
    std::vector<double> completionSeconds;
    std::vector<double> goodputMbps;
};

struct FairnessAggregate {
    uint64_t fullSecondFlows = 0;
    uint64_t bytes = 0;
    long double squaredBytes = 0;

    void add(int64_t delivered)
    {
        fullSecondFlows++;
        bytes += delivered;
        squaredBytes += static_cast<long double>(delivered) * delivered;
    }
};

void writeRatio(std::ostream& out, long double numerator, long double denominator)
{
    if (denominator > 0)
        out << numerator / denominator;
    else
        out << "null";
}

void writeDistribution(std::ostream& out, std::vector<double>& values)
{
    out << "{\"count\": " << values.size();
    if (values.empty()) {
        out << ", \"mean\": null, \"stddev\": null, \"p99\": null, \"quantiles\": []}";
        return;
    }

    std::sort(values.begin(), values.end());
    const auto mean = std::accumulate(values.begin(), values.end(), 0.L) / values.size();
    long double sumSquaredDeviations = 0;
    for (double value : values) {
        const auto deviation = value - mean;
        sumSquaredDeviations += deviation * deviation;
    }
    const auto rank = [&](int percentile) {
        return std::max<size_t>(1, (percentile * values.size() + quantileIntervals - 1) / quantileIntervals) - 1;
    };
    out << ", \"mean\": " << mean << ", \"stddev\": ";
    if (values.size() > 1)
        out << std::sqrt(sumSquaredDeviations / (values.size() - 1));
    else
        out << "null";
    out << ", \"p99\": " << values[rank(99)] << ", \"quantiles\": [";
    for (int percentile = 0; percentile <= quantileIntervals; ++percentile) {
        if (percentile)
            out << ',';
        out << values[rank(percentile)];
    }
    out << "]}";
}

bool isSatellite(const cModule *module, const cModule *network)
{
    return module && module->getParentModule() == network && !strcmp(module->getName(), "satellite");
}

double activeCapacity(const cDatarateChannel *channel)
{
    return channel->isDisabled() ? 0 : channel->getDatarate();
}

} // namespace

WorkloadMetrics::~WorkloadMetrics()
{
    cancelAndDelete(discoverIsls);
}

void WorkloadMetrics::registerFlow(int id, int64_t expectedBytes, simtime_t arrival)
{
    if (id <= 0 || expectedBytes <= 0 || arrival < SIMTIME_ZERO)
        throw cRuntimeError("Invalid workload flow %d", id);
    if (!flows.try_emplace(id, Flow{expectedBytes, arrival, 0, -1, {}}).second)
        throw cRuntimeError("Workload flow %d registered more than once", id);
}

void WorkloadMetrics::receiveBytes(int id, int64_t receivedBytes)
{
    const auto found = flows.find(id);
    if (found == flows.end())
        throw cRuntimeError("Received data for unknown workload flow %d", id);
    auto& flow = found->second;
    if (receivedBytes <= 0 || receivedBytes > flow.expectedBytes - flow.deliveredBytes || simTime() < flow.arrival)
        throw cRuntimeError("Unexpected payload delivery for workload flow %d", id);

    auto second = simTime().inUnit(SIMTIME_S);
    // Include a delivery at the exact configured end in the final interval.
    if (second > 0 && simTime() == measurementEnd && simTime() == SimTime(second, SIMTIME_S))
        second--;
    if (size_t(second) >= payloadBySecond.size())
        payloadBySecond.resize(second + 1);
    payloadBySecond[second] += receivedBytes;
    if (flow.expectedBytes > longFlowMinExclusive) {
        auto& bins = flow.deliveredBySecond;
        if (bins.empty() || bins.back().first != second)
            bins.emplace_back(second, receivedBytes);
        else
            bins.back().second += receivedBytes;
    }
    flow.deliveredBytes += receivedBytes;
    if (flow.deliveredBytes == flow.expectedBytes)
        flow.completion = simTime();
}

void WorkloadMetrics::initialize()
{
    const char *limit = getEnvir()->getConfig()->getConfigValue("sim-time-limit");
    if (limit && *limit)
        measurementEnd = SimTime::parse(limit);
    queueAttemptSignal = registerSignal("packetPushStarted");
    queueDropSignal = registerSignal("packetDropped");
    channelSentSignal = registerSignal("messageSent");
    channelDiscardSignal = registerSignal("messageDiscarded");
    // LeoChannelConstructor creates the fixed ISLs at its time-zero event.
    discoverIsls = new cMessage("workloadIslMetrics");
    discoverIsls->setSchedulingPriority(1);
    scheduleAt(SIMTIME_ZERO, discoverIsls);
}

void WorkloadMetrics::handleMessage(cMessage *message)
{
    if (message != discoverIsls)
        throw cRuntimeError("Unexpected message in WorkloadMetrics");
    watchFixedIsls();
}

void WorkloadMetrics::watchFixedIsls()
{
    auto *network = getParentModule();
    for (cModule::SubmoduleIterator it(network); !it.end(); ++it) {
        auto *satellite = *it;
        if (!isSatellite(satellite, network))
            continue;
        for (int index = 0; index < satellite->gateSize("pppg"); ++index) {
            auto *gate = satellite->gateHalf("pppg", cGate::OUTPUT, index);
            auto *next = gate->getNextGate();
            if (!next || !isSatellite(next->getOwnerModule(), network))
                continue;
            auto *channel = check_and_cast<cDatarateChannel *>(gate->getChannel());
            auto *ppp = satellite->getSubmodule("ppp", index);
            auto *queue = ppp ? ppp->getSubmodule("queue") : nullptr;
            if (!queue)
                throw cRuntimeError("Missing ISL PPP queue at %s", gate->getFullPath().c_str());
            auto& isl = isls[channel];
            isl.capacityBps = activeCapacity(channel);
            islCapacityBps += isl.capacityBps;
            channel->subscribe(channelSentSignal, this);
            channel->subscribe(channelDiscardSignal, this);
            channel->subscribe(POST_MODEL_CHANGE, this);
            queue->subscribe(queueAttemptSignal, this);
            queue->subscribe(queueDropSignal, this);
        }
    }
}

void WorkloadMetrics::accumulateCapacity(simtime_t until)
{
    islCapacityBits += static_cast<long double>(islCapacityBps) * (until - lastCapacityUpdate).dbl();
    lastCapacityUpdate = until;
}

void WorkloadMetrics::receiveSignal(cComponent *source, simsignal_t signal, cObject *value, cObject *details)
{
    if (signal == queueAttemptSignal) {
        islQueueAttempts++;
    }
    else if (signal == queueDropSignal) {
        islQueueDrops++;
        const auto *drop = dynamic_cast<const inet::PacketDropDetails *>(details);
        if (drop && drop->getReason() == inet::QUEUE_OVERFLOW)
            islQueueOverflow++;
    }
    else if (signal == channelSentSignal) {
        const auto *transmission = check_and_cast<cChannel::MessageSentSignalValue *>(value);
        const auto *packet = dynamic_cast<const cPacket *>(transmission->getMessage());
        if (!packet)
            return;
        auto& isl = isls.at(source);
        isl.lastPacketBits = packet->getBitLength();
        isl.lastStart = transmission->getTimestamp(signal);
        isl.lastDuration = transmission->getChannelResult()->duration;
        islWireBits += isl.lastPacketBits;
        islChannelAttempts++;
        if (packet->hasBitError())
            islChannelCorrupt++;
    }
    else if (signal == channelDiscardSignal) {
        const auto *discard = check_and_cast<cTimestampedValue *>(value);
        if (dynamic_cast<const cPacket *>(discard->objectValue(signal))) {
            islChannelAttempts++;
            islChannelDiscards++;
        }
    }
    else if (signal == POST_MODEL_CHANGE) {
        const auto *change = dynamic_cast<const cPostParameterChangeNotification *>(value);
        if (!change || (strcmp(change->par->getName(), "datarate") && strcmp(change->par->getName(), "disabled")))
            return;
        auto& isl = isls.at(source);
        accumulateCapacity(simTime());
        const auto newCapacity = activeCapacity(check_and_cast<cDatarateChannel *>(source));
        islCapacityBps += newCapacity - isl.capacityBps;
        isl.capacityBps = newCapacity;
    }
}

void WorkloadMetrics::finish()
{
    const simtime_t end = std::max(measurementEnd, simTime());
    accumulateCapacity(end);
    long double sentBits = islWireBits;
    for (const auto& [_, isl] : isls) {
        const auto remaining = isl.lastStart + isl.lastDuration - end;
        if (remaining > SIMTIME_ZERO && isl.lastDuration > SIMTIME_ZERO)
            sentBits -= static_cast<long double>(isl.lastPacketBits) * remaining.dbl() / isl.lastDuration.dbl();
    }

    const auto secondCount = std::max<size_t>(payloadBySecond.size(), std::ceil(end.dbl()));
    payloadBySecond.resize(secondCount);
    std::vector<FairnessAggregate> fairness(secondCount);
    std::array<SizeAggregate, sizeBounds.size()> sizeClasses;
    uint64_t completed = 0;
    uint64_t bytesOffered = 0;
    uint64_t bytesDelivered = 0;
    for (const auto& [_, flow] : flows) {
        const auto sizeIndex = std::lower_bound(sizeBounds.begin(), sizeBounds.end(), flow.expectedBytes) - sizeBounds.begin();
        auto& aggregate = sizeClasses[sizeIndex];
        aggregate.offered++;
        aggregate.bytesOffered += flow.expectedBytes;
        aggregate.bytesDelivered += flow.deliveredBytes;
        bytesOffered += flow.expectedBytes;
        bytesDelivered += flow.deliveredBytes;
        if (flow.completion >= SIMTIME_ZERO) {
            completed++;
            const double fct = (flow.completion - flow.arrival).dbl();
            aggregate.completionSeconds.push_back(fct);
            if (fct > 0)
                aggregate.goodputMbps.push_back(flow.expectedBytes * 8e-6 / fct);
        }

        if (flow.expectedBytes > longFlowMinExclusive) {
            auto second = flow.arrival.inUnit(SIMTIME_S);
            if (flow.arrival != SimTime(second, SIMTIME_S))
                second++;
            const auto stop = (flow.completion >= SIMTIME_ZERO ? flow.completion : end).inUnit(SIMTIME_S);
            auto bin = flow.deliveredBySecond.begin();
            for (; second < stop; ++second) {
                while (bin != flow.deliveredBySecond.end() && bin->first < second)
                    bin++;
                const auto bytes = bin != flow.deliveredBySecond.end() && bin->first == second ? bin->second : 0;
                fairness[second].add(bytes);
            }
        }
    }

    const std::filesystem::path target = par("outputFile").stringValue();
    const auto temporary = target.string() + ".tmp";
    std::ofstream out(temporary);
    if (!out)
        throw cRuntimeError("Cannot write workload metrics: %s", temporary.c_str());
    out.imbue(std::locale::classic());
    out << std::setprecision(12)
        << "{\n  \"schema_version\": 1,\n  \"duration_s\": " << end.dbl()
        << ",\n  \"last_event_s\": " << simTime().dbl()
        << ",\n  \"offered_flows\": " << flows.size()
        << ", \"completed_flows\": " << completed << ", \"unfinished_flows\": " << flows.size() - completed
        << ",\n  \"offered_bytes\": " << bytesOffered << ", \"delivered_bytes\": " << bytesDelivered
        << ",\n  \"quantile_percent_step\": " << 100 / quantileIntervals
        << ",\n  \"definitions\": {"
           "\"fct_seconds\": \"Time from workload arrival to the delivery of the final payload byte\", "
           "\"goodput_mbps\": \"Received payload bits divided by FCT for completed flows\", "
           "\"endpoint_seconds\": \"In-order payload delivered per second, including unfinished flows\", "
           "\"jain\": \"Payload-rate Jain index across long flows outstanding for the entire second\", "
           "\"core_utilization\": \"Transmitted wire bits divided by capacity integrated over directed ISLs and simulation time\", "
           "\"core_queue_drop_fraction\": \"ISL queue drops divided by queue push attempts\", "
           "\"core_channel_loss_fraction\": \"ISL channel discards and corrupt packets divided by channel send attempts\"}"
        << ",\n  \"size_classes\": [";
    for (size_t index = 0; index < sizeClasses.size(); ++index) {
        auto& aggregate = sizeClasses[index];
        if (index)
            out << ',';
        out << "\n    {\"min_exclusive_bytes\": " << (index ? sizeBounds[index - 1] : 0)
            << ", \"max_inclusive_bytes\": ";
        if (index + 1 == sizeBounds.size())
            out << "null";
        else
            out << sizeBounds[index];
        out << ", \"offered\": " << aggregate.offered
            << ", \"completed\": " << aggregate.completionSeconds.size()
            << ", \"unfinished\": " << aggregate.offered - aggregate.completionSeconds.size()
            << ", \"offered_bytes\": " << aggregate.bytesOffered
            << ", \"delivered_bytes\": " << aggregate.bytesDelivered
            << ", \"fct_seconds\": ";
        writeDistribution(out, aggregate.completionSeconds);
        out << ", \"goodput_mbps\": ";
        writeDistribution(out, aggregate.goodputMbps);
        out << '}';
    }
    out << "\n  ],\n  \"fairness_long_min_exclusive_bytes\": " << longFlowMinExclusive
        << ",\n  \"endpoint_seconds\": [";
    for (size_t second = 0; second < secondCount; ++second) {
        const auto& bin = fairness[second];
        if (second)
            out << ',';
        out << "\n    {\"start_s\": " << second << ", \"end_s\": " << std::min(end.dbl(), double(second + 1))
            << ", \"delivered_bytes\": " << payloadBySecond[second]
            << ", \"long_full_second_flows\": " << bin.fullSecondFlows
            << ", \"long_delivered_bytes_sum\": " << bin.bytes
            << ", \"long_delivered_bytes_sum_of_squares\": " << bin.squaredBytes << ", \"jain\": ";
        if (bin.fullSecondFlows >= 2)
            writeRatio(out, static_cast<long double>(bin.bytes) * bin.bytes, bin.fullSecondFlows * bin.squaredBytes);
        else
            out << "null";
        out << '}';
    }
    out << "\n  ],\n  \"core\": {\"directed_isl_count\": " << isls.size()
        << ", \"capacity_bits\": " << islCapacityBits << ", \"wire_bits_transmitted\": " << sentBits
        << ", \"utilization\": ";
    writeRatio(out, sentBits, islCapacityBits);
    out << ", \"queue_attempt_packets\": " << islQueueAttempts << ", \"queue_drop_packets\": " << islQueueDrops
        << ", \"queue_overflow_packets\": " << islQueueOverflow << ", \"queue_drop_fraction\": ";
    writeRatio(out, islQueueDrops, islQueueAttempts);
    out << ", \"channel_attempt_packets\": " << islChannelAttempts
        << ", \"channel_discard_packets\": " << islChannelDiscards
        << ", \"channel_corrupt_packets\": " << islChannelCorrupt
        << ", \"channel_loss_fraction\": ";
    writeRatio(out, islChannelDiscards + islChannelCorrupt, islChannelAttempts);
    out << "}\n}\n";
    out.close();
    if (!out)
        throw cRuntimeError("Failed writing workload metrics: %s", temporary.c_str());
    std::filesystem::rename(temporary, target);
}

} // namespace mporbexperiments
