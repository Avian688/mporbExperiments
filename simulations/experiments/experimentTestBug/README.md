# SF3 loss and connection-level recovery

One backlogged MPORB connection sends on three 100 Mbps paths with 50 ms
propagation RTT. A second MPORB connection with one subflow starts near 5 s
and shares only SF3's bottleneck. From 5 to 5.25 s the first sender's forward
access link on SF3 drops all packets. The other two paths, reverse channels,
and the second connection stay available. The first connection uses the
LEO run's 4 MiB send limit and `defaultCwnd` scheduler.

From this directory, with the OMNeT++ environment sourced, run the two matched
9 s simulations with the existing libraries.

```sh
opp_run -u Cmdenv -c Omega5 && opp_run -u Cmdenv -c Omega10
```

Run the competition-only controls to see what happens when the competing
connection joins with the SF3 link still working.

```sh
opp_run -u Cmdenv -c Omega5NoLoss && opp_run -u Cmdenv -c Omega10NoLoss
```

Open `ExperimentTestBug.anf` in the OMNeT++ IDE to browse the runs. Vectors
start at 4.75 s. Rerunning a configuration overwrites its results.

- Compare sender `cwnd` and `mbytesInFlight` on the two unaffected subflows
  with receiver `metaExpectedDsn`, `holBlockedBytes`, and app `goodput`.
  The expected DSN is the value the receiver uses for `DATA_ACK`.
- Check the sender's `metaReinjectedBytes` for when data-level recovery starts.
- On SF3, compare `cwnd` with `mpOrbDeltaAppliedShuffle`, `U`, `numRtos`, and
  `recoveryPoint`. To convert the applied correction from bit/s to window
  bytes, multiply by the corresponding `srtt` and divide by eight.
