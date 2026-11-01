"""pbtk — PocketBook toolkit (host side).

Transports, all behind :class:`pbtk.device.Device`:
  * USB storage (T0): the reader mounted as a disk; deploy, logs, probe.
  * Agent (T1): an app linked with libpbagent, over Wi-Fi; live screen and input.
"""

__version__ = "0.1.0"


class PbtkError(Exception):
    """A user-facing failure with an actionable message."""
