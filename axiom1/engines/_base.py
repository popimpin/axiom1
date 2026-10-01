from pathlib import PurePosixPath, PureWindowsPath


class EngineError(ValueError):
    """An engine refusing rather than guessing. The message says what was wrong and what would be accepted."""


def relative_path(path):
    """A path inside the job's folder: relative, no '..'. Engines never write anywhere else."""
    if not isinstance(path, str) or not path.strip():
        raise EngineError("path must be a non-empty string")
    if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute() or PureWindowsPath(path).drive:
        raise EngineError(f"path {path!r} must be relative to the job's folder")
    if ".." in PurePosixPath(path.replace("\\", "/")).parts:
        raise EngineError(f"path {path!r} must not leave the job's folder")
    return path
