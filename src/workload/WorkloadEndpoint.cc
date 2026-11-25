#include "WorkloadEndpoint.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdlib>
#include <fstream>
#include <limits>
#include <sstream>
#include <string>
#include <tuple>
#include <unordered_set>

#include "inet/common/InitStages.h"
#include "inet/common/packet/chunk/ByteCountChunk.h"
#include "inet/common/socket/SocketTag_m.h"
#include "inet/networklayer/common/L3AddressResolver.h"

#include "WorkloadMetrics.h"

namespace mporbexperiments {

using namespace inet;
using namespace omnetpp;

Define_Module(WorkloadEndpoint);

WorkloadEndpoint::~WorkloadEndpoint()
{
    cancelAndDelete(dispatch);
}

int WorkloadEndpoint::numInitStages() const
{
    return NUM_INIT_STAGES;
}

void WorkloadEndpoint::initialize(int stage)
{
    if (stage != INITSTAGE_LAST)
        return;

    metrics = check_and_cast<WorkloadMetrics *>(getModuleByPath(par("metricsModule").stringValue()));
    activePortBase = par("activePortBase");
    passivePortBase = par("passivePortBase");
    portStride = par("portStride");
    if (par("fullMssPadding").boolValue()) {
        paddingMss = getParentModule()->getSubmodule("tcp")->par("mss").intValue();
        if (paddingMss <= 0)
            throw cRuntimeError("Full-MSS padding requires a positive TCP MSS");
    }
    if (activePortBase < 1 || activePortBase > 65535 || passivePortBase < 1 || passivePortBase > 65535 ||
            activePortBase == passivePortBase || portStride < 1)
        throw cRuntimeError("Invalid workload port blocks");

    readWorkload(par("workloadFile").stringValue());
    dispatch = new cMessage("workload-starts");
    if (!transfers.empty())
        scheduleAt(transfers.front().at, dispatch);
}

bool WorkloadEndpoint::laterTransfer(const Transfer& a, const Transfer& b)
{
    return std::tie(a.at, a.id) > std::tie(b.at, b.id);
}

void WorkloadEndpoint::readWorkload(const char *path)
{
    std::ifstream input(path);
    std::string line;
    auto readLine = [&]() {
        if (!std::getline(input, line))
            return false;
        if (!line.empty() && line.back() == '\r')
            line.pop_back();
        return true;
    };
    if (!readLine() || line != "id,start_s,server,client,bytes,port_slot")
        throw cRuntimeError("Missing or invalid workload header in '%s'", path);
    const int terminal = getParentModule()->getIndex();
    const auto *network = getParentModule()->getParentModule();
    std::unordered_set<int> localPorts;
    int lineNumber = 1;
    while (readLine()) {
        ++lineNumber;
        std::istringstream row(line);
        std::array<std::string, 6> fields;
        for (auto& field : fields) {
            if (!std::getline(row, field, ','))
                throw cRuntimeError("Incomplete workload row %d in '%s'", lineNumber, path);
        }
        if (row.peek() != std::char_traits<char>::eof() || line.back() == ',')
            throw cRuntimeError("Extra workload columns on row %d in '%s'", lineNumber, path);

        auto number = [&](std::size_t column, std::int64_t max) {
            const std::string& value = fields[column];
            std::int64_t result = 0;
            const auto end = value.data() + value.size();
            const auto parsed = std::from_chars(value.data(), end, result);
            if (parsed.ec != std::errc() || parsed.ptr != end || result < 0 || result > max)
                throw cRuntimeError("Invalid workload integer at row %d, column %zu in '%s'",
                                    lineNumber, column + 1, path);
            return result;
        };
        const int server = number(2, std::numeric_limits<int>::max());
        const int client = number(3, std::numeric_limits<int>::max());
        if (server != terminal && client != terminal)
            continue;
        const int id = number(0, std::numeric_limits<int>::max());
        const auto size = number(4, std::numeric_limits<std::int32_t>::max());
        const int slot = number(5, 65535);
        const std::int64_t lastOffset = (static_cast<std::int64_t>(slot) + 1) * portStride - 1;
        const simtime_t arrival = SimTime::parse(fields[1].c_str());
        if (size == 0 || arrival < SIMTIME_ZERO || server == client ||
                lastOffset + std::max(activePortBase, passivePortBase) > 65535 ||
                lastOffset >= std::abs(activePortBase - passivePortBase) ||
                network->getSubmodule("userTerminal", server) == nullptr ||
                network->getSubmodule("userTerminal", client) == nullptr)
            throw cRuntimeError("Invalid workload transfer on row %d in '%s'", lineNumber, path);

        const bool sending = server == terminal;
        const int port = (sending ? activePortBase : passivePortBase) + slot * portStride;
        if (!localPorts.insert(port).second)
            throw cRuntimeError("Workload slot %d reuses a port at terminal %d", slot, terminal);
        // The passive socket and its subflow listeners must exist before the SYN.
        transfers.push_back({arrival + (sending ? SimTime::fromRaw(1) : SIMTIME_ZERO),
                             id, slot, size, client, sending});
        if (!sending)
            metrics->registerFlow(id, size, arrival, server, client, port, portStride);
    }
    if (!input.eof())
        throw cRuntimeError("Error reading workload '%s'", path);

    std::make_heap(transfers.begin(), transfers.end(), laterTransfer);
}

bool WorkloadEndpoint::open(const Transfer& transfer)
{
    L3Address destination;
    if (transfer.sending) {
        auto *client = getParentModule()->getParentModule()->getSubmodule("userTerminal", transfer.client);
        // LeoSatellites assigns this address on the dynamically created PPP interface.
        // A previously attached terminal retains the address during a handover.
        destination = L3AddressResolver().addressOf(client, L3AddressResolver::ADDR_IPv4);
        if (destination.isUnspecified())
            return false;
    }

    auto socket = std::make_unique<TcpSocket>();
    socket->setCallback(this);
    socket->setOutputGate(gate("socketOut"));
    const int id = socket->getSocketId();
    const int portOffset = transfer.slot * portStride;
    auto& session = sessions.emplace(id, Session{std::move(socket), transfer.id, transfer.bytes, 0, transfer.sending}).first->second;
    if (transfer.sending) {
        session.socket->bind(activePortBase + portOffset);
        session.socket->connect(destination, passivePortBase + portOffset);
    }
    else {
        session.socket->bind(passivePortBase + portOffset);
        session.socket->listenOnce();
    }
    return true;
}

void WorkloadEndpoint::handleMessage(cMessage *msg)
{
    if (msg == dispatch) {
        while (!transfers.empty() && transfers.front().at == simTime()) {
            std::pop_heap(transfers.begin(), transfers.end(), laterTransfer);
            Transfer transfer = transfers.back();
            transfers.pop_back();
            if (!open(transfer)) {
                // A terminal that has never attached does not have an IPv4 address yet.
                transfer.at += SimTime(100, SIMTIME_MS);
                transfers.push_back(transfer);
                std::push_heap(transfers.begin(), transfers.end(), laterTransfer);
            }
        }
        if (!transfers.empty())
            scheduleAt(transfers.front().at, dispatch);
        return;
    }

    const auto tag = check_and_cast<ITaggedObject *>(msg)->getTags().findTag<SocketInd>();
    auto session = tag ? sessions.find(tag->getSocketId()) : sessions.end();
    if (session == sessions.end()) {
        delete msg;
        return;
    }
    session->second.socket->processMessage(msg);
    if (session->second.finished)
        sessions.erase(session);
}

void WorkloadEndpoint::close(Session& session)
{
    if (!session.closeRequested) {
        session.closeRequested = true;
        session.socket->close();
    }
}

void WorkloadEndpoint::socketEstablished(TcpSocket *socket)
{
    auto& session = sessions.at(socket->getSocketId());
    metrics->watchDiagnosticConnection(session.flowId, getParentModule(), socket->getSocketId(), session.sending);
    if (!session.sending || session.payloadSent)
        return;
    metrics->connectionEstablished(session.flowId);
    // Stock single-path OrbCC sends full segments. Include the padding in its FIN sequence.
    auto wireBytes = session.bytes;
    if (paddingMss > 0)
        wireBytes += (paddingMss - wireBytes % paddingMss) % paddingMss;
    auto *packet = new Packet("workload-object");
    packet->insertAtBack(makeShared<ByteCountChunk>(B(wireBytes)));
    session.payloadSent = true;
    socket->send(packet);
    // MpTcp queues the FIN behind the whole object and can then send a short final segment.
    close(session);
}

void WorkloadEndpoint::socketDataArrived(TcpSocket *socket, Packet *packet, bool)
{
    std::unique_ptr<Packet> received(packet);
    auto& session = sessions.at(socket->getSocketId());
    const auto remaining = session.bytes - session.received;
    const auto bytes = paddingMss > 0 ? std::min<std::int64_t>(packet->getByteLength(), remaining) : packet->getByteLength();
    if (bytes == 0)
        return;
    if (session.sending || bytes > remaining)
        throw cRuntimeError("Unexpected payload on workload flow %d: %lld bytes arrived after %lld of %lld delivered",
                            session.flowId, static_cast<long long>(bytes), static_cast<long long>(session.received),
                            static_cast<long long>(session.bytes));
    session.received += bytes;
    metrics->receiveBytes(session.flowId, bytes);
    if (session.received == session.bytes)
        close(session);
}

void WorkloadEndpoint::socketAvailable(TcpSocket *, TcpAvailableInfo *)
{
    throw cRuntimeError("Workload sockets must use nonforking listeners");
}

void WorkloadEndpoint::socketPeerClosed(TcpSocket *socket)
{
    auto& session = sessions.at(socket->getSocketId());
    // The master may receive its FIN before the remaining data arrives on the other subflows.
    if (session.sending || session.received == session.bytes)
        close(session);
}

void WorkloadEndpoint::socketClosed(TcpSocket *socket)
{
    sessions.at(socket->getSocketId()).finished = true;
}

void WorkloadEndpoint::socketFailure(TcpSocket *socket, int)
{
    auto& session = sessions.at(socket->getSocketId());
    metrics->socketFailed(session.flowId);
    session.finished = true;
}

void WorkloadEndpoint::socketStatusArrived(TcpSocket *, TcpStatusInfo *status)
{
    delete status;
}

} // namespace mporbexperiments
