"""Owner-authored external observers for bounded JSON function cases.

The supervisor never imports candidate code. Each case runs in a separate child;
exit-zero without exactly one well-formed result cannot pass. This proves only
the supplied cases, not general correctness or resistance to all hostile code.
"""
from __future__ import annotations

import base64
import builtins
import json
import re
from typing import Any

from patchington.contracts import CheckSpec

_CHILD = r'''
import base64, importlib, json, os, sys
module, function, encoded = sys.argv[1:]
case = json.loads(base64.b64decode(encoded))
sys.path.insert(0, os.getcwd())
try:
    target = getattr(importlib.import_module(module), function)
    value = target(*case["args"], **case["kwargs"])
    result = {"kind": "return", "value": value}
except BaseException as error:
    result = {"kind": "raise", "exception": type(error).__name__}
try:
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
except (TypeError, ValueError):
    sys.exit(3)
'''

_SUPERVISOR = r'''
import base64, json, os, selectors, signal, subprocess, sys, time
payload = json.loads(base64.b64decode(sys.argv[1]))
child_code = base64.b64decode(payload["child"]).decode()
completed = 0

def reject(detail):
    print("PATCHINGTON_PROBE_FAILED completed=%d/%d detail=%s" %
          (completed, len(payload["cases"]), detail), flush=True)
    sys.exit(1)

def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate result key")
        result[key] = value
    return result

for case in payload["cases"]:
    encoded = base64.b64encode(json.dumps({"args": case["args"], "kwargs": case["kwargs"]},
                                         allow_nan=False).encode()).decode()
    argv = [sys.executable, "-I", "-S", "-B", "-c", child_code,
            payload["module"], payload["function"], encoded]
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    output = bytearray()
    problem = None
    deadline = time.monotonic() + payload["case_timeout"]
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            if time.monotonic() >= deadline:
                problem = "case deadline exceeded"
                break
            for key, _ in selector.select(min(0.1, max(0, deadline-time.monotonic()))):
                chunk = os.read(key.fd, min(65536, payload["output_limit"] + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > payload["output_limit"]:
                    problem = "case output exceeded bound"
                    break
            if problem:
                break
    if problem:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if process.poll() is None:
                process.kill()
    try:
        code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
        reject("child did not terminate")
    process.stdout.close()
    if problem:
        reject(problem)
    if code != 0:
        reject("child process failed")
    try:
        lines = bytes(output).decode("utf-8").splitlines()
        if len(lines) != 1:
            raise ValueError("missing or extra output")
        result = json.loads(lines[0], object_pairs_hook=no_duplicate_keys,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if not isinstance(result, dict):
            raise ValueError("result is not an object")
        if "raises" in case:
            expected = {"kind": "raise", "exception": case["raises"]}
        else:
            expected = {"kind": "return", "value": case["expected"]}
        if json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) != \
                json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False):
            raise ValueError("unexpected result")
    except (ValueError, UnicodeError, TypeError):
        reject("missing, extra, malformed or unexpected result")
    completed += 1
print("PATCHINGTON_PROBE_EXECUTED=%d/%d" % (completed, len(payload["cases"])), flush=True)
'''


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > 10:
        raise ValueError("probe case nesting exceeds bound")
    if value is None or type(value) in {bool, int, float, str}:
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _json_value(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item, depth + 1)
        return
    raise ValueError("probe cases accept closed JSON values only")


def function_probe(name: str, module: str, function: str, cases: tuple[dict, ...], *,
                   python: str = "python", timeout: int = 30, case_timeout: int = 2,
                   output_limit: int = 65536, regression: bool = True) -> CheckSpec:
    """Freeze trusted expected results into argv, kept outside candidate source.

    Each case supplies ``args`` (list), optional ``kwargs`` (dict), and exactly one
    of ``expected`` (closed JSON) or ``raises`` (a builtin exception class name).
    """
    identifier = r"[A-Za-z_][A-Za-z0-9_]*"
    if not re.fullmatch(identifier + r"(?:\." + identifier + r")*", module):
        raise ValueError("invalid probe module identifier")
    if not re.fullmatch(identifier, function):
        raise ValueError("invalid probe function identifier")
    if not isinstance(cases, tuple) or not 1 <= len(cases) <= 100:
        raise ValueError("one to 100 immutable probe cases required")
    if not 1 <= case_timeout <= 30 or not 1024 <= output_limit <= 1_000_000:
        raise ValueError("invalid probe execution bounds")
    normalized = []
    for case in cases:
        if (not isinstance(case, dict) or set(case) - {"args", "kwargs", "expected", "raises"}
                or ("expected" in case) == ("raises" in case)):
            raise ValueError("invalid probe case schema")
        args = case.get("args", [])
        kwargs = case.get("kwargs", {})
        if not isinstance(args, list) or len(args) > 20 or not isinstance(kwargs, dict):
            raise ValueError("probe input must use bounded JSON args/kwargs")
        _json_value(case)
        if "raises" in case:
            exception = getattr(builtins, case["raises"], None) if isinstance(case["raises"], str) else None
            if not isinstance(exception, type) or not issubclass(exception, BaseException):
                raise ValueError("expected exception must be a builtin exception name")
        normalized.append({**case, "args": args, "kwargs": kwargs})
    payload = {"module": module, "function": function, "cases": normalized,
               "case_timeout": case_timeout, "output_limit": output_limit,
               "child": base64.b64encode(_CHILD.encode()).decode()}
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError) as exc:
        raise ValueError("probe cases must be finite JSON values") from exc
    if len(encoded) > 64_000:
        raise ValueError("probe payload exceeds bound")
    return CheckSpec(name=name, argv=(python, "-I", "-S", "-c", _SUPERVISOR,
                                      base64.b64encode(encoded).decode()),
                     required=True, regression=regression, timeout=timeout)


def delivery_fee_probe(*, python: str = "python", timeout: int = 30,
                       case_timeout: int = 2, output_limit: int = 65536) -> CheckSpec:
    """The owner-defined fee boundary cases used by the local qualification demo."""
    return function_probe("delivery-fee-boundary", "src.fees", "delivery_fee", (
        {"args": [49], "expected": 5}, {"args": [50], "expected": 0},
        {"args": [51], "expected": 0}), python=python, timeout=timeout,
        case_timeout=case_timeout, output_limit=output_limit, regression=True)
