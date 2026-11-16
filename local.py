import argparse
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import tarfile
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import urlopen
from zipfile import ZipFile

import msvcrt

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / ".local"
ENV_FILE = ROOT / ".env"
PG_RUNTIME = LOCAL / "pgsql"
PG_CTL = PG_RUNTIME / "bin" / "pg_ctl.exe"
PG_DATA = LOCAL / "pgdata"
PG_SETUP = LOCAL / "pg-setup-password"
PG_URL = (
    "https://repo.maven.apache.org/maven2/io/zonky/test/postgres/"
    "embedded-postgres-binaries-windows-amd64/17.10.0/"
    "embedded-postgres-binaries-windows-amd64-17.10.0.jar"
)
PG_SHA256 = "f418c95daa6473f05d5129803fdb674d49fb472ee5c94039d408c941f50a9721"


def manage(*arguments):
    subprocess.run([sys.executable, "manage.py", *arguments], cwd=ROOT, check=True)


@contextmanager
def exclusive_start():
    with (LOCAL / "start.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("This project is already starting or running.") from error
        yield


def install_dependencies():
    requirements = ROOT / "requirements.txt"
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = LOCAL / "requirements.sha256"
    available = subprocess.run(
        [sys.executable, "-c", "import django, psycopg, httpx, bs4, dotenv"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0
    if not available or not marker.exists() or marker.read_text().strip() != fingerprint:
        print("Installing Python dependencies...", flush=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
            check=True,
        )
        marker.write_text(fingerprint, encoding="ascii")


def available_port(first):
    for port in range(first, first + 100):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free localhost port starting at {first}.")


def install_postgres():
    if PG_CTL.is_file():
        return
    print("Downloading PostgreSQL 17.10 for Windows x64...", flush=True)
    with TemporaryDirectory(dir=LOCAL) as folder:
        stage = Path(folder)
        archive = stage / "postgres.jar"
        subprocess.run(
            ["curl.exe", "-fLsS", "--retry", "2", "--max-time", "120",
             PG_URL, "-o", str(archive)],
            check=True,
        )
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != PG_SHA256:
                raise RuntimeError("PostgreSQL download checksum mismatch.")
        with ZipFile(archive) as bundle:
            packed = bundle.extract("postgres-windows-x86_64.txz", stage)
        runtime = stage / "pgsql"
        with tarfile.open(packed, "r:xz") as binaries:
            binaries.extractall(runtime, filter="data")
        runtime.rename(PG_RUNTIME)


def pg_running():
    return PG_CTL.is_file() and subprocess.run(
        [str(PG_CTL), "-D", str(PG_DATA), "status"], stdout=subprocess.DEVNULL,
    ).returncode == 0


def write_environment():
    if ENV_FILE.exists():
        return
    entries = {
        "DJANGO_SECRET_KEY": secrets.token_hex(48),
        "DJANGO_DEBUG": "1",
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1",
        "PGHOST": "127.0.0.1",
        "PGPORT": available_port(55437),
        "PGDATABASE": "linkedin_jobs",
        "PGUSER": "linkedin_app",
        "PGPASSWORD": secrets.token_hex(32),
        "LINKEDIN_REQUEST_INTERVAL": "2",
    }
    temporary = ENV_FILE.with_suffix(".env.tmp")
    temporary.write_text("".join(f"{key}={value}\n" for key, value in entries.items()), encoding="ascii")
    temporary.replace(ENV_FILE)


def init_postgres(port):
    if (PG_DATA / "PG_VERSION").exists():
        return
    print("Creating a local PostgreSQL cluster...", flush=True)
    with TemporaryDirectory(dir=LOCAL) as folder:
        data = Path(folder) / "pgdata"
        subprocess.run(
            [str(PG_RUNTIME / "bin" / "initdb.exe"), "-D", str(data), "-U", "postgres",
             "--auth=scram-sha-256", "--encoding=UTF8", "--no-locale", f"--pwfile={PG_SETUP}"],
            check=True,
        )
        with (data / "postgresql.conf").open("a", encoding="ascii") as config:
            config.write(f"\nlisten_addresses = '127.0.0.1'\nport = {port}\n")
        data.rename(PG_DATA)


def prepare_database():
    from dotenv import dotenv_values
    import psycopg
    from psycopg import sql

    local_database = not ENV_FILE.exists() or PG_SETUP.exists() or (PG_DATA / "PG_VERSION").exists()
    if not ENV_FILE.exists() and not PG_SETUP.exists():
        PG_SETUP.write_text(secrets.token_hex(32) + "\n", encoding="ascii")
    write_environment()
    config = {key: value for key, value in dotenv_values(ENV_FILE).items() if value is not None}
    os.environ.update(config)
    if not local_database:
        return

    port = int(config["PGPORT"])
    install_postgres()
    init_postgres(port)
    if not pg_running():
        subprocess.run(
            [str(PG_CTL), "-D", str(PG_DATA), "-l", str(LOCAL / "postgresql.log"), "-w", "start"],
            check=True,
        )
    if not PG_SETUP.exists():
        return

    with psycopg.connect(
        host="127.0.0.1", port=port, dbname="postgres", user="postgres",
        password=PG_SETUP.read_text(encoding="ascii").strip(), autocommit=True,
        connect_timeout=5,
    ) as database:
        user = config["PGUSER"]
        name = config["PGDATABASE"]
        if database.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [user]).fetchone() is None:
            database.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(user), sql.Literal(config["PGPASSWORD"]),
            ))
        if database.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name]).fetchone() is None:
            database.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(user),
            ))
    PG_SETUP.unlink()


