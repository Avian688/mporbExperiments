#ifndef MPORBEXPERIMENTS_WORKLOAD_WORKLOADENDPOINT_H
#define MPORBEXPERIMENTS_WORKLOAD_WORKLOADENDPOINT_H

#include <cstdint>
#include <memory>
#include <unordered_map>
#include <vector>

#include "inet/transportlayer/contract/tcp/TcpSocket.h"

namespace mporbexperiments {

class WorkloadMetrics;

class WorkloadEndpoint : public omnetpp::cSimpleModule, public inet::TcpSocket::ICallback
{
  private:
    struct Transfer {
        omnetpp::simtime_t at;
        int id;
        int slot;
        std::int64_t bytes;
        int client;
        bool sending;
    };

    struct Session {
        std::unique_ptr<inet::TcpSocket> socket;
        int flowId;
        std::int64_t bytes;
        std::int64_t received = 0;
        bool sending;
        bool payloadSent = false;
        bool closeRequested = false;
        bool finished = false;
    };

    WorkloadMetrics *metrics = nullptr;
    omnetpp::cMessage *dispatch = nullptr;
    int activePortBase = 0;
    int passivePortBase = 0;
    int portStride = 0;
    int paddingMss = 0;
    std::vector<Transfer> transfers;
    std::unordered_map<int, Session> sessions;

    static bool laterTransfer(const Transfer& a, const Transfer& b);
    void readWorkload(const char *path);
    bool open(const Transfer& transfer);
    void close(Session& session);

  protected:
    int numInitStages() const override;
    void initialize(int stage) override;
    void handleMessage(omnetpp::cMessage *msg) override;

    void socketDataArrived(inet::TcpSocket *socket, inet::Packet *packet, bool urgent) override;
    void socketAvailable(inet::TcpSocket *socket, inet::TcpAvailableInfo *availableInfo) override;
    void socketEstablished(inet::TcpSocket *socket) override;
    void socketPeerClosed(inet::TcpSocket *socket) override;
    void socketClosed(inet::TcpSocket *socket) override;
    void socketFailure(inet::TcpSocket *socket, int code) override;
    void socketStatusArrived(inet::TcpSocket *socket, inet::TcpStatusInfo *status) override;
    void socketDeleted(inet::TcpSocket *socket) override {}

  public:
    ~WorkloadEndpoint() override;
};

} // namespace mporbexperiments

#endif
