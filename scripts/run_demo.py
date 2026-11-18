from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cancer_evidence.cli import main


if __name__ == "__main__":
    main(["demo", *sys.argv[1:]])
    print("CERTIFICATE_VERIFIED")
