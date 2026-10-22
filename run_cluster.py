"""
Cluster Orchestrator for Dynamic Load Balancer.

Spawns:
- 4 Backend Nodes on ports 8001, 8002, 8003, and 8004
- Load Balancer on port 8080

Streams all logs cleanly with colored process tags, and handles graceful shutdown
on Ctrl+C.
"""

from __future__ import annotations
import os
import signal
import subprocess
import sys
import threading
import time
from typing import List, Tuple

# ANSI color codes for terminal logging
COLORS = {
    "node-1": "\033[94m",       # Bright Blue
    "node-2": "\033[96m",       # Cyan
    "node-3": "\033[92m",       # Bright Green
    "node-4": "\033[93m",       # Yellow
    "lb":     "\033[95m",       # Magenta
    "system": "\033[97;1m",     # Bold White
    "reset":  "\033[0m",
}

BACKEND_CONFIGS = [
    ("node-1", 8001, 3),
    ("node-2", 8002, 2),
    ("node-3", 8003, 1),
    ("node-4", 8004, 1),
]


def stream_output(prefix: str, color: str, pipe) -> None:
    """Reads lines from a subprocess stdout/stderr and prints with prefix."""
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            clean_line = line.rstrip()
            if clean_line:
                print(f"{color}[{prefix}]{COLORS['reset']} {clean_line}", flush=True)
    except Exception:
        pass


def main() -> None:
    # Enable Windows virtual terminal processing for ANSI colors
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

    print(f"{COLORS['system']}============================================================{COLORS['reset']}")
    print(f"{COLORS['system']}    Dynamic Layer-7 Load Balancer Cluster Orchestrator     {COLORS['reset']}")
    print(f"{COLORS['system']}============================================================{COLORS['reset']}")
    print(f"[*] Reverse Proxy Host:    http://127.0.0.1:8080/")
    print(f"[*] Telemetry Dashboard:  \033[92;1mhttp://localhost:8080/dashboard\033[0m")
    print(f"[*] Backend Cluster:       8001, 8002, 8003, 8004")
    print(f"[*] Press \033[91;1mCtrl+C\033[0m at any time to safely shut down cluster.\n")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    processes: List[Tuple[str, subprocess.Popen]] = []

    try:
        # 1. Spawn 4 Backend Nodes
        for node_id, port, default_weight in BACKEND_CONFIGS:
            cmd = [
                sys.executable,
                "backend_node.py",
                "--id", node_id,
                "--port", str(port),
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            processes.append((node_id, proc))
            color = COLORS.get(node_id, COLORS["system"])
            thread = threading.Thread(
                target=stream_output,
                args=(f"{node_id}:{port}", color, proc.stdout),
                daemon=True
            )
            thread.start()
            time.sleep(0.15)

        # 2. Spawn Load Balancer
        lb_cmd = [sys.executable, "load_balancer.py"]
        lb_proc = subprocess.Popen(
            lb_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        processes.append(("load-balancer", lb_proc))
        lb_thread = threading.Thread(
            target=stream_output,
            args=("LoadBalancer:8080", COLORS["lb"], lb_proc.stdout),
            daemon=True
        )
        lb_thread.start()

        # Monitor loop
        while True:
            time.sleep(0.5)
            # Check if any process died unexpectedly
            for name, proc in processes:
                ret = proc.poll()
                if ret is not None:
                    print(f"\n{COLORS['system']}[!] Process {name} exited with code {ret}.{COLORS['reset']}")
                    return

    except KeyboardInterrupt:
        print(f"\n\n{COLORS['system']}[*] Caught Ctrl+C. Initiating graceful shutdown...{COLORS['reset']}")
    finally:
        for name, proc in processes:
            try:
                proc.terminate()
            except Exception:
                pass
        
        # Give processes a moment to terminate cleanly
        time.sleep(0.5)
        for name, proc in processes:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

        print(f"{COLORS['system']}[*] All cluster processes safely stopped. Goodbye!{COLORS['reset']}")


if __name__ == "__main__":
    main()
