"""A typed Python client for the ENA Portal and Browser APIs."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("enaportal")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
