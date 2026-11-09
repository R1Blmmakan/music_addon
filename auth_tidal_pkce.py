"""Convenience wrapper for app/auth_tidal.py."""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.auth_tidal import main

if __name__ == "__main__":
    main()
