"""Compatibility shim: `python server.py` entry point for the vaults hub.

The implementation lives in the vaults/ package; this shim keeps existing
opencode configs and stdio spawning working unchanged.
"""

from vaults.server import main

if __name__ == "__main__":
    main()
