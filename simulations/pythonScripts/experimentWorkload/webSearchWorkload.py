"""Generate finite downloads using the complete UCMP web-search workload.

CDF and sampling method:
https://github.com/ht-gong/UCMP/blob/master/traffic/websearch.csv
https://github.com/ht-gong/UCMP/blob/master/traffic/generate_websearch_traffic.m
"""

from bisect import bisect_left
import csv
import math
from pathlib import Path
import random
from typing import NamedTuple

from workloadTopology import PAIRS


DEFAULT_OFFERED_MBPS = 640
DEFAULT_WORKLOAD_SEED = 1


class Flow(NamedTuple):
    id: int
    start: float
    server: int
    client: int
    bytes: int


def _load_size_cdf():
    with Path(__file__).with_name('websearch.csv').open(encoding='utf-8', newline='') as source:
        sizes, cdf = zip(*((int(size), float(probability)) for size, probability in csv.reader(source)))
    if (sizes[0] <= 0 or cdf[0] != 0 or cdf[-1] != 1 or
            any(left >= right for left, right in zip(sizes, sizes[1:])) or
            any(not left < right for left, right in zip(cdf, cdf[1:]))):
        raise ValueError('Invalid web-search flow-size CDF')
    return sizes, cdf


FLOW_SIZES, SIZE_CDF = _load_size_cdf()
MEAN_FLOW_BYTES = math.fsum(size * (probability - preceding)
                           for size, preceding, probability in zip(FLOW_SIZES[1:], SIZE_CDF, SIZE_CDF[1:]))


def generate_flows(start, end, offered_mbps=DEFAULT_OFFERED_MBPS, workload_seed=DEFAULT_WORKLOAD_SEED):
    """Return time-ordered flows in [start, end) for an expected payload load in Mbps."""
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
        raise ValueError('The arrival window must have finite times with 0 <= start < end')
    arrival_rate = offered_mbps * (1_000_000 / (8 * MEAN_FLOW_BYTES))
    if not math.isfinite(arrival_rate) or arrival_rate <= 0:
        raise ValueError('Offered Mbps must give a finite, positive arrival rate')

    rng = random.Random(workload_seed)
    flows = []
    arrival = start
    while True:
        next_arrival = arrival + rng.expovariate(arrival_rate)
        if next_arrival >= end:
            return flows
        if next_arrival == arrival:
            raise ValueError('The interarrival time is below numerical precision')
        arrival = next_arrival
        server, client = rng.choice(PAIRS)
        # UCMP samples the first CDF point at or above the draw, including the largest flows.
        size = FLOW_SIZES[bisect_left(SIZE_CDF, rng.random())]
        flows.append(Flow(len(flows) + 1, arrival, server, client, size))
