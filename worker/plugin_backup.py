"""The GsCore file-cleanup helper, confined to this plugin's private resources."""

from pathlib import Path, PureWindowsPath

from port_errors import Unsupported


def install_backup(package, data, plugin):
    root = Path(data).resolve() / "resources" / plugin

    def clear_path_all_file(path, pattern="*"):
        directory = Path(path).resolve()
        parts = PureWindowsPath(pattern)
        if (
            root.resolve() != root
            or not directory.is_relative_to(root)
            or directory == root
            or parts.anchor
            or ".." in parts.parts
        ):
            raise Unsupported("cache cleanup outside the current plugin resources")
        from gsuid_core.logger import logger

        for file in directory.glob(pattern):
            if not file.resolve().is_relative_to(directory):
                raise Unsupported("cache entry outside the selected directory")
            try:
                file.unlink()
            except OSError as exc:
                logger.warning(
                    "Could not clear cached file", file=file.name, error=exc.strerror
                )

    package("gsuid_core.utils.backup")
    package(
        "gsuid_core.utils.backup.backup_files"
    ).clear_path_all_file = clear_path_all_file
