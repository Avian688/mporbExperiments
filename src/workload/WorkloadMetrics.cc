// SPDX-License-Identifier: LGPL-3.0-or-later
#include "WorkloadMetrics.h"

#include "inet/common/Simsignals.h"
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
#include <map>
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
    std::vector<double> setupSeconds;
    std::vector<double> firstByteSeconds;
    std::vector<double> completionSeconds;
    std::vector<double> goodputMbps;
};

struct PairAggregate {
    uint64_t offered = 0;
    uint64_t completed = 0;
    uint64_t bytesOffered = 0;
    uint64_t bytesDelivered = 0;
    std::vector<double> shortFctSeconds;
    std::vector<double> longGoodputMbps;
};

struct FlowSecond {
    uint64_t arrivals = 0;
    uint64_t completions = 0;
    uint64_t bytesOffered = 0;
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

void writeDistribution(std::ostream& out, std::vector<double>& values, bool includeQuantiles = true)
{
    out << "{\"count\": " << values.size();
    if (values.empty()) {
        out << ", \"mean\": null, \"stddev\": null, \"p99\": null";
        if (includeQuantiles)
            out << ", \"quantiles\": []";
        out << '}';
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
    out << ", \"p99\": " << values[rank(99)];
    if (includeQuantiles) {
        out << ", \"quantiles\": [";
        for (int percentile = 0; percentile <= quantileIntervals; ++percentile) {
            if (percentile)
                out << ',';
            out << values[rank(percentile)];
        }
        out << ']';
    }
    out << '}';
}

int64_t measurementSecond(simtime_t time, simtime_t end)
{
    const auto second = time.inUnit(SIMTIME_S);
    // A delivery exactly at the configured end belongs to the final interval.
    return second > 0 && time == end && time == SimTime(second, SIMTIME_S) ? second - 1 : second;
}

bool isSatellite(const cModule *module, const cModule *network)
{
    return module && module->getParentModule() == network && !strcmp(module->getName(), "satellite");
}

const cModule *getPppInterface(cComponent *source, const char *emitter)
{
    const auto *module = dynamic_cast<const cModule *>(source);
    if (!module || strcmp(module->getName(), emitter))
        return nullptr;
    const auto *interface = module->getParentModule();
    return interface && interface->isVector() && !strcmp(interface->getName(), "ppp") ? interface : nullptr;
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

void WorkloadMetrics::registerFlow(int id, int64_t expectedBytes, simtime_t arrival, int server, int client)
{
    if (id <= 0 || expectedBytes <= 0 || arrival < SIMTIME_ZERO)
        throw cRuntimeError("Invalid workload flow %d", id);
    if (!flows.try_emplace(id, Flow{expectedBytes, arrival, server, client}).second)
        throw cRuntimeError("Workload flow %d registered more than once", id);
}

void WorkloadMetrics::connectionEstablished(int id)
{
    flows.at(id).established = simTime();
}

void WorkloadMetrics::socketFailed(int id)
{
    flows.at(id).socketFailed = true;
}

void WorkloadMetrics::receiveBytes(int id, int64_t receivedBytes)
{
    const auto found = flows.find(id);
    if (found == flows.end())
        throw cRuntimeError("Received data for unknown workload flow %d", id);
    auto& flow = found->second;
    if (receivedBytes <= 0 || receivedBytes > flow.expectedBytes - flow.deliveredBytes || simTime() < flow.arrival)
        throw cRuntimeError("Unexpected payload delivery for workload flow %d", id);

    const auto second = measurementSecond(simTime(), measurementEnd);
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
    if (flow.deliveredBytes == 0)
        flow.firstByte = simTime();
    flow.deliveredBytes += receivedBytes;
    if (flow.deliveredBytes == flow.expectedBytes)
        flow.completion = simTime();
}

void WorkloadMetrics::initialize()
{
    const char *limit = getEnvir()->getConfig()->getConfigValue("sim-time-limit");
    if (limit && *limit)
        measurementEnd = SimTime::parse(limit);
    watchTransportRecovery();
    queueAttemptSignal = registerSignal("packetPushStarted");
    queueDropSignal = registerSignal("packetDropped");
    // Queue drops propagate to the network. A single listener also covers
    // satellite access queues created during handovers.
    getParentModule()->subscribe(queueDropSignal, this);
    channelSentSignal = registerSignal("messageSent");
    channelDiscardSignal = registerSignal("messageDiscarded");
    accessUplinkSignal = inet::packetSentToLowerSignal;
    accessDownlinkSignal = registerSignal("rxPkOk");
    ticksPerSecond = SimTime(1, SIMTIME_S).raw();
    coreBySecond.resize(static_cast<size_t>(std::ceil(measurementEnd.dbl())));
    watchTerminalAccess();
    // LeoChannelConstructor creates the fixed ISLs at its time-zero event.
    discoverIsls = new cMessage("workloadIslMetrics");
    discoverIsls->setSchedulingPriority(1);
    scheduleAt(SIMTIME_ZERO, discoverIsls);
}

void WorkloadMetrics::watchTransportRecovery()
{
    tcpRtoSignal = registerSignal("numRtos");
    reinjectedBytesSignal = registerSignal("metaReinjectedBytes");
    for (cModule::SubmoduleIterator it(getParentModule()); !it.end(); ++it) {
        auto *terminal = *it;
        if (strcmp(terminal->getName(), "userTerminal") != 0)
            continue;
        if (auto *tcp = terminal->getSubmodule("tcp")) {
            tcp->subscribe(tcpRtoSignal, this);
            tcp->subscribe(reinjectedBytesSignal, this);
        }
    }
}

void WorkloadMetrics::receiveSignal(cComponent *source, simsignal_t signal, uintval_t value, cObject *)
{
    if (signal == tcpRtoSignal)
        tcpRtos++;
    else if (signal == reinjectedBytesSignal) {
        metaReinjections++;
        // Key the cumulative counter by module ID so teardown and pointer reuse are safe.
        reinjectedBytesByConnection[source->getId()] = value;
    }
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
            isl.sourceSatellite = satellite->getIndex();
            isl.targetSatellite = next->getOwnerModule()->getIndex();
            islCapacityBps += isl.capacityBps;
            islQueues.emplace(queue, &isl);
            channel->subscribe(channelSentSignal, this);
            channel->subscribe(channelDiscardSignal, this);
            channel->subscribe(POST_MODEL_CHANGE, this);
            queue->subscribe(queueAttemptSignal, this);
        }
    }
}

void WorkloadMetrics::watchTerminalAccess()
{
    auto *network = getParentModule();
    if (!network->hasSubmoduleVector("userTerminal"))
        return;
    terminalAccess.resize(network->getSubmoduleVectorSize("userTerminal"));
    for (size_t index = 0; index < terminalAccess.size(); ++index) {
        if (auto *terminal = network->getSubmodule("userTerminal", index)) {
            // Subscribe on the host so PPP replacements at handovers need no listener updates.
            terminal->subscribe(accessUplinkSignal, this);
            terminal->subscribe(accessDownlinkSignal, this);
        }
    }
}

void WorkloadMetrics::finishIslSecond(Isl& isl, int64_t ticksInSecond)
{
    if (isl.busyTicks == 0 || ticksInSecond <= 0)
        return;
    if (coreBySecond.size() <= isl.second)
        coreBySecond.resize(isl.second + 1);
    auto& second = coreBySecond[isl.second];
    const std::pair<int, int> link{isl.sourceSatellite, isl.targetSatellite};
    const std::pair<int, int> peak{second.peakSourceSatellite, second.peakTargetSatellite};
    if (isl.busyTicks > second.peakBusyTicks || (isl.busyTicks == second.peakBusyTicks && link < peak)) {
        second.peakBusyTicks = isl.busyTicks;
        second.peakSourceSatellite = link.first;
        second.peakTargetSatellite = link.second;
    }
    const long double fraction = static_cast<long double>(isl.busyTicks) / ticksInSecond;
    second.linksAt80Percent += fraction >= 0.80L;
    second.linksAt95Percent += fraction >= 0.95L;
    isl.totalBusyTicks += isl.busyTicks;
    isl.busyTicks = 0;
}

void WorkloadMetrics::recordIslBusyInterval(Isl& isl, simtime_t until)
{
    // Account for the previous frame when the next frame begins or the run ends.
    // This clips a frame in flight at the cutoff and handles second boundaries.
    int64_t start = isl.lastStart.raw();
    const int64_t stop = std::min(isl.lastStart + isl.lastDuration, until).raw();
    while (start < stop) {
        const auto second = static_cast<size_t>(start / ticksPerSecond);
        if (isl.second != second) {
            finishIslSecond(isl, ticksPerSecond);
            isl.second = second;
        }
        const auto next = std::min(stop, start + ticksPerSecond - start % ticksPerSecond);
        isl.busyTicks += next - start;
        start = next;
    }
}

void WorkloadMetrics::recordAccessFrame(TerminalAccess& terminal, simsignal_t signal, const cPacket *packet)
{
    // Start uplink bytes at transmission and count only valid frames on downlink.
    auto& direction = signal == accessUplinkSignal ? terminal.uplink : terminal.downlink;
    const int64_t now = simTime().raw();
    size_t second = now / ticksPerSecond;
    if (second && simTime() == measurementEnd && now % ticksPerSecond == 0)
        second--;
    if (direction.bitsBySecond.size() <= second)
        direction.bitsBySecond.resize(second + 1);
    const auto bits = packet->getBitLength();
    direction.wireBits += bits;
    direction.bitsBySecond[second] += bits;
}

void WorkloadMetrics::recordAccessQueueDrop(cComponent *source, bool overflow)
{
    const auto *interface = getPppInterface(source, "queue");
    if (!interface)
        return;
    const auto *host = interface->getParentModule();
    if (!strcmp(host->getName(), "userTerminal")) {
        auto& terminal = terminalAccess[host->getIndex()];
        terminal.uplinkQueueDrops++;
        terminal.uplinkQueueOverflow += overflow;
    }
    else if (isSatellite(host, getParentModule())) {
        const auto *gate = host->gateHalf("pppg", cGate::OUTPUT, interface->getIndex())->getNextGate();
        if (gate && !strcmp(gate->getOwnerModule()->getName(), "userTerminal")) {
            auto& terminal = terminalAccess[gate->getOwnerModule()->getIndex()];
            terminal.downlinkQueueDrops++;
            terminal.downlinkQueueOverflow += overflow;
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
        const auto *drop = dynamic_cast<const inet::PacketDropDetails *>(details);
        const bool overflow = drop && drop->getReason() == inet::QUEUE_OVERFLOW;
        if (auto found = islQueues.find(source); found != islQueues.end()) {
            found->second->queueDrops++;
            islQueueDrops++;
            islQueueOverflow += overflow;
        }
        else
            recordAccessQueueDrop(source, overflow);
    }
    else if (signal == accessUplinkSignal || signal == accessDownlinkSignal) {
        if (const auto *interface = getPppInterface(source, "ppp"))
            recordAccessFrame(terminalAccess[interface->getParentModule()->getIndex()],
                              signal, check_and_cast<const cPacket *>(value));
    }
    else if (signal == channelSentSignal) {
        const auto *transmission = check_and_cast<cChannel::MessageSentSignalValue *>(value);
        const auto *packet = dynamic_cast<const cPacket *>(transmission->getMessage());
        if (!packet)
            return;
        auto& isl = isls.at(source);
        const auto start = transmission->getTimestamp(signal);
        recordIslBusyInterval(isl, start);
        isl.lastPacketBits = packet->getBitLength();
        isl.lastStart = start;
        isl.lastDuration = transmission->getChannelResult()->duration;
        isl.wireBitsStarted += isl.lastPacketBits;
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
    std::vector<FlowSecond> flowSeconds(secondCount);
    std::array<SizeAggregate, sizeBounds.size()> sizeClasses;
    std::map<std::pair<int, int>, PairAggregate> endpointPairs;
    uint64_t completed = 0;
    uint64_t established = 0;
    uint64_t withPayload = 0;
    uint64_t socketFailures = 0;
    uint64_t bytesOffered = 0;
    uint64_t bytesDelivered = 0;
    for (const auto& [_, flow] : flows) {
        const auto sizeIndex = std::lower_bound(sizeBounds.begin(), sizeBounds.end(), flow.expectedBytes) - sizeBounds.begin();
        auto& aggregate = sizeClasses[sizeIndex];
        auto& pair = endpointPairs[{flow.server, flow.client}];
        aggregate.offered++;
        aggregate.bytesOffered += flow.expectedBytes;
        aggregate.bytesDelivered += flow.deliveredBytes;
        pair.offered++;
        pair.bytesOffered += flow.expectedBytes;
        pair.bytesDelivered += flow.deliveredBytes;
        bytesOffered += flow.expectedBytes;
        bytesDelivered += flow.deliveredBytes;
        socketFailures += flow.socketFailed;
        if (secondCount && flow.arrival <= end) {
            auto& bin = flowSeconds[measurementSecond(flow.arrival, end)];
            bin.arrivals++;
            bin.bytesOffered += flow.expectedBytes;
        }
        if (flow.established >= SIMTIME_ZERO) {
            established++;
            aggregate.setupSeconds.push_back((flow.established - flow.arrival).dbl());
        }
        if (flow.firstByte >= SIMTIME_ZERO) {
            withPayload++;
            aggregate.firstByteSeconds.push_back((flow.firstByte - flow.arrival).dbl());
        }
        if (flow.completion >= SIMTIME_ZERO) {
            completed++;
            pair.completed++;
            if (secondCount)
                flowSeconds[measurementSecond(flow.completion, end)].completions++;
            const double fct = (flow.completion - flow.arrival).dbl();
            aggregate.completionSeconds.push_back(fct);
            if (flow.expectedBytes <= sizeBounds[0])
                pair.shortFctSeconds.push_back(fct);
            if (fct > 0) {
                aggregate.goodputMbps.push_back(flow.expectedBytes * 8e-6 / fct);
                if (flow.expectedBytes > longFlowMinExclusive)
                    pair.longGoodputMbps.push_back(flow.expectedBytes * 8e-6 / fct);
            }
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
        << ", \"established_flows\": " << established << ", \"flows_with_payload\": " << withPayload
        << ", \"flows_with_socket_failure\": " << socketFailures
        << ",\n  \"offered_bytes\": " << bytesOffered << ", \"delivered_bytes\": " << bytesDelivered
        << ",\n  \"quantile_percent_step\": " << 100 / quantileIntervals
        << ",\n  \"definitions\": {"
           "\"flows_with_socket_failure\": \"Downloads with a socket failure reported to either workload endpoint\", "
           "\"setup_seconds\": \"Time from workload arrival to sender connection establishment\", "
           "\"first_byte_seconds\": \"Time from workload arrival to the first in-order payload delivery\", "
           "\"fct_seconds\": \"Time from workload arrival to the delivery of the final payload byte\", "
           "\"goodput_mbps\": \"Received payload bits divided by FCT for completed flows\", "
           "\"endpoint_seconds\": \"Per-second workload arrivals, delivered payload, completions and outstanding downloads including failures\", "
           "\"endpoint_pairs\": \"Totals, FCT for completed downloads up to 100 kB and goodput for completed downloads over 1 MB\", "
           "\"transport_recovery\": \"Terminal TCP retransmission timeout events and meta-level MPTCP payload reinjections\", "
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
            << ", \"setup_seconds\": ";
        writeDistribution(out, aggregate.setupSeconds);
        out << ", \"first_byte_seconds\": ";
        writeDistribution(out, aggregate.firstByteSeconds);
        out << ", \"fct_seconds\": ";
        writeDistribution(out, aggregate.completionSeconds);
        out << ", \"goodput_mbps\": ";
        writeDistribution(out, aggregate.goodputMbps);
        out << '}';
    }
    out << "\n  ],\n  \"endpoint_pairs\": [";
    bool firstPair = true;
    for (auto& [endpoints, summary] : endpointPairs) {
        if (!firstPair)
            out << ',';
        firstPair = false;
        out << "\n    {\"server\": " << endpoints.first << ", \"client\": " << endpoints.second
            << ", \"offered_flows\": " << summary.offered << ", \"completed_flows\": " << summary.completed
            << ", \"offered_bytes\": " << summary.bytesOffered << ", \"delivered_bytes\": " << summary.bytesDelivered
            << ", \"short_fct_seconds\": ";
        writeDistribution(out, summary.shortFctSeconds, false);
        out << ", \"long_goodput_mbps\": ";
        writeDistribution(out, summary.longGoodputMbps, false);
        out << '}';
    }
    uint64_t reinjectedBytes = 0;
    for (const auto& entry : reinjectedBytesByConnection)
        reinjectedBytes += entry.second;
    out << "\n  ],\n  \"transport_recovery\": {\"tcp_retransmission_timer_events\": " << tcpRtos
        << ", \"mptcp_reinjections\": " << metaReinjections
        << ", \"mptcp_reinjected_bytes\": " << reinjectedBytes << '}'
        << ",\n  \"fairness_long_min_exclusive_bytes\": " << longFlowMinExclusive
        << ",\n  \"endpoint_seconds\": [";
    uint64_t outstanding = 0;
    for (size_t second = 0; second < secondCount; ++second) {
        const auto& bin = fairness[second];
        const auto& counts = flowSeconds[second];
        outstanding += counts.arrivals;
        outstanding -= counts.completions;
        if (second)
            out << ',';
        out << "\n    {\"start_s\": " << second << ", \"end_s\": " << std::min(end.dbl(), double(second + 1))
            << ", \"arrivals\": " << counts.arrivals << ", \"completions\": " << counts.completions
            << ", \"outstanding_at_end\": " << outstanding << ", \"offered_bytes\": " << counts.bytesOffered
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
    out << '}';
    writeLinkMetrics(out, end);
    out << "\n}\n";
    out.close();
    if (!out)
        throw cRuntimeError("Failed writing workload metrics: %s", temporary.c_str());
    std::filesystem::rename(temporary, target);
}

void WorkloadMetrics::writeLinkMetrics(std::ostream& out, simtime_t end)
{
    std::vector<const Isl *> mostUsed;
    for (auto& [_, isl] : isls) {
        recordIslBusyInterval(isl, end);
        finishIslSecond(isl, std::min(ticksPerSecond, end.raw() - static_cast<int64_t>(isl.second) * ticksPerSecond));
        if (isl.totalBusyTicks > 0 || isl.queueDrops > 0)
            mostUsed.push_back(&isl);
    }
    constexpr size_t topLinkCount = 8;
    const auto topCount = std::min(topLinkCount, mostUsed.size());
    std::partial_sort(mostUsed.begin(), mostUsed.begin() + topCount, mostUsed.end(),
                      [](const Isl *a, const Isl *b) {
                          if (a->totalBusyTicks != b->totalBusyTicks)
                              return a->totalBusyTicks > b->totalBusyTicks;
                          if (a->sourceSatellite != b->sourceSatellite)
                              return a->sourceSatellite < b->sourceSatellite;
                          return a->targetSatellite < b->targetSatellite;
                      });

    out << ",\n  \"link_pressure\": {"
           "\"isl_busy_fraction_definition\": \"Fraction of interval spent serializing a frame on a directed ISL\", "
           "\"access_definition\": \"PPP wire frames started on uplink and accepted at terminal on downlink. "
           "Downlink queue drops are attributed while the satellite queue is attached to the terminal.\", "
           "\"busiest_isls\": [";
    for (size_t index = 0; index < topCount; ++index) {
        const auto& isl = *mostUsed[index];
        if (index)
            out << ',';
        out << "\n    {\"source_satellite\": " << isl.sourceSatellite
            << ", \"target_satellite\": " << isl.targetSatellite
            << ", \"wire_bits_started\": " << isl.wireBitsStarted
            << ", \"queue_drops\": " << isl.queueDrops << ", \"busy_fraction\": ";
        writeRatio(out, isl.totalBusyTicks, end.raw());
        out << '}';
    }
    out << "\n  ], \"terminal_access\": [";
    for (size_t index = 0; index < terminalAccess.size(); ++index) {
        const auto& terminal = terminalAccess[index];
        if (index)
            out << ',';
        out << "\n    {\"terminal\": " << index
            << ", \"uplink_wire_bits_started\": " << terminal.uplink.wireBits
            << ", \"downlink_wire_bits_received\": " << terminal.downlink.wireBits
            << ", \"uplink_queue_drops\": " << terminal.uplinkQueueDrops
            << ", \"uplink_queue_overflow\": " << terminal.uplinkQueueOverflow
            << ", \"downlink_queue_drops\": " << terminal.downlinkQueueDrops
            << ", \"downlink_queue_overflow\": " << terminal.downlinkQueueOverflow << '}';
    }
    out << "\n  ], \"seconds\": [";
    coreBySecond.resize(static_cast<size_t>(std::ceil(end.dbl())));
    for (size_t second = 0; second < coreBySecond.size(); ++second) {
        const auto& core = coreBySecond[second];
        const auto ticksInSecond = std::min(ticksPerSecond, end.raw() - static_cast<int64_t>(second) * ticksPerSecond);
        uint64_t peakUplinkBits = 0;
        uint64_t peakDownlinkBits = 0;
        int peakUplinkTerminal = -1;
        int peakDownlinkTerminal = -1;
        for (size_t index = 0; index < terminalAccess.size(); ++index) {
            const auto& terminal = terminalAccess[index];
            if (second < terminal.uplink.bitsBySecond.size() && terminal.uplink.bitsBySecond[second] > peakUplinkBits) {
                peakUplinkBits = terminal.uplink.bitsBySecond[second];
                peakUplinkTerminal = index;
            }
            if (second < terminal.downlink.bitsBySecond.size() && terminal.downlink.bitsBySecond[second] > peakDownlinkBits) {
                peakDownlinkBits = terminal.downlink.bitsBySecond[second];
                peakDownlinkTerminal = index;
            }
        }
        if (second)
            out << ',';
        out << "\n    {\"start_s\": " << second << ", \"end_s\": " << std::min(end.dbl(), double(second + 1))
            << ", \"peak_isl_busy_fraction\": ";
        writeRatio(out, core.peakBusyTicks, ticksInSecond);
        out << ", \"peak_isl\": ";
        if (core.peakSourceSatellite >= 0)
            out << '[' << core.peakSourceSatellite << ',' << core.peakTargetSatellite << ']';
        else
            out << "null";
        out << ", \"isls_at_80pct\": " << core.linksAt80Percent
            << ", \"isls_at_95pct\": " << core.linksAt95Percent
            << ", \"peak_access_uplink_mbps\": ";
        writeRatio(out, peakUplinkBits * static_cast<long double>(ticksPerSecond) * 1e-6L, ticksInSecond);
        out << ", \"peak_access_uplink_terminal\": " << peakUplinkTerminal;
        out << ", \"peak_access_downlink_mbps\": ";
        writeRatio(out, peakDownlinkBits * static_cast<long double>(ticksPerSecond) * 1e-6L, ticksInSecond);
        out << ", \"peak_access_downlink_terminal\": " << peakDownlinkTerminal;
        out << '}';
    }
    out << "\n  ]}";
}

} // namespace mporbexperiments
