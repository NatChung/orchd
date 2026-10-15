
# Never read the developer's real orchd settings: point the settings file at a throwaway path, clear the env override.
import atexit as _atexit, os as _os, tempfile as _tempfile
_cfg = _tempfile.TemporaryDirectory()
_atexit.register(_cfg.cleanup)
_os.environ["ORCHD_CONFIG_FILE"] = _os.path.join(_cfg.name, "config.toml")
_os.environ.pop("ORCHD_WORKER_MODEL", None)
# Tests never rewrite the developer's real Orch home at startup; startup-migration tests opt in explicitly.
_os.environ["ORCHD_AUTO_MIGRATE"] = "0"
