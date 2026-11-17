"""Production launcher: one process, two uvicorn.Server instances.

Ingress on :8099 always; the link app on :8100 only when external_links is
configured correctly. A bind/runtime failure of the link server is isolated so
8099 keeps serving.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import uvicorn

from .config import load_config_from_file
from .main import create_apps

LOG = logging.getLogger("hikvision_recordings.serve")
INGRESS_PORT = 8099
LINK_PORT = 8100
LEVELS = (
    "critical", "error", "warning", "info", "debug", "trace",
)


def _log_level(name: str) -> str:
    value = (name or "info").lower()
    if value == "fatal":
        return "critical"
    return value if value in LEVELS else "info"


def _link_log_level(user_level: str) -> str:
    """Link server never runs below info — uvicorn trace dumps request paths."""
    order = {name: index for index, name in enumerate(LEVELS)}
    # Lower index = more severe. "Never trace or debug" => at least info.
    return user_level if order[user_level] <= order["info"] else "info"


async def _run() -> None:
    user_level = _log_level(os.environ.get("LOG_LEVEL", "info"))
    logging.basicConfig(
        level=getattr(logging, user_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = load_config_from_file()
    ingress_app, link_app = create_apps(config)

    ingress = uvicorn.Server(
        uvicorn.Config(
            ingress_app,
            host="0.0.0.0",
            port=INGRESS_PORT,
            log_level=user_level,
            access_log=False,
            server_header=False,
        )
    )

    link_server: uvicorn.Server | None = None
    link_task: asyncio.Task | None = None

    if link_app is not None:
        link_server = uvicorn.Server(
            uvicorn.Config(
                link_app,
                host="0.0.0.0",
                port=LINK_PORT,
                log_level=_link_log_level(user_level),
                access_log=False,
                server_header=False,
                lifespan="off",
            )
        )

        async def _link_serve() -> None:
            try:
                await link_server.serve()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — isolate from 8099
                LOG.error("link server failed (ingress keeps running): %s", exc)

        async def _stop_link() -> None:
            if link_server is None:
                return
            link_server.should_exit = True
            if link_task is not None and not link_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(link_task), timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    link_task.cancel()
                    try:
                        await link_task
                    except asyncio.CancelledError:
                        pass

        ingress_app.state.stop_link_server = _stop_link

    LOG.info("Starting DVR Recordings on :%s (ingress) …", INGRESS_PORT)
    # 8099 starts first; then the link listener (if any).
    ingress_task = asyncio.create_task(ingress.serve(), name="ingress-serve")
    if link_server is not None:
        LOG.info("Starting external link listener on :%s …", LINK_PORT)
        link_task = asyncio.create_task(_link_serve(), name="link-serve")

    await ingress_task
    if link_task is not None and not link_task.done():
        if link_server is not None:
            link_server.should_exit = True
        await link_task


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
