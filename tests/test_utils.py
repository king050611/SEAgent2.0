from pathlib import Path
import unittest

from src.utils import resolve_path_from_base


class ResolvePathFromBaseTest(unittest.TestCase):
    def test_keeps_absolute_path(self):
        path = Path("/tmp/model")

        self.assertEqual(resolve_path_from_base(path, Path("/workspace/app")), path)

    def test_resolves_relative_path(self):
        self.assertEqual(
            resolve_path_from_base("Qwen3.5-9B", Path("/workspace/app")),
            Path("/workspace/app/Qwen3.5-9B"),
        )


if __name__ == "__main__":
    unittest.main()
