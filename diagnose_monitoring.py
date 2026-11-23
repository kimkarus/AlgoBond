"""Diagnose native imports and optional database reads, never broker calls."""
import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description="Диагностика среды мониторинга без запуска торговли")
    p.add_argument("--project", type=Path, default=Path(__file__).resolve().parent)
    p.add_argument("--mode", choices=("monitoring", "trading"), default="monitoring")
    p.add_argument("--config", help="Имя INI-файла для --connect")
    p.add_argument("--connect", action="store_true", help="Дополнительно проверить базу выбранной конфигурации")
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    if args.connect and not args.config:
        p.error("Для --connect укажите имя профиля через --config.")
    root = args.project.resolve()
    log = args.output or root / "data/diagnostics" / ("monitoring_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
    lines = ["AlgoBond monitoring diagnostics", "Python: " + sys.version.replace("\n", " "), "Executable: " + sys.executable]
    checks = {
        "sqlite3": "import sqlite3",
        "http.client": "import http.client",
        "pg8000": "from algobond_monitor import postgresql_driver; postgresql_driver()",
    }
    for module, check in checks.items():
        code = "import sys; sys.path.insert(0, sys.argv[1]); " + check
        command = [sys.executable, "-I", "-B", "-X", "faulthandler", "-c", code, str(Path(__file__).resolve().parent)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=30, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            status = "OK" if result.returncode == 0 else f"FAIL 0x{result.returncode & 0xffffffff:08X}"
            lines.extend([module + ": " + status, result.stderr.decode("utf-8", "replace")])
        except subprocess.TimeoutExpired:
            status = "TIMEOUT"
            lines.append(module + ": TIMEOUT")
        print(module + ": " + status, flush=True)
    if args.connect:
        payload = dict(action="catalog", mode=args.mode, name=args.config)
        command = [sys.executable, "-I", "-B", "-X", "faulthandler", str(Path(__file__).with_name("algobond_monitor.py")), "--worker", "--project", str(root)]
        try:
            result = subprocess.run(command, input=json.dumps(payload).encode(), capture_output=True, timeout=40, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            if result.returncode:
                summary = f"Database worker: FAIL 0x{result.returncode & 0xffffffff:08X}"
            else:
                data = json.loads(result.stdout)
                summary = "Database worker: " + (data["error"] if "error" in data else "OK; accounts=" + str(len(data["accounts"])))
            lines.extend([summary, result.stderr.decode("utf-8", "replace")])
        except (subprocess.TimeoutExpired, ValueError):
            summary = "Database worker: TIMEOUT or invalid response"
            lines.append(summary)
        print(summary, flush=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(lines), encoding="utf-8")
    print("Report: " + str(log), flush=True)


if __name__ == "__main__":
    main()
