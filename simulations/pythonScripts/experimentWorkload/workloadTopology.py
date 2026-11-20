"""Define the approved US–Europe workload endpoints and their routing topology."""

import contextlib
import io
from pathlib import Path
import tempfile

import leo


SNAPSHOT_SET = "LeoWorkload"
PATH_COUNT = 5
DURATION = 600

# GeoNames city centers (latitude, longitude) from the approved terminal map.
# Keep servers first and the order stable so the server-to-client groups keep
# their snapshot indices.
TERMINALS = (
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
TERMINAL_IDS = {name: index for index, (name, _, _) in enumerate(TERMINALS)}
PAIRS = tuple(sorted(
    (TERMINAL_IDS[server], TERMINAL_IDS[client])
    for clients, servers in CLIENT_SERVER_GROUPS
    for client in clients
    for server in servers
))


def routing_definitions():
    generator = leo.shared_definitions()
    generator.TERMINAL_DEFINITIONS = TERMINALS
    generator.PAIR_DEFINITIONS = tuple(
        (f"{TERMINALS[source][0]} to {TERMINALS[destination][0]}",
         TERMINALS[source][0], TERMINALS[destination][0], source, destination)
        for source, destination in PAIRS
    )
    generator.K_PATH_SNAPSHOT_SET = SNAPSHOT_SET
    generator.PATH_COUNT = PATH_COUNT
    return generator


def write_topology(target: Path, duration: float):
    generator = routing_definitions()
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
