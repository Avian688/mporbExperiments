// SPDX-License-Identifier: LGPL-3.0-or-later
#include "WorkloadTcpDiagnostics.h"

#include "../../../mporb/src/transportlayer/tcp/MpOrbSubflowConnection.h"
#include "../../../mptcp/src/transportlayer/tcp/MpTcp.h"
#include "../../../mptcp/src/transportlayer/tcp/MpTcpConnection.h"

#include "inet/common/ProtocolTag_m.h"
#include "inet/common/Simsignals.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <filesystem>
#include <iomanip>
#include <locale>
#include <sstream>

namespace mporbexperiments {

using namespace omnetpp;

namespace {

constexpr const char *senderSubflowSignals[] = {
    "state", "cwnd", "rtt", "srtt", "rto", "numRtos", "paceRate", "additiveIncrease",
    "cwndLimited", "mbytesInFlight", "mbytesInFlightTotal", "mbytesLoss", "lossRecovery", "recoveryPoint",
    "subflowSendQueueBytes", "sndNxt", "sndWnd", "unacked", "dupAcks", "retransmissionRate",
    "U", "sharingFlows", "bottleneckBandwidth", "queueingDelay", "mpOrbForwardQueueingDelay",
    "mpOrbReverseQueueingDelay", "semiCoupledAlphaRateShare", "mpOrbDeltaSubflowRate",
    "mpOrbDeltaConnectionRate", "mpOrbDeltaCorrection", "mpOrbDeltaAppliedShuffle",
    "omegaAdditionalIncrease",
};
constexpr const char *receiverSubflowSignals[] = {"state", "throughput", "tcpRcvPayloadBytes", "tcpRcvQueueBytes"};
constexpr const char *senderMetaSignals[] = {"state", "sndNxt", "unacked", "metaReinjectedBytes", "metaReinjections"};
constexpr const char *receiverMetaSignals[] = {"state", "holBlockedBytes", "metaExpectedDsn", "metaDsnGapBytes"};

} // namespace

WorkloadTcpDiagnostics::WorkloadTcpDiagnostics(const std::string& directory, const std::string& flowIds,
                                               bool detailedSender) :
    detailedSender(detailedSender),
    binWidth(SimTime(100, SIMTIME_MS).raw()),
    utilizationSignal(cComponent::registerSignal("U")),
    payloadSignal(cComponent::registerSignal("tcpRcvPayloadBytes")),
    pintHopSignal(cComponent::registerSignal("sampledPintHopId")),
    appBytesSignal(cComponent::registerSignal("appDeliveredBytes")),
    sentPayloadSignal(cComponent::registerSignal("tcpSentPayloadBytes")),
    appliedShuffleSignal(cComponent::registerSignal("mpOrbDeltaAppliedShuffle")),
    omegaBoostSignal(cComponent::registerSignal("omegaAdditionalIncrease"))
{
    std::istringstream ids(flowIds);
    std::string token;
    while (ids >> token) {
        int id = 0;
        const auto parsed = std::from_chars(token.data(), token.data() + token.size(), id);
        if (parsed.ec != std::errc() || parsed.ptr != token.data() + token.size() || id <= 0)
            throw cRuntimeError("Invalid workload diagnostic flow ID '%s'", token.c_str());
        selectedFlows.insert(id);
    }
    if (selectedFlows.empty() || directory.empty())
        throw cRuntimeError("Workload TCP diagnostics need a directory and at least one flow ID");

    const std::filesystem::path output(directory);
    std::filesystem::create_directories(output);
    vectors.open(output / "tcp_vectors.csv");
    hops.open(output / "pint_hops.csv");
    if (!vectors || !hops)
        throw cRuntimeError("Cannot open TCP diagnostic CSVs in '%s'", directory.c_str());

    vectors.imbue(std::locale::classic());
    vectors << std::setprecision(12)
            << "flow_id,endpoint,terminal,rank,local_port,module_id,metric,bin_start_s,"
               "first_time_s,last_time_s,first,min,max,last,count,sum\n";
    hops << "hop_id,node_path\n";
    if (detailedSender) {
        adjustments.open(output / "window_adjustments.csv");
        if (!adjustments)
            throw cRuntimeError("Cannot open window adjustment CSV in '%s'", directory.c_str());
        adjustments.imbue(std::locale::classic());
        adjustments << std::setprecision(12) << "flow_id,terminal,rank,time_s,metric,value\n";
    }
}

bool WorkloadTcpDiagnostics::includes(int flowId) const
{
    return selectedFlows.find(flowId) != selectedFlows.end();
}

void WorkloadTcpDiagnostics::watch(int flowId, cModule *terminal, int socketId, bool sender)
{
    if (!includes(flowId))
        return;

    auto *tcp = dynamic_cast<inet::tcp::MpTcp *>(terminal->getSubmodule("tcp"));
    auto *meta = tcp ? tcp->getMetaConnection(socketId) : nullptr;
    if (!meta)
        return;

    const int basePort = meta->getLocalPortNumber();
    const Identity identity{flowId, sender, terminal->getIndex(), 0, basePort, meta->getId()};
    watchConnection(meta, identity);
    if (!sender) {
        auto appIdentity = identity;
        appIdentity.rank = -1;
        appIdentity.moduleId = -1;
        appTraces.try_emplace(flowId, Trace{appIdentity, {}}).first->second.identity = appIdentity;
    }

    std::unordered_map<int, int> *senderPorts = nullptr;
    if (sender && detailedSender) {
        auto *ipv4 = terminal->getModuleByPath(".ipv4.ip");
        if (!ipv4)
            throw cRuntimeError("Missing sender IPv4 module at %s", terminal->getFullPath().c_str());
        auto [position, inserted] = senderSubflowsByIpv4.try_emplace(ipv4);
        if (inserted)
            ipv4->subscribe(inet::packetReceivedFromUpperSignal, this);
        senderPorts = &position->second;
    }

    // The saved path rank is encoded by the offset within the flow's port block.
    for (auto *subflow : meta->getSubflows()) {
        const int port = subflow->getLocalPort();
        if (port <= basePort)
            continue;
        watchConnection(subflow, Identity{flowId, sender, terminal->getIndex(), port - basePort, port, subflow->getId()});
        if (senderPorts)
            (*senderPorts)[port] = subflow->getId();
    }
}

void WorkloadTcpDiagnostics::watchConnection(cModule *module, const Identity& identity)
{
    if (!tcpTraces.try_emplace(module->getId(), Trace{identity, {}}).second)
        return;

    const auto subscribe = [this, module](const auto& signals) {
        for (const auto *name : signals)
            module->subscribe(cComponent::registerSignal(name), this);
    };
    if (identity.rank == 0) {
        if (identity.sender)
            subscribe(senderMetaSignals);
        else
            subscribe(receiverMetaSignals);
    }
    else if (identity.sender)
        subscribe(senderSubflowSignals);
    else
        subscribe(receiverSubflowSignals);

    // The establishment callback follows the first TCP state transitions.
    const auto *connection = check_and_cast<const inet::tcp::TcpConnection *>(module);
    sample(module, cComponent::registerSignal("state"), connection->getFsmState());
}

void WorkloadTcpDiagnostics::received(int flowId, int64_t deliveredBytes)
{
    if (!includes(flowId))
        return;

    auto& trace = appTraces.try_emplace(flowId, Trace{Identity{flowId, false, -1, -1, -1, -1}, {}}).first->second;
    add(trace, appBytesSignal, deliveredBytes);
}

void WorkloadTcpDiagnostics::sample(cComponent *source, simsignal_t signal, double value)
{
    if (!std::isfinite(value) || (signal == payloadSignal && value <= 0))
        return;
    auto& trace = tcpTraces.at(source->getId());
    add(trace, signal, value);
    if (detailedSender && value != 0 && (signal == appliedShuffleSignal || signal == omegaBoostSignal)) {
        const auto& id = trace.identity;
        adjustments << id.flowId << ',' << id.terminal << ',' << id.rank << ',' << simTime()
                    << ',' << cComponent::getSignalName(signal) << ',' << value << '\n';
    }
    if (signal == utilizationSignal)
        samplePintHop(source, trace);
}

void WorkloadTcpDiagnostics::samplePintHop(cComponent *source, Trace& trace)
{
    const auto found = trace.vectors.find(pintHopSignal);
    if (found != trace.vectors.end() && found->second.start == simTime().raw() / binWidth * binWidth)
        return;

    // Take the first accepted PINT sample per bin. The ID is the reporting
    // node, which may have several interfaces, including terminal access.
    const auto *subflow = dynamic_cast<const inet::tcp::MpOrbSubflowConnection *>(source);
    if (!subflow)
        return;
    const auto data = subflow->getCurrentIntData();
    if (data.empty())
        return;
    const int id = data.front().getHopId();
    if (id < 0)
        return;

    add(trace, pintHopSignal, id);
    if (pintHopIds.insert(id).second) {
        const auto *node = getSimulation()->getModule(id);
        hops << id << ',' << (node ? node->getFullPath() : "") << '\n';
    }
}

void WorkloadTcpDiagnostics::add(Trace& trace, simsignal_t signal, double value)
{
    const int64_t tick = simTime().raw();
    const int64_t start = tick / binWidth * binWidth;
    auto& bin = trace.vectors[signal];
    if (bin.count == 0 || bin.start != start) {
        if (bin.count > 0)
            write(trace.identity, signal, bin);
        bin = Bin{start, tick, tick, 1, value, value, value, value, value};
        return;
    }

    bin.lastTime = tick;
    bin.count++;
    bin.minimum = std::min(bin.minimum, value);
    bin.maximum = std::max(bin.maximum, value);
    bin.last = value;
    bin.sum += value;
}

void WorkloadTcpDiagnostics::write(const Identity& identity, simsignal_t signal, const Bin& bin)
{
    vectors << identity.flowId << ',' << (identity.sender ? "sender" : "receiver") << ','
            << identity.terminal << ',' << identity.rank << ',' << identity.localPort << ','
            << identity.moduleId << ',' << cComponent::getSignalName(signal) << ','
            << SimTime::fromRaw(bin.start) << ',' << SimTime::fromRaw(bin.firstTime) << ','
            << SimTime::fromRaw(bin.lastTime) << ',' << bin.first << ',' << bin.minimum << ','
            << bin.maximum << ',' << bin.last << ',' << bin.count << ',' << bin.sum << '\n';
}

void WorkloadTcpDiagnostics::finish()
{
    unsubscribeAll();
    const auto flush = [this](const auto& traces) {
        for (const auto& entry : traces)
            for (const auto& vector : entry.second.vectors)
                if (vector.second.count > 0)
                    write(entry.second.identity, vector.first, vector.second);
    };
    flush(tcpTraces);
    flush(appTraces);
    vectors.close();
    hops.close();
    if (!vectors || !hops)
        throw cRuntimeError("Failed writing workload TCP diagnostic CSVs");
    if (detailedSender) {
        adjustments.close();
        if (!adjustments)
            throw cRuntimeError("Failed writing workload window adjustment CSV");
    }
}

void WorkloadTcpDiagnostics::receiveSignal(cComponent *source, simsignal_t signal, cObject *value, cObject *)
{
    if (signal != inet::packetReceivedFromUpperSignal)
        return;
    const auto *packet = check_and_cast<const inet::Packet *>(value);
    if (packet->getDataLength() <= inet::tcp::TCP_MIN_HEADER_LENGTH)
        return;
    const auto& protocol = packet->findTag<inet::PacketProtocolTag>();
    if (!protocol || protocol->getProtocol() != &inet::Protocol::tcp)
        return;
    const auto& header = packet->peekAtFront<inet::tcp::TcpHeader>();
    const auto& ports = senderSubflowsByIpv4.at(source);
    const auto match = ports.find(header->getSrcPort());
    if (match == ports.end())
        return;
    const auto bytes = packet->getByteLength() - header->getHeaderLength().get();
    if (bytes > 0)
        add(tcpTraces.at(match->second), sentPayloadSignal, bytes);
}

void WorkloadTcpDiagnostics::receiveSignal(cComponent *source, simsignal_t signal, bool value, cObject *)
{
    sample(source, signal, value);
}

void WorkloadTcpDiagnostics::receiveSignal(cComponent *source, simsignal_t signal, intval_t value, cObject *)
{
    sample(source, signal, value);
}

void WorkloadTcpDiagnostics::receiveSignal(cComponent *source, simsignal_t signal, uintval_t value, cObject *)
{
    sample(source, signal, value);
}

void WorkloadTcpDiagnostics::receiveSignal(cComponent *source, simsignal_t signal, double value, cObject *)
{
    sample(source, signal, value);
}

void WorkloadTcpDiagnostics::receiveSignal(cComponent *source, simsignal_t signal, const SimTime& value, cObject *)
{
    sample(source, signal, value.dbl());
}

} // namespace mporbexperiments
