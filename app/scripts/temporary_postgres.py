"""Owned, loopback-only PostgreSQL cluster with a random password and private files."""

import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.scripts.demo_stack import port


class TemporaryPostgres:
    def __init__(self, bin_dir=None):
        executable = shutil.which("initdb")
        self.bin_dir = Path(
            bin_dir
            or (
                str(Path(executable).parent)
                if executable
                else "/opt/homebrew/opt/postgresql@17/bin"
            )
        )
        self.bin_dir = (self.bin_dir / "initdb").resolve().parent
        self.temp = None
        self.running = False

    def command(self, name, *args):
        return subprocess.run(
            [str(self.bin_dir / name), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def __enter__(self):
        if not (self.bin_dir / "initdb").is_file():
            raise RuntimeError("PostgreSQL binaries unavailable; specify --pg-bin")
        self.temp = tempfile.TemporaryDirectory(prefix="ecom-isolated-pg-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        password = secrets.token_urlsafe(24)
        pwfile = self.root / "password"
        pwfile.write_text(password)
        pwfile.chmod(0o600)
        try:
            self.command(
                "initdb",
                "-D",
                str(self.data),
                "-U",
                "xc_bench",
                "--auth-host=scram-sha-256",
                "--auth-local=trust",
                "--pwfile=" + str(pwfile),
                "--encoding=UTF8",
                "--locale=C",
            )
            pwfile.unlink()
            self.port = port()
            self.options = (
                f"-h 127.0.0.1 -p {self.port} -k {self.root} -c max_connections=120"
            )
            self.start()
            self.database_url = f"postgresql+psycopg://xc_bench:{password}@127.0.0.1:{self.port}/postgres"
            self.version = self.command("postgres", "--version").stdout.strip()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def start(self):
        self.command(
            "pg_ctl",
            "-D",
            str(self.data),
            "-l",
            str(self.root / "postgres.log"),
            "-o",
            self.options,
            "-w",
            "start",
        )
        self.running = True

    def stop(self):
        if self.running:
            self.command("pg_ctl", "-D", str(self.data), "-m", "fast", "-w", "stop")
            self.running = False

    def __exit__(self, *args):
        self.stop()
        if self.temp:
            self.temp.cleanup()
