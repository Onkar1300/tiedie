"""Compatibility shim for pc-ble-driver-py.

Some older versions of pc-ble-driver-py import the Python 2 module name `Queue`.
On Python 3 the module is named `queue`.

Keeping this file in the application working directory allows `import Queue`
(from third-party code) to succeed.
"""

from queue import *  # noqa: F401,F403
