#!/usr/bin/env python3
import argparse
import leo

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generate ExperimentLeo and its separate unloaded ping prepass')
    parser.add_argument('--sim-time',type=float,default=leo.DURATION)
    parser.add_argument('--route-store',type=leo.Path,default=leo.DEFAULT_STORE)
    args = parser.parse_args()
    matrix = leo.generate(args.sim_time,args.route_store)
    print(f'Generated {len(matrix)} traffic runs and one unloaded ping run')
