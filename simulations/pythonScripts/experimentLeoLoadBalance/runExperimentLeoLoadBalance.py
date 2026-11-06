#!/usr/bin/env python3
"""Run ExperimentLeo's matrix on the saved 170–270 s topology window."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'experimentLeo'))
import leo
from runExperimentLeo import run


if __name__ == '__main__':
    run(replay_start=leo.LOAD_BALANCE_START, default_duration=leo.LOAD_BALANCE_DURATION)
