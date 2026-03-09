"""Config loader for Chief of Staff."""

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.toml"


def load_config(path: Path | None = None) -> dict:
    """Load and return config from TOML file."""
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found: {path}. Copy config.example.toml to config.toml"
        )
    with open(path, "rb") as f:
        return tomllib.load(f)
