"""Define workload terminal layouts and their endpoint route groups."""

import contextlib
from dataclasses import dataclass
import io
from pathlib import Path
import tempfile

import leo


PATH_COUNT = 5
DURATION = 600
DEFAULT_LAYOUT = "eastbound"
DEFAULT_OFFERED_MBPS = 640

# GeoNames city centers (latitude, longitude) from the approved terminal map.
# Keep the original order to preserve terminal IDs in existing catalogs.
LEGACY_TERMINALS = (
    ("Boston", 42.3584, -71.0598),
    ("New York", 40.7143, -74.006),
    ("Philadelphia", 39.9524, -75.1636),
    ("Ashburn", 39.0437, -77.4875),
    ("London", 51.5085, -0.1257),
    ("Dublin", 53.3331, -6.2489),
    ("Amsterdam", 52.374, 4.8897),
    ("Paris", 48.8534, 2.3488),
    ("Portland (Maine)", 43.6574, -70.2589),
    ("Providence", 41.824, -71.4128),
    ("Hartford", 41.7637, -72.6851),
    ("Albany", 42.6526, -73.7562),
    ("Syracuse", 43.0481, -76.1474),
    ("Newark", 40.7357, -74.1724),
    ("Harrisburg", 40.2737, -76.8844),
    ("Allentown", 40.6084, -75.4902),
    ("Wilmington (Delaware)", 39.746, -75.5466),
    ("Baltimore", 39.2904, -76.6122),
    ("Richmond", 37.5538, -77.4603),
    ("Norfolk", 36.8468, -76.2852),
    ("Bristol", 51.4552, -2.5966),
    ("Birmingham", 52.4814, -1.8998),
    ("Manchester", 53.4809, -2.2374),
    ("Belfast", 54.5968, -5.9254),
    ("Cork", 51.898, -8.4706),
    ("Galway", 53.2724, -9.051),
    ("Rotterdam", 51.9225, 4.4792),
    ("Brussels", 50.8505, 4.3488),
    ("Hamburg", 53.5507, 9.993),
    ("Lille", 50.6339, 3.0551),
    ("Rouen", 49.4431, 1.0993),
    ("Rennes", 48.1111, -1.6743),
)

# For each group of clients, list the regional, primary Atlantic, and
# secondary Atlantic servers in that order.
CLIENT_SERVER_GROUPS = (
    (("Portland (Maine)", "Providence", "Hartford"), ("Boston", "London", "Amsterdam")),
    (("Albany", "Syracuse", "Newark"), ("New York", "Dublin", "Paris")),
    (("Harrisburg", "Allentown", "Wilmington (Delaware)"), ("Philadelphia", "Paris", "London")),
    (("Baltimore", "Richmond", "Norfolk"), ("Ashburn", "Amsterdam", "Dublin")),
    (("Bristol", "Birmingham", "Manchester"), ("London", "New York", "Ashburn")),
    (("Belfast", "Cork", "Galway"), ("Dublin", "Boston", "Philadelphia")),
    (("Rotterdam", "Brussels", "Hamburg"), ("Amsterdam", "Ashburn", "Boston")),
    (("Lille", "Rouen", "Rennes"), ("Paris", "Philadelphia", "New York")),
)
LEGACY_TERMINAL_IDS = {name: index for index, (name, _, _) in enumerate(LEGACY_TERMINALS)}
# The original layouts share these numeric pairs, so they draw the same
# endpoint slots in the same order for each workload seed.
PAIRS = tuple(sorted(
    (LEGACY_TERMINAL_IDS[server], LEGACY_TERMINAL_IDS[client])
    for clients, servers in CLIENT_SERVER_GROUPS
    for client in clients
    for server in servers
))

SITES = {site[0]: site for site in LEGACY_TERMINALS}
EASTBOUND_TERMINALS = (
    *LEGACY_TERMINALS[:4],
    SITES["Providence"],
    SITES["Newark"],
    SITES["Wilmington (Delaware)"],
    SITES["Baltimore"],
    SITES["London"],
    ("Cardiff", 51.482619, -3.167598),
    ("Southampton", 50.900031, -1.399977),
    SITES["Dublin"],
    ("Glasgow", 55.862270, -4.239101),
    ("Liverpool", 53.405068, -2.961934),
    SITES["Amsterdam"],
    ("Antwerp", 51.222319, 4.413071),
    ("Leeds", 53.790017, -1.551348),
    SITES["Paris"],
    ("Caen", 49.183754, -0.349989),
    ("Nantes", 47.210386, -1.590017),
    *LEGACY_TERMINALS[20:],
)

