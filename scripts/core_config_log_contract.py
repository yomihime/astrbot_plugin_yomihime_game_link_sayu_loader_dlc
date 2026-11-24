"""Offline parity probes for private Core settings and severity-only worker diagnostics."""

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


def observe(mode):
    sys.addaudithook(offline)
    work = Path.cwd()
    if mode == "native":
        sys.path.insert(0, str(work))
        from gsuid_core.config import CONFIG_DEFAULT, CoreConfig, core_config
        from gsuid_core.logger import logger, log_history

        rebuild, ctx = CoreConfig, nullcontext
    else:
        sys.path.insert(0, str(ROOT / "worker"))
        from plugin_core_config import install_core_config
        from plugin_logger import install_logger
        from port_errors import Unsupported
        from service_settings import management_scope

        module, log = ModuleType("config"), ModuleType("logger")

        def rebuild():
            return install_core_config(module, work)

        core_config = rebuild()
        CONFIG_DEFAULT = module.CONFIG_DEFAULT
        diag = install_logger(log, core_config, source_root=work)
        logger = log.logger

        def ctx():
            return management_scope(writable=True)

    keys = (
        "HOST",
        "PORT",
        "LANGUAGE",
        "masters",
        "superusers",
        "command_start",
        "log",
        "misfire_grace_time",
        "enable_empty_start",
    )
    results = {
        "defaults": {k: [CONFIG_DEFAULT[k], core_config.get_config(k)] for k in keys}
    }
    with ctx():
        results["unknown"] = [
            core_config.get_config("absent"),
            core_config.set_config("absent", True),
        ]
        results["language"] = [
            core_config.set_config("LANGUAGE", "en"),
            rebuild().get_config("LANGUAGE"),
        ]
        core_config.lazy_set_config("LANGUAGE", "ja")
        results["lazy"] = [
            core_config.get_config("LANGUAGE"),
            rebuild().get_config("LANGUAGE"),
        ]
        core_config.lazy_write_config()
        results["lazy"].append(rebuild().get_config("LANGUAGE"))
    if mode == "native":
        log_history.clear()
    bound = logger.bind(category="fixture")
    accepted = []
    for severity in (
        "trace",
        "debug",
        "info",
        "success",
        "warning",
        "error",
        "critical",
        "exception",
    ):
        accepted.append(getattr(bound, severity)("synthetic event") is None)
    results["logged"] = accepted
    results["opt_present"] = hasattr(logger, "opt")
    for label, op in (
        ("unbind", lambda: bound.unbind("missing")),
        ("new_unbind", lambda: bound.new(other="value").unbind("other")),
    ):
        try:
            op()
        except Exception as e:
            results[label] = type(e).__name__
        else:
            results[label] = "ok"
    results["levels"] = (
        [r.level.upper() for r in log_history]
        if mode == "native"
        else [level for level, _ in diag.recent]
    )
    if mode == "loader":
        for key in ("masters", "superusers", "WS_TOKEN", "REGISTER_CODE"):
            with ctx():
                try:
                    core_config.set_config(key, "fixture")
                except Unsupported:
                    pass
                else:
                    raise AssertionError("host authority can be overwritten")
        try:
            core_config.set_config("LANGUAGE", "en")
        except Unsupported:
            pass
        else:
            raise AssertionError("ungranted profile write")
        copy = core_config.get_config("masters")
        copy.append("fixture")
        assert not core_config.get_config("masters")
        other = install_core_config(ModuleType("other"), work / "other")
        assert other.get_config("LANGUAGE") == "zh-cn"
        marker = "sensitive-test-value"
        for _ in range(100):
            logger.error(marker, metadata=marker, exc_info=RuntimeError(marker))
        assert len(diag.recent) == 64 and diag.counts["ERROR"] >= 100
        assert marker not in repr(diag.recent) and marker not in repr(diag.counts)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("native", "loader"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    args = parser.parse_args()
    if args.mode:
        result = observe(args.mode)
    else:
        result = {"reference": REFERENCE_COMMIT}
        with tempfile.TemporaryDirectory(prefix="sayu-config-log-") as tmp:
            for mode in ("loader", "native"):
                work = Path(tmp) / mode
                work.mkdir()
                if mode == "native":
                    copy_reference(args.reference.resolve(), work)
                output = work / "observed.json"
                subprocess.run(
                    [
                        sys.executable,
                        "-IB",
                        "-X",
                        "utf8",
                        __file__,
                        "--mode",
                        mode,
                        "--output",
                        str(output),
                    ],
                    cwd=work,
                    check=True,
                    timeout=60,
                )
                result[mode] = json.loads(output.read_bytes())
        if result["loader"] != result["native"]:
            raise AssertionError(result)
    args.output.write_text(
        json.dumps(result, ensure_ascii=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
