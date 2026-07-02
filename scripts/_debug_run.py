import asyncio
import faulthandler
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "data" / "logs" / "debug_run_trace.txt"
TRACE.write_text("", encoding="utf-8")
_f = open(TRACE, "a", encoding="utf-8", buffering=1)

faulthandler.enable(_f)
faulthandler.dump_traceback_later(3, repeat=True, file=_f)

from lab.util import load_config, setup_logging, use_stable_event_loop  # noqa: E402
from lab.collect.runner import run_orchestrator  # noqa: E402

use_stable_event_loop()
_cfg = load_config()
setup_logging(_cfg)
_f.write("debug: entering run_orchestrator\n")
try:
    asyncio.run(run_orchestrator(_cfg))
except BaseException as exc:  # noqa: BLE001
    _f.write(f"debug: exception {type(exc).__name__}: {exc}\n")
    traceback.print_exc(file=_f)
    raise
finally:
    _f.write("debug: run_orchestrator returned/exited\n")
    _f.flush()
