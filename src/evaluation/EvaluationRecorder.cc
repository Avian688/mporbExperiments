// SPDX-License-Identifier: LGPL-3.0-or-later
#include <algorithm>
#include <cstdint>
#include <fstream>
#include <vector>

#include <omnetpp.h>
#include "inet/common/InitStages.h"
#include "inet/common/Simsignals.h"
#include "inet/common/packet/Packet.h"
#include "../../../mptcp/src/transportlayer/tcp/SubflowConnection.h"

namespace mporbexperiments {

using namespace omnetpp;

class EvaluationRecorder : public cSimpleModule, public cListener
{
    int connections = 0;
    int ranks = 0;
    int passivePort = 0;
    int64_t interval = 0;
    int64_t end = 0;
    simsignal_t payloadSignal = SIMSIGNAL_NULL;
    std::vector<std::vector<uint64_t>> appBytes;
    std::vector<std::vector<uint64_t>> rankBytes;

    int numInitStages() const override { return inet::NUM_INIT_STAGES; }

    void initialize(int stage) override
    {
        if (stage != inet::INITSTAGE_LAST)
            return;
        connections = par("numConnections").intValue();
        ranks = par("firstConnectionRanks").intValue();
        passivePort = par("passivePort").intValue();
        interval = SimTime(par("interval")).raw();
        end = SimTime::parse(getEnvir()->getConfig()->getConfigValue("sim-time-limit")).raw();
        if (connections <= 0 || ranks < 0 || interval <= 0 || end <= 0)
            throw cRuntimeError("Invalid evaluation endpoint recorder configuration");
        const auto bins = (end + interval - 1) / interval;
        appBytes.assign(connections, std::vector<uint64_t>(bins));
        rankBytes.assign(ranks, std::vector<uint64_t>(bins));
        for (int connection = 0; connection < connections; ++connection) {
            auto *server = getParentModule()->getSubmodule("server", connection);
            server->getSubmodule("app", 0)->subscribe(inet::packetReceivedSignal, this);
        }
        if (ranks) {
            payloadSignal = registerSignal("tcpRcvPayloadBytes");
            getParentModule()->getSubmodule("server", 0)->getSubmodule("tcp")->subscribe(payloadSignal, this);
        }
    }

    void receiveSignal(cComponent *source, simsignal_t signal, cObject *value, cObject *) override
    {
        if (signal != inet::packetReceivedSignal || simTime().raw() >= end)
            return;
        // A sink thread emits at delivery and propagates the signal to its app.
        const auto *module = check_and_cast<cModule *>(source);
        const int connection = module->getParentModule()->getParentModule()->getIndex();
        appBytes.at(connection)[simTime().raw() / interval] += check_and_cast<inet::Packet *>(value)->getByteLength();
    }

    void receiveSignal(cComponent *source, simsignal_t signal, intval_t value, cObject *) override
    {
        if (signal != payloadSignal || value <= 0 || simTime().raw() >= end)
            return;
        const auto *subflow = dynamic_cast<inet::tcp::SubflowConnection *>(source);
        const int rank = subflow ? subflow->getLocalPort() - passivePort : 0;
        if (rank >= 1 && rank <= ranks)
            rankBytes[rank - 1][simTime().raw() / interval] += value;
    }

    void finish() override
    {
        std::ofstream out(par("outputFile").stringValue());
        out << "start_s,end_s";
        for (int connection = 0; connection < connections; ++connection)
            out << ",connection_" << connection << "_bytes";
        for (int rank = 1; rank <= ranks; ++rank)
            out << ",a_rank_" << rank << "_rx_bytes";
        out << '\n';
        for (int64_t start = 0, bin = 0; start < end; start += interval, ++bin) {
            out << SimTime::fromRaw(start) << ',' << SimTime::fromRaw(std::min(start + interval, end));
            for (const auto& bytes : appBytes)
                out << ',' << bytes[bin];
            for (const auto& bytes : rankBytes)
                out << ',' << bytes[bin];
            out << '\n';
        }
        out.close();
        if (!out)
            throw cRuntimeError("Failed to write evaluation endpoint counters");
    }
};

Define_Module(EvaluationRecorder);

} // namespace mporbexperiments
