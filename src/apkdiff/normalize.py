from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

REQUIRED_PASSES: tuple[str, ...] = ("LocalDcePass", "RegAllocPass")


class RedexUnavailable(RuntimeError):
    """Raised when --normalize is requested but Redex is not on PATH."""


class RedexFailed(RuntimeError):
    """Raised when Redex ran but didn't apply the required passes."""


def is_available() -> bool:
    return shutil.which("redex") is not None


def normalize(apk_in: Path, *, out_dir: Path | None = None) -> Path:
    if not is_available():
        raise RedexUnavailable(
            "redex not found on PATH. Install it inside the dev shell "
            "(`nix develop`) before using --normalize."
        )
    apk_in = Path(apk_in)
    if out_dir is None:
        out_dir = Path(tempfile.mkdtemp(prefix="apkdiff-redex-"))
    else:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

    config_path = out_dir / "redex-config.json"
    config_path.write_text(json.dumps({"redex": {"passes": list(REQUIRED_PASSES)}}))

    out_apk = out_dir / (apk_in.stem + ".normalized.apk")
    cmd = ["redex", "-c", str(config_path), "-o", str(out_apk), str(apk_in)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RedexFailed(
            f"redex exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
    _assert_passes_applied(proc.stdout + "\n" + proc.stderr)
    if not out_apk.exists():
        raise RedexFailed(f"redex produced no output at {out_apk}")
    return out_apk


def _assert_passes_applied(log: str) -> None:
    missing = [p for p in REQUIRED_PASSES if p not in log]
    if missing:
        raise RedexFailed(
            f"redex did not apply required passes: {missing}. "
            f"Both LocalDcePass and RegAllocPass are mandatory."
        )
