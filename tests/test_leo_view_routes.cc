#include "visualizer/LeoViewRoutes.h"
#include <cassert>

using namespace inet::leoView;

int main()
{
    // Reverse traversal is the same physical link; a loop within one route
    // must not increase that route's contribution twice.
    auto edges = countEdges({{5,{0,1,2,1}}, {6,{3,2,1,4}}, {7,{5,1,2,6}}});
    auto shared = std::find_if(edges.begin(),edges.end(),[](const auto& edge) {
        return edge.first==1 && edge.second==2;
    });
    assert(shared!=edges.end() && shared->paths==3);
    assert(edges.size()==6);
    const auto single = edgeStroke({1,2,5,1},10,true);
    const auto twice = edgeStroke({1,2,5,2},10,true);
    const auto triple = edgeStroke(*shared,10,true);
    const auto busy = edgeStroke({1,2,5,8},10,true);
    assert(single.color==pairColor(5));
    assert(twice.color[0]>twice.color[1] && twice.color[1]>twice.color[2]);
    assert(triple.color[0]>5*triple.color[1]);
    assert(busy.color[0]<triple.color[0]);
    assert(single.width<twice.width && twice.width<triple.width && triple.width<busy.width);
    const auto access = edgeStroke({1,12,5,5},10,false);
    assert(access.color==single.color && access.width==single.width);
    assert(edgeStroke({1,2,5,3},10,false).color==triple.color);
    // Sharing a node or crossing on a map is not sharing a link.
    for (const auto& edge : countEdges({{5,{0,1,2}}, {6,{3,1,4}}}))
        assert(edge.paths==1);
    assert(countEdges({{5,{1}}, {6,{2,2}}}).empty());
}
