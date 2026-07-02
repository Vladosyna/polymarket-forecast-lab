"""Single-instance guard for the lab's own long-running processes.

A standard PID-registry / single-instance pattern: each managed process
(`orchestrator` = ``lab run``, `collector` = ``lab collect``, `dashboard` =
the Streamlit app) records itself at startup together with a content hash of
the code it loaded. The guard can then detect two conditions among *our own*
instances and stand down the older one:

* redundant   -- more than one instance of a role, or a standalone collector
                 while an orchestrator (which collects in-process) is running;
* outdated    -- an instance still executing an older code version than what is
                 on disk (Python loads modules once, so a long-lived process
                 keeps stale code until restarted).

Only processes whose command line clearly belongs to this application are ever
considered; unrelated programs are never touched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from lab.util import PROJECT_ROOT, now_utc_iso

log = logging.getLogger(__name__)

ROLES = ("orchestrator", "collector", "dashboard")
_REGISTRY_NAME = "processes.json"


# --- code version --------------------------------------------------------

def code_version() -> str:
    """Deterministic short hash of the source that defines runtime behavior.

    Content-based (not mtime) so it is stable across machines and reproducible
    in tests: two checkouts with identical bytes yield the same version.
    """
    h = hashlib.sha1()
    src = PROJECT_ROOT / "src" / "lab"
    files = sorted(src.rglob("*.py"), key=lambda p: p.relative_to(PROJECT_ROOT).as_posix())
    cfg = PROJECT_ROOT / "config.yaml"
    if cfg.exists():
        files.append(cfg)
    for f in files:
        rel = f.relative_to(PROJECT_ROOT).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(f.read_bytes())
        except OSError:
            continue
        h.update(b"\0")
    return h.hexdigest()[:12]


# --- registry ------------------------------------------------------------

def _runtime_dir(config: dict[str, Any]) -> Path:
    d = Path(config["storage"]["db_path"])
    d = (d if d.is_absolute() else PROJECT_ROOT / d).parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def registry_path(config: dict[str, Any]) -> Path:
    return _runtime_dir(config) / _REGISTRY_NAME


def _load_registry(config: dict[str, Any]) -> list[dict[str, Any]]:
    p = registry_path(config)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return [e for e in entries if isinstance(e, dict) and "pid" in e]


def _write_registry(config: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    p = registry_path(config)
    try:
        p.write_text(json.dumps({"entries": entries}, indent=2), encoding="utf-8")
    except OSError:
        log.warning("could not write process registry")


def _self_start_ts() -> float:
    try:
        import psutil

        return float(psutil.Process(os.getpid()).create_time())
    except Exception:
        return time.time()


def register_self(config: dict[str, Any], role: str) -> None:
    """Record the current process as the live instance of `role`."""
    pid = os.getpid()
    entries = [e for e in _load_registry(config)
               if e.get("pid") != pid and _pid_alive(e.get("pid"))]
    entries.append({
        "role": role,
        "pid": pid,
        "code_version": code_version(),
        "start_ts": _self_start_ts(),
        "registered_at": now_utc_iso(),
    })
    _write_registry(config, entries)


# --- process inspection --------------------------------------------------

def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        return False


def classify_cmdline(cmdline: list[str]) -> str | None:
    """Map a command line to one of our roles, or None if it is not ours."""
    tokens = [t.lower() for t in cmdline]
    joined = " ".join(tokens)
    if "streamlit" in joined and "dashboard" in joined:
        return "dashboard"
    has_lab = any(("lab" == t or t.endswith("lab.exe") or t.endswith("\\lab")
                   or "lab" in t.split(os.sep)[-1]) for t in tokens) or " lab" in f" {joined}"
    if not has_lab:
        return None
    # collector check precedes orchestrator so "uv run lab collect" is a collector
    if "collect" in tokens:
        return "collector"
    if "run" in tokens:
        return "orchestrator"
    return None


def _alive_registry_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Registered instances whose PID is still alive, ordered start_ts refreshed."""
    out: list[dict[str, Any]] = []
    try:
        import psutil
    except Exception:
        psutil = None  # type: ignore
    for e in _load_registry(config):
        pid = e.get("pid")
        if not _pid_alive(pid):
            continue
        start_ts = e.get("start_ts")
        if psutil is not None:
            try:
                start_ts = float(psutil.Process(pid).create_time())
            except Exception:
                pass
        out.append({**e, "start_ts": start_ts})
    return out


