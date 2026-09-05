from dotenv import load_dotenv

load_dotenv(".env", override=True)

from concurrent.futures import ThreadPoolExecutor  # noqa: E402

try:
    from importlib.metadata import version

    __version__ = version("yuxi")
except Exception:
    __version__ = "unknown"

executor = ThreadPoolExecutor()  # noqa: E402

# 显式绑定 config 实例。子模块 yuxi.config 首次被导入时，导入机制会把「模块对象」
# setattr 到 yuxi 包上；若 config 依赖惰性 __getattr__ 缓存，包属性会在「实例/模块」
# 之间随导入顺序漂移，导致 `from yuxi import config` 偶发拿到模块对象而在运行期
# AttributeError（kb_utils 等顶层引用会直接炸）。此处 from-import 的名字绑定发生在
# 父 setattr 之后，使包属性永久稳定为 Config 实例；yuxi.config 已进入 sys.modules，
# 后续子模块导入不会再次触发父 setattr。
from yuxi.config import config as config  # noqa: E402


def __getattr__(name: str):
    if name == "config":
        from yuxi.config import config as loaded_config

        globals()[name] = loaded_config
        return loaded_config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_version():
    """Return the Yuxi version."""
    return __version__
