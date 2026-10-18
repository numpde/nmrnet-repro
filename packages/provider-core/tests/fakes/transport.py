"""Controlled HTTP streams for failure/retirement cases a finite peer cannot force."""

import asyncio

import httpx


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, failure=None, close_failure=None, stall=None):
        self.chunks = chunks
        self.failure = failure
        self.close_failure = close_failure
        self.stall = stall
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.stall is not None:
            await self.stall.wait()
        if self.failure is not None:
            raise self.failure

    async def aclose(self):
        if self.close_failure is not None:
            raise self.close_failure
        self.closed = True


class Client:
    def __init__(self, stream, *, close_failure=None, send_gate=None):
        self.stream = stream
        self.close_failure = close_failure
        self.send_gate = send_gate
        self.sent = asyncio.Event()
        self.closed = False
        self.calls = []

    async def send(self, request, **options):
        self.calls.append((request, options))
        self.sent.set()
        if self.send_gate is not None:
            await self.send_gate.wait()
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=self.stream, request=request)

    async def aclose(self):
        if self.close_failure is not None:
            raise self.close_failure
        self.closed = True
