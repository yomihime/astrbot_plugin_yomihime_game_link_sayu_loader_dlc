"""Populate Windows architecture variables for this isolated interpreter."""

import os
import platform


class UnsupportedWindowsArchitecture(ValueError):
    pass


def _machine_names(process, native):
    # IsWow64Process2 reports IMAGE_FILE_MACHINE_UNKNOWN for a native process.
    names = {0x014C: "x86", 0x01C4: "ARM", 0x8664: "AMD64", 0xAA64: "ARM64"}
    try:
        return names[process or native], names[native]
    except KeyError as exc:
        raise UnsupportedWindowsArchitecture("unknown Windows machine type") from exc


def _windows_machines():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        query = kernel.IsWow64Process2
    except AttributeError:
        # Before IsWow64Process2, GetSystemInfo reports the calling process's
        # view and GetNativeSystemInfo reports the native system architecture.
        class SystemInfo(ctypes.Structure):
            _fields_ = [
                ("architecture", wintypes.WORD),
                ("reserved", wintypes.WORD),
                ("page_size", wintypes.DWORD),
                ("minimum_address", ctypes.c_void_p),
                ("maximum_address", ctypes.c_void_p),
                ("processor_mask", ctypes.c_size_t),
                ("processor_count", wintypes.DWORD),
                ("processor_type", wintypes.DWORD),
                ("allocation_granularity", wintypes.DWORD),
                ("processor_level", wintypes.WORD),
                ("processor_revision", wintypes.WORD),
            ]

        machines = {0: 0x014C, 5: 0x01C4, 9: 0x8664, 12: 0xAA64}
        process, native = SystemInfo(), SystemInfo()
        kernel.GetSystemInfo(ctypes.byref(process))
        kernel.GetNativeSystemInfo(ctypes.byref(native))
        try:
            return machines[process.architecture], machines[native.architecture]
        except KeyError as exc:
            raise UnsupportedWindowsArchitecture(
                "unknown Windows processor type"
            ) from exc

    process, native = wintypes.WORD(), wintypes.WORD()
    query.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.WORD),
    )
    query.restype = wintypes.BOOL
    if not query(wintypes.HANDLE(-1), ctypes.byref(process), ctypes.byref(native)):
        raise ctypes.WinError(ctypes.get_last_error())
    return process.value, native.value


def configure_worker_architecture():
    if os.name != "nt":
        return
    process, native = _machine_names(*_windows_machines())
    os.environ["PROCESSOR_ARCHITECTURE"] = process
    if process == native:
        os.environ.pop("PROCESSOR_ARCHITEW6432", None)
    else:
        os.environ["PROCESSOR_ARCHITEW6432"] = native
    # Dependencies can have primed uname during worker imports. CPython
    # 3.12/3.13 have a single uname cache and no public invalidation function.
    if hasattr(platform, "invalidate_caches"):
        platform.invalidate_caches()
    else:
        platform._uname_cache = None
