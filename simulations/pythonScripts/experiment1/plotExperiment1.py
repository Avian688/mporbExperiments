from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runExperiments import run
if __name__ == "__main__":
    sys.argv[1:1] = ["--start-step", "4", "--skip-generate"]
    run(1)
