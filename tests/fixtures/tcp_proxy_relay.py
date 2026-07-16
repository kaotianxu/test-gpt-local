"""Small TCP relay used only by live Phase 5 delayed-proxy acceptance."""

from __future__ import annotations

import argparse
import asyncio


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def _serve(listen_port: int, upstream_port: int) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", upstream_port
            )
        except OSError:
            writer.close()
            return
        await asyncio.gather(
            _copy(reader, upstream_writer),
            _copy(upstream_reader, writer),
        )

    server = await asyncio.start_server(handle, "127.0.0.1", listen_port)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream-port", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(_serve(args.listen_port, args.upstream_port))


if __name__ == "__main__":
    main()
