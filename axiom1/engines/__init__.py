"""Engines: the deterministic parts of a job, written once, tested, and shared by every process.

The model reads the inputs and fills an entry; engines do what has one right answer (parse a time, add a
duration, write a CSV with exact columns). An engine that cannot be sure refuses with EngineError instead
of guessing, because the person relying on the result is not going to check it.

Every engine is one module in this package and grows the harness by being dropped in:
  - standard library only, and engines import each other only relatively, so the package can be copied
    into a job's tree (as `axiom_engines/`) and run in a bare python container
  - a SPEC naming the engine, its version and its functions, with worked examples that are also its tests
    (see _contract.py for the rules every engine is held to)
"""
import importlib
import pkgutil

from ._base import EngineError

__all__ = ["EngineError", "specs", "versions", "call", "summary"]


def _modules():
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if not info.name.startswith("_"):
            yield importlib.import_module(f"{__name__}.{info.name}")


def specs():
    return {mod.SPEC["name"]: mod.SPEC for mod in _modules()}


def versions():
    return {name: spec["version"] for name, spec in specs().items()}


def call(engine, function, args=None, allow_io=False):
    """Call one engine function by name. File-touching functions (io: true) are refused unless allowed:
    the agent's `engine` tool explores with pure functions; processes call engines directly."""
    mods = {mod.SPEC["name"]: mod for mod in _modules()}
    if engine not in mods:
        raise EngineError(f"no engine named {engine!r}; engines: {', '.join(sorted(mods))}")
    spec = mods[engine].SPEC["functions"]
    if function not in spec:
        raise EngineError(f"engine {engine!r} has no function {function!r}; it has: {', '.join(sorted(spec))}")
    if spec[function].get("io") and not allow_io:
        raise EngineError(f"{engine}.{function} reads or writes files; call it from process.py instead")
    args = args or {}
    expected = list(spec[function]["args"])
    wrong = [a for a in args if a not in expected]
    missing = [a for a in expected if a not in args]
    if wrong or missing:
        raise EngineError(f"{engine}.{function} takes ({', '.join(expected)})"
                          + (f"; unknown: {', '.join(wrong)}" if wrong else "")
                          + (f"; missing: {', '.join(missing)}" if missing else ""))
    return getattr(mods[engine], function)(**args)


def summary():
    """One line per function, for the agent's briefing."""
    lines = []
    for name, spec in specs().items():
        lines.append(f"{name} (v{spec['version']}): {spec['summary']}")
        for fn, fs in spec["functions"].items():
            lines.append(f"  {name}.{fn}({', '.join(fs['args'])}) -> {fs['returns']}"
                         + ("  [files]" if fs.get("io") else ""))
    return "\n".join(lines)
