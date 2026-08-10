import os
from src.config.models import Config


def load() -> Config:
    """Load config from environment variables."""
    return Config.from_env()
