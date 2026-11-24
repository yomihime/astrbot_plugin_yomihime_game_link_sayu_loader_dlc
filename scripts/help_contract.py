"""Compare standalone legacy/new help renderers and registry with pinned native functions."""

import argparse
import asyncio
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_local import worker_archive  # noqa: E402
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


async def observe(mode, archive):
    from PIL import Image

    work = Path.cwd()
    if mode == "native":
        sys.path.insert(0, str(work))
    else:
        sys.path.insert(0, str(archive))
        import sayu_compat
        from plugin_runtime import PluginRuntime

        sayu_compat.install("Fixture", "fx", work, source_root=work)
        PluginRuntime(work).install(work, "Fixture")
    sys.addaudithook(offline)
    random.seed(41)
    from gsuid_core.data_store import get_res_path
    from gsuid_core.help.draw_new_plugin_help import get_new_help
    from gsuid_core.help.draw_plugin_help import get_help
    from gsuid_core.help.model import PluginHelp, PluginSV
    from gsuid_core.help.utils import clean_plugin_help, plugins_help, register_help
    from gsuid_core.utils.fonts.fonts import core_font

    result = {"models": [sorted(t.__required_keys__) for t in (PluginHelp, PluginSV)]}
    sample = Image.new("RGB", (64, 64), "#273871")
    register_help("Fixture", "fxhelp", sample)
    register_help("Fixture", "fxhelp", sample.copy())
    group = plugins_help["插件帮助一览"]["data"]
    assert len(group) == 1
    result["registry"] = {
        k: (v.mode if k == "icon" else v) for k, v in group[0].items()
    }
    data = {
        "常用": {
            "desc": "示例",
            "data": [{"name": "绑定", "desc": "说明", "eg": "fx示例", "highlight": 1}],
        },
        "管理": {
            "desc": "特权",
            "pm": 1,
            "data": [{"name": "状态", "desc": "检查", "eg": "fx状态"}],
        },
    }

    def save(label, raw):
        with Image.open(BytesIO(raw)) as image:
            result[label] = [
                image.format,
                image.size,
                sha256(image.convert("RGB").tobytes()).hexdigest(),
                sha256(raw).hexdigest(),
            ]

    for pm, theme in ((6, "dark"), (1, "light")):
        args = ("Fixture", {"Public": "any"}, sample, data)
        opts = {
            "plugin_prefix": "fx",
            "help_mode": theme,
            "column": 2,
            "pm": pm,
            "need_cover": True,
        }
        save(f"modern-{pm}", await get_new_help(*args, **opts))
        save(f"cached-{pm}", await get_new_help(*args, **opts))

    def rgba(size):
        return Image.new("RGBA", size, (34, 50, 70, 255))

    old = (
        "Fixture",
        "旧版帮助",
        data,
        rgba((1000, 1000)),
        sample,
        rgba((900, 90)),
        rgba((1340, 85)),
        rgba((260, 96)),
        core_font,
    )
    save("classic", await get_help(*old, column=3))
    save("classic-cached", await get_help(*old, column=3))
    from bs4 import BeautifulSoup
    from gsuid_core.utils.error_reply import (
        draw_error_img,
        get_error_img,
        get_error_type,
    )
    from gsuid_core.utils.image import convert
    from gsuid_core.utils.image.mys.bbs import soup_to_img

    for code in (-51, 10104, -400, 400, 999):
        result[f"error-{code}"] = [get_error_type(code), await get_error_img(code)]
    try:
        save("binding-error-artwork", await get_error_img(-51, force_image=True))
    except RuntimeError as exc:
        result["binding-error-artwork"] = [type(exc).__name__, str(exc)]
    save("error-artwork", await draw_error_img(400, "合成错误", "数据错误"))
    paragraph = "合成样本。\nCompatibility 12.5% / 1000!"
    result["line-layout"] = [
        await convert.str_lenth(paragraph, 16, 40),
        convert.get_str_size(paragraph, core_font(18), 60),
        convert.get_height(paragraph, 24),
        [convert.number_to_chinese(n) for n in (12, 12.5, 1234, 13456, 345678901)],
    ]
    save("text-image", await convert.text2pic("合成样本。图文", 360, 16))
    markup = '<h2>合成标题</h2><p>内容</p><div class="ql-divider"><img src="https://mihoyo-community-web.invalid/fixture"></div>'
    save("post-body", await soup_to_img(BeautifulSoup(markup, "lxml")))
    help_dir = get_res_path("help")
    result["cache_files"] = sorted(p.name for p in help_dir.glob("*.jpg"))
    clean_plugin_help("Fixture")
    result["cleared"] = [
        len(plugins_help["插件帮助一览"]["data"]),
        sorted(p.name for p in help_dir.glob("*.jpg")),
    ]
    if mode == "loader":
        from port_errors import Unsupported

        try:
            clean_plugin_help("../Fixture")
        except Unsupported:
            pass
        else:
            raise AssertionError("unsafe help path accepted")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("loader", "native"))
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode:
        result = asyncio.run(observe(args.mode, args.archive))
    else:
        result = {"reference": REFERENCE_COMMIT}
        with tempfile.TemporaryDirectory(prefix="sayu-help-") as tmp:
            root = Path(tmp)
            archive = root / "worker.pyz"
            archive.write_bytes(worker_archive({"package": "Fixture", "prefix": "fx"}))
            for mode in ("loader", "native"):
                work = root / mode
                work.mkdir()
                if mode == "native":
                    copy_reference(args.reference, work)
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
                        "--archive",
                        str(archive),
                        "--output",
                        str(output),
                    ],
                    cwd=work,
                    timeout=90,
                    check=True,
                )
                result[mode] = json.loads(output.read_bytes())
        result["differences"] = [
            k
            for k in result["loader"]
            if result["native"].get(k) != result["loader"][k]
        ]
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    if result.get("differences"):
        raise SystemExit(str(result["differences"]))


if __name__ == "__main__":
    main()