def prepare_admin():
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from django.contrib.auth import get_user_model
    from jobs.models import SearchQuery

    users = get_user_model().objects
    if users.filter(is_superuser=True, is_active=True).exists():
        return
    username = "admin"
    if users.filter(username=username).exists():
        username = f"admin-{secrets.token_hex(3)}"
    password = secrets.token_urlsafe(24)
    users.create_superuser(username=username, password=password)
    SearchQuery.objects.get_or_create(keywords="Python Developer", country="USA")
    print(f"\nFirst login\n  Username: {username}\n  Password: {password}\n", flush=True)


def serve(port, open_browser):
    address = f"127.0.0.1:{port}"
    url = f"http://{address}/admin/"
    print(f"Django Admin: {url}", flush=True)
    django = subprocess.Popen(
        [sys.executable, "manage.py", "runserver", address, "--noreload"],
        cwd=ROOT,
    )
    try:
        if open_browser:
            for _ in range(60):
                if django.poll() is not None:
                    break
                try:
                    with urlopen(url, timeout=1) as response:
                        ready = response.status == 200
                except (URLError, TimeoutError):
                    ready = False
                if ready:
                    webbrowser.open(url + "jobs/searchquery/")
                    break
                time.sleep(0.25)
        return django.wait()
    finally:
        if django.poll() is None:
            django.terminate()
            django.wait()


def main():
    parser = argparse.ArgumentParser(description="Set up and run the local LinkedIn parser.")
    parser.add_argument("action", nargs="?", choices=("start", "stop"), default="start")
    parser.add_argument("--no-admin-prompt", action="store_true", help="Skip admin creation.")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    LOCAL.mkdir(exist_ok=True)
    if args.action == "stop":
        if pg_running():
            subprocess.run([str(PG_CTL), "-D", str(PG_DATA), "-m", "fast", "-w", "stop"], check=True)
        return 0

    with exclusive_start():
        install_dependencies()
        prepare_database()
        manage("migrate", "--noinput")
        if not args.no_admin_prompt:
            prepare_admin()
        return serve(available_port(8000), not args.no_browser)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Startup error: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(0)
