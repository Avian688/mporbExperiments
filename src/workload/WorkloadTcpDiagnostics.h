// SPDX-License-Identifier: LGPL-3.0-or-later
#ifndef __MPORBEXPERIMENTS_WORKLOADTCPDIAGNOSTICS_H
#define __MPORBEXPERIMENTS_WORKLOADTCPDIAGNOSTICS_H

#include <omnetpp.h>

#include <cstdint>
#include <fstream>
#include <string>
#include <unordered_map>
#include <unordered_set>

namespace mporbexperiments {

class WorkloadTcpDiagnostics : private omnetpp::cListener
{
  public:
    WorkloadTcpDiagnostics(const std::string& directory, const std::string& flowIds,
                           bool detailedSender, bool traceWindowUpdates);

    bool includes(int flowId) const;
    void watch(int flowId, omnetpp::cModule *terminal, int socketId, bool sender);
    void received(int flowId, int64_t deliveredBytes);
    void finish();

  private:
    struct Identity {
        int flowId;
        bool sender;
        int terminal;
        int rank;
        int localPort;
        int moduleId;
    };

    struct Bin {
        int64_t start = 0;
        int64_t firstTime = 0;
        int64_t lastTime = 0;
        uint64_t count = 0;
        double first = 0;
        double minimum = 0;
        double maximum = 0;
        double last = 0;
        double sum = 0;
    };

    struct Trace {
        Identity identity;
        std::unordered_map<omnetpp::simsignal_t, Bin> vectors;
    };

    std::unordered_set<int> selectedFlows;
    std::unordered_map<int, Trace> tcpTraces;
    std::unordered_map<int, Trace> appTraces;
    std::unordered_map<omnetpp::cComponent *, std::unordered_map<int, int>> senderSubflowsByIpv4;
    std::unordered_set<int> pintHopIds;
    std::ofstream vectors;
    std::ofstream hops;
    std::ofstream adjustments;
    std::ofstream windowUpdates;
    const bool detailedSender;
    const bool traceWindowUpdates;
    int64_t binWidth;
    omnetpp::simsignal_t utilizationSignal;
    omnetpp::simsignal_t payloadSignal;
    omnetpp::simsignal_t pintHopSignal;
    omnetpp::simsignal_t appBytesSignal;
    omnetpp::simsignal_t sentPayloadSignal;
    omnetpp::simsignal_t appliedShuffleSignal;
    omnetpp::simsignal_t omegaBoostSignal;

    void watchConnection(omnetpp::cModule *module, const Identity& identity);
    void sample(omnetpp::cComponent *source, omnetpp::simsignal_t signal, double value);
    void samplePintHop(omnetpp::cComponent *source, Trace& trace);
    void add(Trace& trace, omnetpp::simsignal_t signal, double value);
    void write(const Identity& identity, omnetpp::simsignal_t signal, const Bin& bin);

    using omnetpp::cListener::finish;
    using omnetpp::cListener::receiveSignal;
    void receiveSignal(omnetpp::cComponent *source, omnetpp::simsignal_t signal,
                       bool value, omnetpp::cObject *) override;
    void receiveSignal(omnetpp::cComponent *source, omnetpp::simsignal_t signal,
                       omnetpp::intval_t value, omnetpp::cObject *) override;
    void receiveSignal(omnetpp::cComponent *source, omnetpp::simsignal_t signal,
                       omnetpp::uintval_t value, omnetpp::cObject *) override;
    void receiveSignal(omnetpp::cComponent *source, omnetpp::simsignal_t signal,
                       double value, omnetpp::cObject *) override;
    void receiveSignal(omnetpp::cComponent *source, omnetpp::simsignal_t signal,
                       const omnetpp::SimTime& value, omnetpp::cObject *) override;
    void receiveSignal(omnetpp::cComponent *source, omnetpp::simsignal_t signal,
                       omnetpp::cObject *value, omnetpp::cObject *) override;
};

} // namespace mporbexperiments

#endif
