"""Bind verified public APS helpers to the generation's private scheduler."""

import ast
from hashlib import sha256
from pathlib import Path
from textwrap import indent
from urllib.request import urlopen

from image_assets import BASE

PATH = "gsuid_core/aps.py"
DIGEST = "821d86728b30de28f03d725eb0fa8f0749d484bf36563e883d00dc3396ddf4da"
FUNCTIONS = (
    "start_scheduler",
    "shutdown_scheduler",
    "remove_repeat_job",
    "get_all_aps_job",
    "_get_trigger_description",
    "add_scheduled_job",
)
PRELUDE = """# Genshin-bots/gsuid_core 96cd7353, gsuid_core/aps.py.
# GPL-3.0, see licenses/gsuid_core-COPYING.txt.
import inspect
from typing import Any, List, Literal, Callable, Optional, Annotated
from msgspec import Meta
from apscheduler.job import Job
from gsuid_core.i18n import t
from gsuid_core.logger import logger

def bind(scheduler):
"""


def verified_scheduler(reference=None):
    if reference is not None and Path(reference).is_dir():
        stream = (Path(reference) / PATH).open("rb")
    else:
        stream = urlopen(BASE + PATH, timeout=30)
    with stream:
        data = stream.read(32768)
    if sha256(data).hexdigest() != DIGEST:
        raise ValueError("native scheduler helper checksum mismatch")
    src = data.decode("utf8")
    helpers = {
        n.name: ast.get_source_segment(src, n)
        for n in ast.parse(src).body
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    # The original global singleton is replaced with a local closure. Job objects
    # and helper function bodies retain the installed APScheduler behavior.
    body = "\n\n".join(helpers[n] for n in FUNCTIONS)
    bindings = ", ".join(f"{name!r}: {name}" for name in FUNCTIONS)
    code = PRELUDE + indent(body + "\nreturn {" + bindings + "}", "    ") + "\n"
    return {"plugin_scheduler_helpers.py": code.encode("utf8")}