def find_unmanaged(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Live lab-looking processes that never registered (e.g. legacy instances)."""
    known = {e.get("pid") for e in _load_registry(config)}
    known.add(os.getpid())
    found: list[dict[str, Any]] = []
    try:
        import psutil
    except Exception:
        return found
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            info = proc.info
            cmdline = info.get("cmdline") or []
            role = classify_cmdline(cmdline)
            if role is None or info["pid"] in known:
                continue
            found.append({"role": role, "pid": info["pid"],
                          "start_ts": float(info.get("create_time") or 0.0),
                          "cmdline": " ".join(cmdline), "code_version": None})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


# --- decision (pure) -----------------------------------------------------

def stale_or_redundant(entries: list[dict[str, Any]], self_pid: int,
                       current_version: str) -> list[int]:
    """PIDs that should stand down, given the live registered instances.

    Rules: outdated code version, per-role duplicates (keep the newest by
    start_ts, preferring a current-version instance or self), and collectors
    made redundant by a live orchestrator. `self_pid` is never returned.
    """
    to_stop: set[int] = set()

    for e in entries:
        pid = e.get("pid")
        if pid == self_pid:
            continue
        if e.get("code_version") != current_version:
            to_stop.add(pid)

    orchestrator_alive = any(e.get("role") == "orchestrator" for e in entries)

    by_role: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        by_role.setdefault(e.get("role"), []).append(e)

    for role, group in by_role.items():
        if role == "collector" and orchestrator_alive:
            for e in group:
                if e.get("pid") != self_pid:
                    to_stop.add(e.get("pid"))
            continue
        if len(group) > 1:
            survivors = sorted(group, key=lambda e: e.get("start_ts") or 0.0, reverse=True)
            kept = None
            for e in survivors:
                if e.get("pid") == self_pid or e.get("code_version") == current_version:
                    kept = e
                    break
            if kept is None:
                kept = survivors[0]
            for e in group:
                if e.get("pid") not in (kept.get("pid"), self_pid):
                    to_stop.add(e.get("pid"))

    to_stop.discard(self_pid)
    return sorted(p for p in to_stop if isinstance(p, int))


# --- enforcement ---------------------------------------------------------

def _terminate(pid: int, timeout: float = 5.0) -> bool:
    try:
        import psutil

        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            proc.kill()
        return True
    except Exception as exc:  # already gone, denied, etc.
        log.warning("could not stop instance", extra={"ctx": {"pid": pid, "error": str(exc)}})
        return False


def enforce(config: dict[str, Any], role: str, act: bool = True) -> dict[str, Any]:
    """Register self and stand down any outdated/redundant sibling instances."""
    current = code_version()
    register_self(config, role)
    entries = _alive_registry_entries(config)
    candidates = stale_or_redundant(entries, os.getpid(), current)
    stopped: list[int] = []
    if act:
        for pid in candidates:
            if _terminate(pid):
                stopped.append(pid)
        if stopped:
            survivors = [e for e in _load_registry(config) if e.get("pid") not in stopped]
            _write_registry(config, survivors)
    result = {"current_version": current, "role": role,
              "candidates": candidates, "stopped": stopped}
    if stopped:
        log.warning("instance guard stopped outdated/redundant instances", extra={"ctx": result})
    else:
        log.info("instance guard: nothing to stop", extra={"ctx": result})
    return result


def report(config: dict[str, Any]) -> dict[str, Any]:
    """Read-only snapshot for `lab ps`: managed + unmanaged instances and flags."""
    current = code_version()
    managed = _alive_registry_entries(config)
    flagged = set(stale_or_redundant(managed, self_pid=-1, current_version=current))
    unmanaged = find_unmanaged(config)
    return {"current_version": current, "managed": managed,
            "flagged_pids": flagged, "unmanaged": unmanaged}
