# ExperimentLeo route viewer

This is an animated, display-only view of ExperimentLeo's existing saved
EdgeDisjoint routes. It creates satellite-orbit and geographic display modules,
loads the saved path catalog, and draws the requested routes. There are no
TCP applications, transport controllers, packets, routing recalculations or
experiment-matrix runs. It records no vectors or scalars.

Everything added for the viewer lives in `mporbExperiments`. LeoSatellites and
OS3 supply their existing orbit modules and snapshot-reading library. Their
source, INIs and saved routes are never modified. The globe/scene setup is
adapted from the existing LeoSatellites viewer because its internals are private.

## Build once

With the OMNeT++ environment sourced, from the `mporbExperiments` project root:

```sh
make makefiles && make MODE=release -j8
```

Use GNU Make 4 (`gmake` on macOS) if the system `make` is older. This builds
only this project; INET, OS3, LeoSatellites and the existing transport libraries
must already be built. The viewer requires an OSG-enabled OMNeT++/Qtenv build
and the igraph development headers already required by LeoSatellites.

## Run from `simulations/pythonScripts`

```sh
# Default: animate all five ExperimentLeo pairs with K=5 for the full duration.
python3 experimentLeo/viewExperimentLeo.py

# One connection, or a selected subset, with another K.
python3 experimentLeo/viewExperimentLeo.py --pairs NewarkToBristol --k 3
python3 experimentLeo/viewExperimentLeo.py --pairs 1 3 5 --k 5

# Show names and the experiment's pair numbers (1–5).
python3 experimentLeo/viewExperimentLeo.py --list-pairs

# Optional frozen view, using the latest saved snapshot at/before 30 seconds.
python3 experimentLeo/viewExperimentLeo.py --pairs all --k 5 --time 30

# Hide expected access-link overlap; retain highlighting on shared core links.
python3 experimentLeo/viewExperimentLeo.py --k 5 --core-only-overlap

# Include the background ISL mesh, or relocate the route store.
python3 experimentLeo/viewExperimentLeo.py --show-isls
python3 experimentLeo/viewExperimentLeo.py --route-store /path/to/leoSaves

# Inspect the command without writing files, building, or launching Qtenv.
python3 experimentLeo/viewExperimentLeo.py --dry-run
```

The default opens Qtenv with a replay timer; use Run/Pause and the normal
animation-speed controls. Replay duration comes from the existing ExperimentLeo
manifest, defaulting to 300 seconds. `--sim-time` overrides that duration, subject
to saved-route coverage. Display frames update every second, as in the existing
viewer, and use the last saved snapshot at or before that frame. Satellite
positions are evaluated at the snapshot time using the same constellation epoch.
`--time` freezes the initial view at a selected saved time and schedules no replay
timer. It is optional; normal launches animate the whole duration.

The five experiment pairs map to catalog groups 5–9. K can be 1–10, including
all experiment values 1–5. The saved catalog identity always remains **K=10**;
the viewer draws only its first K ranks. No new saves are needed. Fewer available
paths are shown as, for example, `3/5 paths`, without duplicating paths or reducing
the requested K. Per-rank RTT/unavailable labels are included for a single pair.

## Overlap styling

Each selected path contributes once to each physical, undirected link it uses.
The viewer draws each link once, preventing arbitrary path draw order from
hiding its overlap count:

- One path: the connection's colour, 2.5-pixel line.
- Two subflows on the same link: yellow, thicker.
- Three subflows: orange.
- Four or more: red, progressively darker and thicker as the count increases.
- Width increases are capped to keep heavily shared links readable.

Only the shared link segment changes colour and thickness. Separate sections
before and after it keep their connection colours. This counts shared **links**,
not merely common satellites or lines crossing
in the projected view. Counts combine subflows from all selected pairs and both
directions of the same physical link. They show route overlap, not measured
utilisation or congestion. Shared terminal access hops are included by default;
at K=5 they can therefore be red even when the core paths are edge-disjoint.
`--core-only-overlap` leaves those access hops in their pair colour and applies
the overlap styling only inside the satellite/ground-station core.

The launcher generates one ignored local `visualize.ini` here, overwriting it
on each launch. It reuses the shared geometry generator with its output paths
redirected to this project. `--skip-generate` reuses that local viewer INI.
The existing ExperimentLeo simulation inputs/results and extracted CSVs are
not touched.
