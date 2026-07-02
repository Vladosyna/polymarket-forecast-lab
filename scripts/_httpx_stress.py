"""Minimal httpx async stress test against the public CLOB endpoint.

No lab code, no psutil, no scheduler -- isolates whether the native access
violation lives in the httpx/SSL stack itself on this machine.
"""
import asyncio
import faulthandler
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "data" / "logs" / "httpx_stress_trace.txt"
_f = open(TRACE, "w", encoding="utf-8", buffering=1)
faulthandler.enable(_f)

if sys.platform.startswith("win"):
    pol = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if pol:
        asyncio.set_event_loop_policy(pol())

import httpx  # noqa: E402

URL = "https://clob.polymarket.com/midpoint"
TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"


async def main() -> None:
    n = 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        while n < 400:
            try:
                r = await client.get(URL, params={"token_id": TOKEN})
                r.json()
            except Exception as exc:  # noqa: BLE001
                _f.write(f"req {n}: {type(exc).__name__}: {exc}\n")
            n += 1
            if n % 25 == 0:
                _f.write(f"ok {n} requests\n")
            await asyncio.sleep(0.2)
    _f.write("DONE all requests, no crash\n")


asyncio.run(main())
