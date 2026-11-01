"""Local executable selection. No device discovery, credential lookup or installs."""
import os
import shutil
from pathlib import Path


def node_executable():
    value = os.environ.get('DSH_NODE')
    if value:
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise RuntimeError('DSH_NODE must name an existing absolute local executable')
        return str(path)
    result = shutil.which('node')
    if not result:
        raise RuntimeError('Node.js is unavailable on PATH; configure DSH_NODE')
    return result
