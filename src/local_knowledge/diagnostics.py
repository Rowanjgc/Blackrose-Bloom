"""Private local tracebacks, without environment dumps or frame locals."""
from datetime import datetime, UTC
import os
from pathlib import Path
import sys
import traceback


def record_exception(operation, error):
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    directory = root / "local-knowledge/diagnostics"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "initialization.log"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(f"\n{datetime.now(UTC).isoformat()} {operation}\n")
            traceback.print_exception(error, file=output)
    except OSError:
        # Diagnostics must not replace the actual initialization exception.
        print("knowledge: unable to write private initialization diagnostics", file=sys.stderr)
