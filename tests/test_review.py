import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from hbr_capture import session
from tools import read_frames


class ReviewRegressionTests(unittest.TestCase):
    def test_font_dpi_does_not_rescale_window_coordinates(self):
        from hbr_capture.widget import Widget
        widget = Widget.__new__(Widget)
        widget.root = SimpleNamespace(winfo_fpixels=lambda unit: 144)
        self.assertEqual(widget._dpi_factor(), 1.0)

    def test_exports_only_resolved_total_damage(self):
        def reading(value, label, unresolved=0):
            return SimpleNamespace(value=value, text=str(value), label=label,
                label_score=0.9, is_total=label == "合计", confidence=0.9,
                box=(0, 0, 10, 10), unresolved=unresolved)
        found = [reading(100, "合计"), reading(200, "平均"),
                 reading(300, "未知"), reading(40, "合计", 1)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "frame.png").touch()
            with patch.object(read_frames.dmg, "DamageReader") as reader:
                reader.return_value.read_file.return_value = found
                with contextlib.redirect_stdout(io.StringIO()):
                    result = read_frames.main(["--frames", tmp, "--json",
                        str(root / "readings.json"), "--emit-battle",
                        str(root / "battle.json")])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads((root / "readings.json").read_text(
                encoding="utf-8"))["total"], 100)
            battle = json.loads((root / "battle.json").read_text(encoding="utf-8"))
            self.assertEqual([a["damage"] for a in battle["turns"][0]["actions"]], [100])

    def test_invalid_index_paths_leave_all_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.png").touch()
            index = root / "index.jsonl"
            for name in ("", "../outside.png", str(root / "ok.png")):
                with self.subTest(name=name):
                    content = json.dumps({"run": "runA", "file": name})
                    index.write_text(content, encoding="utf-8")
                    with patch.object(session, "recycle") as recycle:
                        with self.assertRaises(ValueError):
                            session.discard_run(root, "runA")
                        recycle.assert_not_called()
                    with self.assertRaises(ValueError):
                        session.archive_run(root, "runA", root / "results")
                    self.assertEqual(index.read_text(encoding="utf-8"), content)
                    self.assertTrue((root / "ok.png").exists())

    def test_existing_archive_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "frame.png").write_bytes(b"new")
            (root / "index.jsonl").write_text(json.dumps({
                "run": "runA", "file": "frame.png"}), encoding="utf-8")
            dest = root / "results" / "runA"
            dest.mkdir(parents=True)
            (dest / "frame.png").write_bytes(b"old")
            with self.assertRaises(FileExistsError):
                session.archive_run(root, "runA", root / "results")
            self.assertEqual((dest / "frame.png").read_bytes(), b"old")
            self.assertEqual((root / "frame.png").read_bytes(), b"new")

    def test_non_object_index_records_are_preserved_but_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "index.jsonl"
            index.write_text('null\n[]\n42\n{"run": "runA"}\n', encoding="utf-8")
            self.assertEqual(session.run_records(root, "runA"), [{"run": "runA"}])
            self.assertEqual(session.remove_run_from_index(root, "runA"), 1)
            self.assertEqual(index.read_text(encoding="utf-8"), 'null\n[]\n42\n')
