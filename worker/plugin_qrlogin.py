"""Render the gsuid_core QR payload for each adapter using private plugin storage."""

import base64
from io import BytesIO
import os
from pathlib import Path
import tempfile

from port_errors import Unsupported


def install_qrlogin(package, data_root):
    root = Path(data_root).resolve()

    async def get_qrcode_base64(url, path, bot_id):
        import qrcode

        code = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        code.add_data(url)
        code.make(fit=True)
        image = code.make_image(fill_color=(255, 134, 36), back_color="white")
        payload = BytesIO()
        if bot_id == "onebot":
            destination = Path(path).resolve()
            if not destination.is_relative_to(root) or destination == root:
                raise Unsupported(
                    "QR attachment must be written to private plugin data"
                )
            image = image.resize((700, 700))
            image.save(
                payload,
                format="PNG",
                save_all=True,
                append_images=[image],
                duration=100,
                loop=0,
            )
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, delete=False
            ) as stream:
                pending = Path(stream.name)
            try:
                pending.write_bytes(payload.getvalue())
                os.replace(pending, destination)
            finally:
                pending.unlink(missing_ok=True)
        else:
            image.save(payload, format="PNG")
        raw = payload.getvalue()
        return (
            "base64://" + base64.b64encode(raw).decode("ascii")
            if bot_id == "onebot_v12"
            else raw
        )

    package(
        "gsuid_core.utils.cookie_manager",
        Path(__file__).parent / "plugin_cookie_manager",
    )
    package(
        "gsuid_core.utils.cookie_manager.qrlogin"
    ).get_qrcode_base64 = get_qrcode_base64
