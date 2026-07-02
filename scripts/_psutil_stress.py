"""Stress psutil.process_iter(cmdline) -- the native call the guard makes."""
import faulthandler
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "data" / "logs" / "psutil_stress_trace.txt"
_f = open(TRACE, "w", encoding="utf-8", buffering=1)
faulthandler.enable(_f)

import psutil  # noqa: E402

_f.write(f"psutil {psutil.__version__} python {sys.version}\n")
for i in range(300):
    count = 0
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            _ = proc.info.get("cmdline")
            count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if i % 20 == 0:
        _f.write(f"iter {i}: scanned {count} procs\n")
    time.sleep(0.05)
_f.write("DONE no crash\n")