# Append the additional senders after the existing receivers to preserve the
# original terminal IDs and the route-solving orientation of each added pair.
EASTBOUND76_SENDERS = (
    ("Portland (Maine)", ("Amsterdam",)),
    ("Hartford", ("Amsterdam",)),
    ("Syracuse", ("Paris",)),
    ("Harrisburg", ("London",)),
    ("Allentown", ("London", "Paris")),
    ("Richmond", ("Dublin", "Amsterdam")),
    ("Norfolk", ("Amsterdam",)),
)
EASTBOUND76_TERMINALS = EASTBOUND_TERMINALS + tuple(SITES[server] for server, _ in EASTBOUND76_SENDERS)
EASTBOUND76_TERMINAL_IDS = {name: index for index, (name, _, _) in enumerate(EASTBOUND76_TERMINALS)}
EASTBOUND76_REMOVED_PAIRS = {
    (EASTBOUND76_TERMINAL_IDS[server], EASTBOUND76_TERMINAL_IDS[client])
    for server, client in (
        ("Boston", "Southampton"),
        ("Boston", "Cork"),
        ("Philadelphia", "Antwerp"),
        ("Providence", "Leeds"),
        ("Wilmington (Delaware)", "Hamburg"),
    )
}
EASTBOUND76_ADDED_PAIRS = {
    (EASTBOUND76_TERMINAL_IDS[server], EASTBOUND76_TERMINAL_IDS[client])
    for server, clients in EASTBOUND76_SENDERS
    for client in clients
}
EASTBOUND76_PAIRS = tuple(sorted((set(PAIRS) - EASTBOUND76_REMOVED_PAIRS) | EASTBOUND76_ADDED_PAIRS))


@dataclass(frozen=True)
class Layout:
    name: str
    terminals: tuple[tuple[str, float, float], ...]
    snapshot_set: str
    pairs: tuple[tuple[int, int], ...]
    default_offered_mbps: float = DEFAULT_OFFERED_MBPS

    @property
    def route_pairs(self):
        # Match the configurator's canonical endpoint orientation and group ordering.
        return tuple(sorted((min(source, destination), max(source, destination))
                            for source, destination in self.pairs))

    def _directory(self, category):
        root = leo.SIM_DIR / category / "experimentWorkload"
        return root if self.name == "legacy" else root / self.name

    @property
    def store(self):
        return self._directory("experiments") / "results/routes"

    @property
    def results(self):
        return self.store.parent / "web-search"

    @property
    def logs(self):
        return self._directory("logs")

    @property
    def plots(self):
        return self._directory("plots")


LAYOUTS = {
    "legacy": Layout("legacy", LEGACY_TERMINALS, "LeoWorkload", PAIRS),
    "eastbound": Layout("eastbound", EASTBOUND_TERMINALS, "LeoWorkloadEastbound", PAIRS),
    "eastbound76": Layout("eastbound76", EASTBOUND76_TERMINALS, "LeoWorkloadEastbound76",
                          EASTBOUND76_PAIRS, default_offered_mbps=520),
}
LAYOUT_NAMES = tuple(LAYOUTS)


def get_layout(name=DEFAULT_LAYOUT):
    return LAYOUTS[name]


def routing_definitions(layout):
    generator = leo.shared_definitions()
    terminals = layout.terminals
    generator.TERMINAL_DEFINITIONS = terminals
    generator.PAIR_DEFINITIONS = tuple(
        (f"{terminals[source][0]} to {terminals[destination][0]}",
         terminals[source][0], terminals[destination][0], source, destination)
        for source, destination in layout.route_pairs
    )
    generator.K_PATH_SNAPSHOT_SET = layout.snapshot_set
    generator.PATH_COUNT = PATH_COUNT
    return generator


def write_topology(target: Path, duration: float, layout: Layout):
    generator = routing_definitions(layout)
    with tempfile.TemporaryDirectory(prefix="leo-workload-topology-") as temporary:
        generator.EXPERIMENT_DIR = Path(temporary)
        generator.INI_FILE = generator.EXPERIMENT_DIR / "routing.ini"
        generator.VISUALIZATION_INI_FILE = generator.EXPERIMENT_DIR / "visualize.ini"
        with contextlib.redirect_stdout(io.StringIO()):
            generator.generate_ini(duration)
        topology = (generator.EXPERIMENT_DIR / "topology.ini").read_text(encoding="utf-8")

    # The runner owns routing mode, applications, and recording. Retain the
    # shared topology and exact endpoint catalog for later traffic runs.
    excluded = {
        "loadFiles", "configLocation", "numApps", "kPathSnapshotMode",
        "kPathPingRouting", "allowRouteSnapshotOverwrite", "record-eventlog",
        "vector-recording", "scalar-recording", "bin-recording",
        "statistic-recording", "param-recording",
    }
    lines = [line for line in topology.splitlines()
             if line.partition("=")[0].strip().rsplit(".", 1)[-1] not in excluded]
    policy = io.StringIO()
    generator.write_policy_parameters(policy, generator.POLICIES["EdgeDisjoint"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines).rstrip() + "\n" + policy.getvalue(), encoding="utf-8")
