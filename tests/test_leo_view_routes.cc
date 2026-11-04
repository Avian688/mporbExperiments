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
    const auto four = edgeStroke({1,2,5,4},10,true);
    const auto busy = edgeStroke({1,2,5,8},10,true);
    assert(single.color==pairColor(5));
    assert(twice.color[0]==1.f && twice.color[1]>.8f && twice.color[2]<.1f);
    assert(triple.color[0]==1.f && triple.color[1]>.4f && triple.color[1]<.7f);
    assert(four.color[0]>5*four.color[1]);
    assert(busy.color[0]<four.color[0]);
    assert(single.width<twice.width && twice.width<triple.width && triple.width<four.width && four.width<busy.width);
    const auto access = edgeStroke({1,12,5,5},10,false);
    assert(access.color==single.color && access.width==single.width);
    assert(edgeStroke({1,2,5,3},10,false).color==triple.color);
    // Sharing a node or crossing on a map is not sharing a link.
    for (const auto& edge : countEdges({{5,{0,1,2}}, {6,{3,1,4}}}))
        assert(edge.paths==1);
    assert(countEdges({{5,{1}}, {6,{2,2}}}).empty());

    // Only the common middle segment changes colour/thickness. Independent
    // prefixes/suffixes retain their pair colours, even when four paths meet.
    const std::vector<Route> partialOverlap = {
        {5,{10,11,1,2,12,13}}, {6,{20,21,1,2,22,23}},
        {7,{30,31,1,2,32,33}}, {8,{40,41,1,2,42,43}},
    };
    for (std::size_t count = 2; count <= partialOverlap.size(); ++count) {
        const std::vector<Route> selected(partialOverlap.begin(), partialOverlap.begin()+count);
        unsigned highlighted = 0;
        for (const auto& edge : countEdges(selected)) {
            const auto stroke = edgeStroke(edge,100,true);
            if (edge.first==1 && edge.second==2) {
                assert(edge.paths==count);
                const auto expected = count==2 ? twice : count==3 ? triple : four;
                assert(stroke.color==expected.color && stroke.width==expected.width);
                ++highlighted;
            }
            else {
                assert(edge.paths==1);
                assert(stroke.color==pairColor(edge.pairIndex));
                assert(stroke.width==single.width);
            }
        }
        assert(highlighted==1);
    }
}
