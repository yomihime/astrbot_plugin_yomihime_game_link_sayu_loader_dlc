"""Keep admin model and page declarations local to their worker generation."""

from pathlib import Path
import sys
from typing import Any

from pydantic import BaseModel


class PageSchema(BaseModel):
    label: str
    icon: str


class GsAdminModel:
    model: Any = None
    pk_name: str = "id"
    page_schema: Any = None


def install_admin(package, source, plugin):
    plugin_root = (Path(source) / plugin).resolve()

    class Site:
        def __init__(self):
            self.plugins_page = {}
            self._registered = {}
            self.is_start = False

        def register_admin(self, *admin_cls, _ADD=False):
            caller = Path(sys._getframe(1).f_code.co_filename).resolve()
            if not _ADD and caller.is_relative_to(plugin_root):
                self.plugins_page.setdefault(plugin, []).extend(admin_cls)
            else:
                self._registered.update((cls, None) for cls in admin_cls if cls)
            return admin_cls[0]

    package("gsuid_core.webconsole")
    module = package("gsuid_core.webconsole.mount_app")
    module.PageSchema = PageSchema
    module.GsAdminModel = GsAdminModel
    module.Site = Site
    module.site = Site()
