#!/usr/bin/env python3
"""Run the LEO bottleneck experiment with 100 Mbps per shared rank."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'experimentLeoLoadBalanceBackground'))
from runExperimentLeoLoadBalanceBackground import run


if __name__ == '__main__':
    run(experiment='experimentLoadBalanceBottleneck')
