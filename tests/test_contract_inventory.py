import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from contract_inventory import contract_record, symbols


class ContractInventoryTests(unittest.TestCase):
    def test_explicit_private_imports_and_sampled_methods_are_indexed(self):
        source = """
from base import _export
_setting = True
class _Client:
    def _request(self): ...
    def _other(self): ...
    def read(self): ...
"""
        names = {"_export", "_setting", "_Client", "_Client._request"}
        rows = symbols(ast.parse(source).body, referenced=names)
        self.assertEqual({name for name, _, _ in rows}, names | {"_Client.read"})

    def test_conditional_exports_and_declared_protocols_keep_native_provenance(self):
        source = """
try:
    import primary.api as adapter
except ImportError:
    from fallback import adapter
finally:
    ready: bool = True
class Stream:
    def __iter__(self): return self
async def lookup(key: str, *, limit=3) -> int: ...
"""
        entries = list(symbols(ast.parse(source).body))
        exports = [node for name, node, _ in entries if name == "adapter"]
        self.assertEqual([type(n) for n in exports], [ast.Import, ast.ImportFrom])
        self.assertIn("ready", {name for name, _, _ in entries})
        self.assertIn("Stream.__iter__", {name for name, _, _ in entries})
        name, node, kind = entries[-1]
        row = contract_record(name, kind, node, None)
        self.assertEqual(row["native_declaration"], "lookup(key: str, *, limit=3) -> int")
        self.assertEqual(row["loader_status"], "unverified")
        self.assertIsNone(row["behavior_probe"])
