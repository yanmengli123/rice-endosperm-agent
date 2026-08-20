from dotenv import load_dotenv

load_dotenv(".env", override=True)

from concurrent.futures import ThreadPoolExecutor  # noqa: E402

try:
    from importlib.metadata import version

    __version__ = version("yuxi")
except Exception:
    __version__ = "unknown"

executor = ThreadPoolExecutor()  # noqa: E402


def __getattr__(name: str):
    if name == "config":
        from yuxi.config import config as loaded_config

        globals()[name] = loaded_config
        return loaded_config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_version():
    """Return the Yuxi version."""
    return __version__
