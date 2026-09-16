"""MAIBuilderをWindowsサービスとして起動するpywin32ラッパー。

管理者PowerShell:
  python builder/windows_service.py --startup auto install
  python builder/windows_service.py start
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError:  # Windows以外でも構文検査とドキュメント生成を可能にする
    servicemanager = win32event = win32service = win32serviceutil = None


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "deploy" / "windows" / "shared.config.json"


def load_config(path: Path | None = None) -> dict:
    p = Path(path or os.environ.get("BUILDER_SERVICE_CONFIG", DEFAULT_CONFIG))
    if not p.is_file():
        raise FileNotFoundError(
            f"{p} がありません。shared.config.json.example をコピーして設定してください")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    required = ("python_exe", "workdir", "public_url", "admin_password")
    missing = [key for key in required if not str(cfg.get(key) or "").strip()]
    if missing:
        raise ValueError("サービス設定が不足しています: " + ", ".join(missing))
    if len(str(cfg["admin_password"])) < 12:
        raise ValueError("admin_password は12文字以上にしてください")
    if not str(cfg["public_url"]).startswith("https://"):
        raise ValueError("public_url は https:// から始めてください")
    return cfg


def command_and_env(cfg: dict) -> tuple[list[str], dict[str, str], Path]:
    workdir = Path(cfg["workdir"]).resolve()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["BUILDER_ADMIN_PASSWORD"] = str(cfg["admin_password"])
    env["BUILDER_PUBLIC_URL"] = str(cfg["public_url"])
    if cfg.get("auth_db"):
        env["BUILDER_AUTH_DB"] = str(cfg["auth_db"])
    cmd = [str(cfg["python_exe"]), "-m", "builder.api", "--shared",
           "--public-url", str(cfg["public_url"]),
           "--host", "127.0.0.1", "--port", str(int(cfg.get("port", 8780)))]
    if cfg.get("build", True):
        cmd.append("--build")
    if cfg.get("no_backtest", False):
        cmd.append("--no-backtest")
    if cfg.get("date"):
        cmd += ["--date", str(cfg["date"])]
    return cmd, env, workdir


if win32serviceutil:
    class MAIBuilderService(win32serviceutil.ServiceFramework):
        _svc_name_ = "MAIBuilder"
        _svc_display_name_ = "MAIBuilder Shared Service"
        _svc_description_ = "招待QRで共有するMAIBuilder APIと画面を常時稼働します。"

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.process: subprocess.Popen | None = None

        def SvcStop(self):  # noqa: N802
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)
            if self.process and self.process.poll() is None:
                self.process.terminate()

        def SvcDoRun(self):  # noqa: N802
            servicemanager.LogInfoMsg("MAIBuilder service starting")
            try:
                self._run()
            except Exception as err:
                servicemanager.LogErrorMsg(f"MAIBuilder service failed: {err}")
                raise

        def _run(self):
            cfg = load_config()
            cmd, env, workdir = command_and_env(cfg)
            log_dir = workdir / "out" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "service.log").open("a", encoding="utf-8") as log:
                self.process = subprocess.Popen(
                    cmd, cwd=workdir, env=env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                while self.process.poll() is None:
                    if win32event.WaitForSingleObject(self.stop_event, 1000) == win32event.WAIT_OBJECT_0:
                        self.process.terminate()
                        break
                try:
                    self.process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                code = self.process.returncode
            if code not in (0, None) and win32event.WaitForSingleObject(self.stop_event, 0) != win32event.WAIT_OBJECT_0:
                raise RuntimeError(f"builder.api exited with {code}")


def main() -> int:
    if "--check-config" in sys.argv[1:]:
        cfg = load_config()
        command_and_env(cfg)
        print("shared.config.json is valid")
        return 0
    if "--run-config" in sys.argv[1:]:
        cfg = load_config()
        cmd, env, workdir = command_and_env(cfg)
        return subprocess.call(cmd, cwd=workdir, env=env)
    if not win32serviceutil:
        print("pywin32が必要です: pip install -r requirements.txt", file=sys.stderr)
        return 2
    # インストール前にも設定不備を検出し、起動不能なサービスを登録しない。
    if any(x in sys.argv[1:] for x in ("install", "update", "start", "restart", "debug")):
        load_config()
    win32serviceutil.HandleCommandLine(MAIBuilderService)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
