#!/usr/bin/env python3
"""Run only the separate unloaded baseline; main experiments send no pings."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experimentLeo'))
from runExperimentLeo import run

if __name__=='__main__':
    run(['--ping-only',*sys.argv[1:]])
