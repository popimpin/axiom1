"""The rules every engine is held to. `problems(module)` returns what is wrong with one engine; an engine
joins the harness only with none. tests/test_engines.py runs it on every engine in the package."""
import ast
import inspect
import sys

from ._base import EngineError

# reaching outside the job (network, processes, the environment) is never an engine's business
FORBIDDEN = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "subprocess", "multiprocessing",
             "ctypes", "asyncio", "threading", "random", "secrets", "shutil", "tempfile", "os", "sys"}
SPEC_KEYS = {"name", "version", "summary", "functions"}
FUNCTION_KEYS = {"args", "returns", "io", "examples"}


def _imports(source):
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [(a.name.split(".")[0], 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append(((node.module or "").split(".")[0], node.level))
    return found


def problems(module):
    out = []
    name = module.__name__.rsplit(".", 1)[-1]
    spec = getattr(module, "SPEC", None)
    if not isinstance(spec, dict):
        return [f"{name}: no SPEC dict"]
    if set(spec) != SPEC_KEYS:
        out.append(f"{name}: SPEC keys must be exactly {sorted(SPEC_KEYS)}, got {sorted(spec)}")
    if spec.get("name") != name:
        out.append(f"{name}: SPEC name {spec.get('name')!r} must match the module name")
    if not isinstance(spec.get("version"), int) or spec.get("version", 0) < 1:
        out.append(f"{name}: version must be an int >= 1")
    if not isinstance(spec.get("summary"), str) or not spec.get("summary", "").strip():
        out.append(f"{name}: summary must be a non-empty string")

    guide = getattr(module, "GUIDE", None)
    if not isinstance(guide, str) or not guide.strip():
        out.append(f"{name}: needs a GUIDE: how to use it, what a refusal means, what it combines with")
    elif isinstance(spec.get("functions"), dict):
        # the manual is how an agent learns the engine; a function the guide never shows is one it will guess at
        unnamed = [fn for fn in spec["functions"] if f"{fn}(" not in guide]
        if unnamed:
            out.append(f"{name}: GUIDE never shows how to call {unnamed}")

    for mod, level in _imports(inspect.getsource(module)):
        if level:
            continue                                   # another engine or _base, relatively
        if mod in FORBIDDEN:
            out.append(f"{name}: imports {mod!r}, which reaches outside the job")
        elif mod not in sys.stdlib_module_names:
            out.append(f"{name}: imports {mod!r}, which is not in the standard library")

    functions = spec.get("functions")
    if not isinstance(functions, dict) or not functions:
        return out + [f"{name}: SPEC functions must be a non-empty dict"]
    for fn_name, fs in functions.items():
        where = f"{name}.{fn_name}"
        fn = getattr(module, fn_name, None)
        if not callable(fn):
            out.append(f"{where}: listed in SPEC but not defined")
            continue
        if not isinstance(fs, dict) or set(fs) != FUNCTION_KEYS:
            out.append(f"{where}: keys must be exactly {sorted(FUNCTION_KEYS)}")
            continue
        params = list(inspect.signature(fn).parameters)
        if list(fs["args"]) != params:
            out.append(f"{where}: SPEC args {list(fs['args'])} do not match the signature {params}")
        if not fn.__doc__:
            out.append(f"{where}: needs a docstring")
        if fs["io"]:
            continue                                   # file functions are tested in the engine's own tests
        examples = fs["examples"]
        if not any("returns" in e for e in examples):
            out.append(f"{where}: needs at least one example with 'returns'")
        if not any("refuses" in e for e in examples):
            out.append(f"{where}: needs at least one example it refuses (refuse, don't guess)")
        for i, ex in enumerate(examples):
            try:
                got = fn(**ex["args"])
            except EngineError as e:
                if "refuses" not in ex:
                    out.append(f"{where} example {i}: refused ({e}) but should return {ex.get('returns')!r}")
                elif ex["refuses"].lower() not in str(e).lower():
                    out.append(f"{where} example {i}: refused with {str(e)!r}, expected it to mention "
                               f"{ex['refuses']!r}")
                continue
            except Exception as e:                       # anything but EngineError is a crash, not a refusal
                out.append(f"{where} example {i}: crashed with {type(e).__name__}: {e}")
                continue
            if "refuses" in ex:
                out.append(f"{where} example {i}: returned {got!r} but should refuse ({ex['refuses']})")
            elif got != ex["returns"]:
                out.append(f"{where} example {i}: returned {got!r}, expected {ex['returns']!r}")
    return out
