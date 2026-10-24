"""Qualify the installed core runtime without scientific provider capabilities."""

from provider_core.container_guard import require_container

require_container()

from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory

for module in ("nmrnet_provider", "nmrnet_inference", "rdkit", "torch"):
    if find_spec(module) is not None:
        raise SystemExit(f"Cannot qualify delivery image: unexpected scientific module {module} is importable.")

# Startup must work beyond argparse: readiness imports core lazily. Construct
# and close only a private listener; no server loop, DNS or API request runs.
from tools.provider_proxy import ServicePolicy, ServiceProxyServer

with TemporaryDirectory() as temporary:
    root = Path(temporary)
    policy = ServicePolicy.admit("external", ["api.example:443"], [])
    with ServiceProxyServer(root / "proxy.sock", "lo", policy, status_directory=root):
        pass
