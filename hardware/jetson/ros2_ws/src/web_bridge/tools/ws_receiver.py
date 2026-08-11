import asyncio
import websockets


async def handler(ws, path):
    print("path:", path)
    print("auth:", ws.request_headers.get("Authorization"))
    async for message in ws:
        print(message)


async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        await asyncio.Future()

asyncio.run(main())
