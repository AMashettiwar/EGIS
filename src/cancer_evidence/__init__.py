"""Cancer Evidence Engine: deterministic, proof-carrying metadata queries."""

__version__ = "0.1.0"

from .engine import evaluate
from .snapshot import read_snapshot, write_snapshot

__all__ = ["evaluate", "read_snapshot", "write_snapshot"]
