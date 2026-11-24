import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import resource_index as resources


class Tree(dict):
    def iteritems(self):
        return ((name, mode, oid) for name, (mode, oid) in self.items())


class Blob(bytes):
    def as_raw_string(self):
        return bytes(self)


class IndexTests(unittest.TestCase):
    def test_complete_directory_exact_objects_and_lazy_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "resource"
            index = resources.ResourceIndex(resources.PLUGIN, root)
            table = root / "map" / "tables"
            forest = {
                resources.ROOT_TREE: Tree(
                    {
                        name.encode(): (0o040000, name.encode())
                        for name in resources.ROOTS.values()
                    }
                ),
                **{name.encode(): Tree() for name in resources.ROOTS.values()},
                b"map": Tree({b"tables": (0o040000, b"tables")}),
                b"tables": Tree(
                    {
                        b"a.json": (0o100644, b"a"),
                        b"b.json": (0o100755, b"b"),
                        b"outside.json": (0o120000, b"link"),
                        b"data.exe": (0o100755, b"exe"),
                    }
                ),
                b"weapon": Tree({b"1.png": (0o100644, b"image")}),
            }
            requests = []

            def fetch(ids, *, tree=False, budget):
                requests.append(ids)
                if tree:
                    return forest, 700
                return {oid: Blob(oid) for oid in ids}, sum(map(len, ids))

            with patch.object(resources, "_fetch", side_effect=fetch):
                self.assertFalse(index.ensure(root.parent / "outside"))
                self.assertTrue(index.ensure(table))
                self.assertTrue(table.is_dir())
                self.assertEqual(list(table.iterdir()), [])
                matches = index.entries(table, "*.json", True)
                self.assertEqual([p.read_bytes() for p in matches], [b"a", b"b"])
                self.assertFalse(index.ensure(table / "outside.json"))
                self.assertTrue(index.ensure(matches[0]))
                self.assertTrue(index.ensure(root / "waves_weapon/1.png"))
                self.assertEqual(
                    requests, [{resources.ROOT_TREE}, {b"a", b"b"}, {b"image"}]
                )

    def test_disabled_provider_does_no_io(self):
        index = resources.ResourceIndex("another", Path.cwd())
        with patch.object(resources, "_fetch") as fetch:
            self.assertEqual(index.entries(Path.cwd() / "map", "*", True), [])
            self.assertFalse(index.ensure(Path.cwd() / "map/data.json"))
            fetch.assert_not_called()
