"""Load explicit mounted secrets for this role, then replace PID 1 with the CLI."""

import argparse
import os
import sys
from pathlib import Path

SECRET_NAMES = (
    "PATCHINGTON_DATABASE_URL",
    "PATCHINGTON_WEBHOOK_SECRET",
    "PATCHINGTON_APPROVAL_SECRET",
    "PATCHINGTON_READ_APP_KEY",
    "PATCHINGTON_WRITE_APP_KEY",
)


def load_secrets(environment):
    result = dict(environment)
    for name in SECRET_NAMES:
        filename = result.pop(f"{name}_FILE", None)
        if not filename:
            continue
        if result.get(name):
            raise ValueError(f"configure either {name} or its file, never both")
        data = Path(filename).read_bytes()
        if not data or len(data) > 65536:
            raise ValueError(f"invalid size for {name} secret file")
        result[name] = data.decode("utf-8").rstrip("\r\n")
    return result


def runtime_role(arguments):
    if not arguments:
        return None
    if arguments[0] != "doctor":
        return arguments[0]
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument("--role", default="worker")
    try:
        known, _ = parser.parse_known_args(arguments[1:])
    except argparse.ArgumentError as error:
        raise ValueError("invalid diagnostic role") from error
    return known.role


def main():
    try:
        environment = load_secrets(os.environ)
        credentials = environment.pop("PATCHINGTON_CLAUDE_CREDENTIALS_FILE", None)
        if credentials:
            if runtime_role(sys.argv[1:]) != "worker":
                raise ValueError("subscription authentication belongs only to workers")
            data = Path(credentials).read_bytes()
            if not data or len(data) > 65536:
                raise ValueError("invalid subscription authentication file")
            directory = Path(environment["HOME"]) / ".claude"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination = directory / ".credentials.json"
            destination.write_bytes(data)
            destination.chmod(0o600)
    except (OSError, ValueError, UnicodeError):
        print("mounted service secrets are unavailable or invalid", file=sys.stderr)
        raise SystemExit(1) from None
    # Fixed interpreter/module and explicit argv preserve signals without a shell.
    os.execve(  # noqa: S606
        sys.executable, [sys.executable, "-m", "patchington.cli", *sys.argv[1:]], environment
    )


if __name__ == "__main__":
    main()
