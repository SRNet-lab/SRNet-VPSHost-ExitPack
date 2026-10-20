# Copyright (c) 2026 SRNet contributors.
# SPDX-License-Identifier: Apache-2.0
# From SRNet-Desktop client-cli/srnet/rpc.py, so srnet-exits runs without the SRNet client.
"""OxenMQ DEALER client for session-router RPC (llarp.*)."""
from __future__ import annotations

import json
import time
from typing import Any


class Rpc:
    def __init__(self, endpoint: str = "tcp://127.0.0.1:1190", timeout_ms: int = 8000):
        self.endpoint = endpoint
        self.timeout_ms = timeout_ms
        import zmq  # imported here so non-router commands work without pyzmq
        self._zmq = zmq
        self._ctx = zmq.Context.instance()

    def call(self, method: str, payload: dict | None = None) -> Any:
        zmq = self._zmq
        sock = self._ctx.socket(zmq.DEALER)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        sock.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        # Session Router's OMQ expects a DEALER routing id; without one, replies time out
        # as zmq.Again ("Resource temporarily unavailable") on map_exit / status.
        sock.setsockopt(zmq.ROUTING_ID, f"srnet-{time.time_ns()}".encode())
        sock.connect(self.endpoint)
        try:
            tag = f"t{time.time_ns()}".encode()
            parts = [method.encode(), tag]
            if payload is not None:
                parts.append(json.dumps(payload).encode())
            sock.send_multipart(parts)
            reply = sock.recv_multipart()
        finally:
            sock.close(0)
        # REPLY, tag, body...
        if len(reply) < 3:
            raise RuntimeError(f"short RPC reply for {method}: {reply!r}")
        body = reply[2].decode()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"raw": body}

    def map_exit(self, address: str, *, full_tunnel: bool = True, ip_ranges: list[str] | None = None) -> Any:
        req: dict = {"address": address, "full_tunnel": full_tunnel}
        if ip_ranges is not None:
            req["ip_ranges"] = ip_ranges
        elif not full_tunnel:
            raise ValueError("ip_ranges required when full_tunnel is false")
        else:
            req["ip_ranges"] = []
        return self.call("llarp.map_exit", req)

    def unmap_exit(self, address: str) -> Any:
        return self.call("llarp.unmap_exit", {"address": address})

    def list_exits(self) -> Any:
        return self.call("llarp.list_exits")
