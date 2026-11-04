// SPDX-License-Identifier: LGPL-3.0-or-later
#ifndef MPORBEXPERIMENTS_LEOVIEWROUTES_H
#define MPORBEXPERIMENTS_LEOVIEWROUTES_H

#include <algorithm>
#include <array>
#include <cstdint>
#include <map>
#include <set>
#include <utility>
#include <vector>

namespace inet { namespace leoView {

struct Route {
    int pairIndex;
    std::vector<int32_t> nodeIds;
};

struct EdgeUse {
    int32_t first;
    int32_t second;
    int pairIndex;
    unsigned paths = 0;
};

struct Stroke {
    std::array<float, 4> color;
    float width;
};

inline std::array<float, 4> pairColor(int pairIndex)
{
    // Orange/red are reserved for overlap, not individual connections.
    static const std::array<float, 4> colors[] = {
        {0.20f, 0.65f, 1.00f, 1.0f}, {0.10f, 0.95f, 0.85f, 1.0f},
        {0.75f, 0.45f, 1.00f, 1.0f}, {0.35f, 0.90f, 0.30f, 1.0f},
        {0.90f, 0.95f, 1.00f, 1.0f},
    };
    return colors[pairIndex % 5];
}

inline std::vector<EdgeUse> countEdges(const std::vector<Route>& routes)
{
    std::map<std::pair<int32_t, int32_t>, EdgeUse> edges;
    for (const auto& route : routes) {
        std::set<std::pair<int32_t, int32_t>> seen;
        for (std::size_t i = 1; i < route.nodeIds.size(); ++i) {
            const int32_t a = route.nodeIds[i - 1], b = route.nodeIds[i];
            if (a == b)
                continue;
            const std::pair<int32_t, int32_t> key = std::minmax(a, b);
            if (!seen.insert(key).second)
                continue; // Count a physical link at most once per path.
            auto it = edges.emplace(key, EdgeUse{key.first, key.second, route.pairIndex, 0}).first;
            ++it->second.paths;
        }
    }
    std::vector<EdgeUse> result;
    for (const auto& entry : edges)
        result.push_back(entry.second);
    return result;
}

inline Stroke edgeStroke(const EdgeUse& edge, int coreNodes, bool includeAccessLinks)
{
    const unsigned count = includeAccessLinks || (edge.first < coreNodes && edge.second < coreNodes)
            ? edge.paths : 1;
    auto color = pairColor(edge.pairIndex);
    if (count == 2)
        color = {1.00f, 0.55f, 0.03f, 1.0f};
    else if (count >= 3) {
        const float shade = std::max(0.55f, 1.0f - 0.04f * (count - 3));
        color = {shade, 0.10f * shade, 0.08f * shade, 1.0f};
    }
    return {color, 2.5f + 0.8f * std::min(6u, count > 0 ? count - 1 : 0)};
}

} }
#endif
