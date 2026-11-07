import ast
from pathlib import Path
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / "orchd"


class TextEncodingTests(unittest.TestCase):
    """Windows defaults to the ANSI code page (cp950 on zh-TW), so UTF-8 files such as a Chinese
    Orch AGENTS.md broke `orch start`. Every text read/write in the package must name its encoding."""

    def test_read_and_write_text_name_an_encoding(self):
        sources = [*PACKAGE.rglob("*.py"), PACKAGE / "worker_bin" / "gh"]
        missing = []
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("read_text", "write_text")
                        and not any(k.arg == "encoding" for k in node.keywords)
                        and "/proc/" not in ast.unparse(node.func.value)):  # Linux-only kernel files
                    missing.append(f"{path.relative_to(PACKAGE.parent)}:{node.lineno}")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
