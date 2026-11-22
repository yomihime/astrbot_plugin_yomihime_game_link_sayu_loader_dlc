"""Read immutable game assets by Git object ID into one request's empty directory."""

from fnmatch import fnmatchcase
from io import BytesIO
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PLUGIN = "XutheringWavesUID"
COMMIT = "88d84d8d73855dd443c1e568164324ff5ff090d0"
# git rev-parse <COMMIT>:XutheringWavesUID/resource
ROOT_TREE = b"4b9df77ccc5a3f95632d5f431f52b6d9c23b8500"
URL = "https://cnb.cool/loping151/XutheringWavesUID-Resources.git/git-upload-pack"
MAX_BYTES = 24 * 1024 * 1024
# Mappings from the pinned upstream resource downloader and RESOURCE_PATH.
ROOTS = {
    "waves_avatar": "avatar",
    "waves_weapon": "weapon",
    "material": "material",
    "phantom": "phantom",
    "monster": "monster",
    "map": "map",
    "role_pile": "role_pile",
    "role_bg": "role_bg",
    "role_detail": "role_detail",
    "share": "share",
    "title_bg": "title_bg",
    "card_polygon": "card_polygon",
    "slash_buff": "slash_buff",
}
SUFFIXES = {".json", ".png", ".jpg", ".webp"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(ids, *, tree=False, budget):
    from dulwich.object_format import SHA1
    from dulwich.pack import PackData, PackInflater
    from dulwich.protocol import pkt_line

    # Stateless upload-pack v0, no sideband, no external delta bases.
    wants = [
        pkt_line(b"want " + oid + (b" filter" if tree else b"") + b"\n")
        for oid in sorted(ids)
    ]
    if tree:
        wants.append(pkt_line(b"filter blob:none\n"))
    body = b"".join(wants) + pkt_line(None) + pkt_line(b"done\n")
    req = Request(
        URL,
        data=body,
        headers={"Content-Type": "application/x-git-upload-pack-request"},
    )
    limit = min(budget, 512 * 1024 if tree else 8 * 1024 * 1024)
    with build_opener(ProxyHandler({}), _NoRedirect()).open(req, timeout=6) as res:
        raw = res.read(limit + 1)
    if len(raw) > limit or not raw.startswith(b"0008NAK\nPACK"):
        raise ValueError("invalid public resource pack")
    with PackData.from_file(BytesIO(raw[8:]), SHA1) as pack:
        if not 0 < len(pack) <= (512 if tree else len(ids)):
            raise ValueError("unexpected resource count")
        pack.check()
        objects = {}
        total = 0
        for obj in PackInflater.for_pack_data(pack):
            total += obj.raw_length()
            if total > budget or obj.type_name != (b"tree" if tree else b"blob"):
                raise ValueError("invalid public resource object")
            objects[obj.id] = obj
    if not ids.issubset(objects) or (not tree and ids != objects.keys()):
        raise ValueError("resource object identity mismatch")
    return objects, total


class ResourceIndex:
    def __init__(self, plugin, resource_root):
        self.root = Path(resource_root).resolve()
        self.enabled = plugin == PLUGIN
        self.files = {}
        self.dirs = set()
        self.remaining = MAX_BYTES

    def begin_invocation(self):
        self.remaining = MAX_BYTES

    def _load(self, path):
        path = Path(path)
        if (
            not self.enabled
            or path.resolve() != path
            or not path.is_relative_to(self.root)
        ):
            return False
        parts = path.relative_to(self.root).parts
        if (
            not parts
            or parts[0] not in ROOTS
            or (path.suffix and path.suffix.lower() not in SUFFIXES)
        ):
            return False
        if self.dirs:
            return True
        trees, _ = _fetch({ROOT_TREE}, tree=True, budget=2 * 1024 * 1024)
        files = {}
        dirs = set()

        def walk(folder, tree):
            dirs.add(folder)
            for name, mode, oid in tree.iteritems():
                name = name.decode("utf-8")
                if name in {".", ".."} or Path(name).name != name or ":" in name:
                    raise ValueError("invalid resource name")
                child = folder / name
                if mode == 0o040000:
                    walk(child, trees[oid])
                elif mode in (0o100644, 0o100755) and child.suffix.lower() in SUFFIXES:
                    files[child] = oid

        for local, remote in ROOTS.items():
            mode, oid = trees[ROOT_TREE][remote.encode()]
            if mode != 0o040000:
                raise ValueError("invalid resource directory")
            walk(self.root / local, trees[oid])
        self.files, self.dirs = files, dirs
        return True

    def _download(self, paths):
        if any(path.resolve() != path for path in paths):
            raise ValueError("resource path outside request data")
        missing = [path for path in paths if not path.is_file()]
        if not missing:
            return
        ids = {self.files[path] for path in missing}
        if len(ids) > 256 or self.remaining <= 0:
            raise ValueError("public resource budget exceeded")
        objects, total = _fetch(ids, budget=self.remaining)
        self.remaining -= total
        for path in missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(objects[self.files[path]].as_raw_string())

    def ensure(self, path):
        path = Path(path)
        if not self._load(path):
            return False
        if path in self.dirs:
            path.mkdir(parents=True, exist_ok=True)
        elif path in self.files:
            self._download([path])
        else:
            return False
        return True

    def entries(self, path, pattern, recursive):
        path = Path(path)
        if not self._load(path) or path not in self.dirs:
            return []
        matches = sorted(
            item
            for item in self.files
            if item.is_relative_to(path)
            and (recursive or item.parent == path)
            and fnmatchcase(item.name, pattern)
        )
        # The upstream table loader reads every matched JSON; one pack avoids N round trips.
        if matches and all(item.suffix == ".json" for item in matches):
            self._download(matches)
        return matches
