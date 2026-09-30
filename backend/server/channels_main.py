"""channels 运行时进程入口（与 api/worker 共镜像的第三个运行单元）。

持有「长连接/轮询」型入站传输（当前：Telegram 长轮询）。ARQ job 模型不适合
永久连接的生命周期，uvicorn 热重载会打断长连接，因此独立常驻进程。
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def main() -> None:
    from yuxi.channels.runtime import run_channels_runtime
    from yuxi.utils.logging_config import logger

    logger.info("[channels] runtime starting")
    asyncio.run(run_channels_runtime())


if __name__ == "__main__":
    main()
