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
    def test_narrow_windowed_game_gap_fits_outer_widget(self):
        from hbr_capture.widget import compute_panel_width, compute_placement
        rect = (265, 100, 2310, 1300)
        width = compute_panel_width(rect, 250, 2560)
        self.assertEqual(width, 225)
        x, y, overlaps, _ = compute_placement(rect, width, 620, 2560, 1600)
        self.assertFalse(overlaps)
        self.assertLessEqual(x + width + 32, rect[0] - 8)
        self.assertGreaterEqual(x, 0)

    def test_real_frame_event_is_labeled_once_and_saved_with_metadata(self):
        from PIL import Image
        from hbr_capture.capture import Frame
        from hbr_capture.monitor import Monitor, MonitorConfig
        fixture = Path(__file__).resolve().parents[1] / "results/20261006-142335/000045.png"
        with Image.open(fixture) as source:
            image = source.convert("RGBA")
            frame = Frame(image.width, image.height, image.tobytes("raw", "BGRA"),
                          non_black_ratio=1)
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            monitor = Monitor(MonitorConfig(outdir=Path(tmp), damage_trigger=True,
                reject_flash=False, on_damage=events.append, quiet=True))
            reader = monitor._ensure_reader()
            with patch.object(reader.label_store, "read", wraps=reader.label_store.read) as label:
                with patch("hbr_capture.monitor.time.time", return_value=0):
                    monitor._check_damage(frame, 0)
                self.assertEqual(label.call_count, 0)
                with patch("hbr_capture.monitor.time.time", return_value=.35):
                    monitor._check_damage(frame, 0)
                self.assertEqual(label.call_count, 1)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["readings"][0]["value"], 32774)
            record = json.loads((Path(tmp) / "index.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["damage"], events[0]["readings"])

    def event_monitor(self):
        from hbr_capture.monitor import Monitor, MonitorConfig
        events = []
        monitor = Monitor(MonitorConfig(damage_stable_seconds=.3,
            damage_disappear_grace=.2, on_damage=events.append))
        monitor._save = lambda *args, **kwargs: True
        return monitor, events

    def test_stable_replacement_without_empty_frame_is_another_hit(self):
        monitor, events = self.event_monitor()
        for now, value in ((0, 4238), (.31, 4238), (.4, 519), (.71, 519), (2, 519)):
            monitor.observe_damage((value,), len(str(value)), "frame", 0, now)
        self.assertEqual([e["readings"][0]["value"] for e in events], [4238, 519])

    def test_changed_number_after_short_gap_keeps_both_hits(self):
        monitor, events = self.event_monitor()
        monitor.observe_damage((4238,), 4, "frame", 0, 0)
        monitor.observe_no_damage(.1)
        monitor.observe_damage((519,), 3, "frame", 0, .32)
        monitor.observe_damage((519,), 3, "frame", 0, .64)
        self.assertEqual([e["readings"][0]["value"] for e in events], [4238, 519])

    def test_dropout_and_fragment_do_not_repeat_lingering_damage(self):
        monitor, events = self.event_monitor()
        monitor.observe_damage((4238,), 4, "frame", 0, 0)
        monitor.observe_damage((4238,), 4, "frame", 0, .31)
        monitor.observe_damage((238,), 3, "frame", 0, .4)
        monitor.observe_damage((238,), 3, "frame", 0, .72)
        monitor.observe_no_damage(.8)
        monitor.observe_no_damage(1.01)
        monitor.observe_damage((4238,), 4, "frame", 0, 1.1)
        monitor.observe_damage((4238,), 4, "frame", 0, 8)
        self.assertEqual(len(events), 1)

    def test_settle_cannot_save_a_pending_damage_event(self):
        monitor, _ = self.event_monitor()
        monitor.observe_damage((1700918,), 7, "frame", 0, 0)
        self.assertTrue(monitor._damage_already_saved())

    def test_live_sums_distinguish_average_unknown_and_incomplete(self):
        from hbr_capture.widget import Widget
        from unittest.mock import MagicMock
        widget = Widget.__new__(Widget)
        widget.damage_readings = []
        from hbr_capture.damage_stats import DamageLedger
        widget.ledger = DamageLedger(enemies=2)
        widget.reading_sum = widget.total_damage = 0
        widget.txt_damage = MagicMock()
        widget.lbl_damage_sum = MagicMock()
        widget.lbl_total_damage = MagicMock()
        widget._display_damage({"readings": [
            {"value": 100, "label": "合计"},
            {"value": 200, "label": "平均"},
            {"value": 300, "label": "未知"},
            {"value": 40, "label": "合计", "unresolved": 1}]})
        self.assertEqual(len(widget.damage_readings), 4)
        self.assertEqual(widget.reading_sum, 500)
        self.assertEqual(widget.total_damage, 500)
        widget.lbl_damage_sum.config.assert_called_with(text="500")

    def test_unknown_revision_is_counted_once(self):
        from hbr_capture.damage_stats import DamageLedger
        ledger = DamageLedger(enemies=2)
        ledger.add({"run": "test", "event_id": 1, "readings": [
            {"value": 100, "label": "未知"}]})
        self.assertEqual(ledger.total, 0)
        self.assertEqual(ledger.pending, 1)
        known = {"run": "test", "event_id": 1, "readings": [
            {"value": 100, "label": "平均"}]}
        ledger.add(known)
        ledger.add(known)
        self.assertEqual(ledger.total, 200)
        self.assertEqual(len(ledger.readings), 1)
        ledger.add({"run": "test", "event_id": 2, "readings": [
            {"value": 100, "label": "合计"}]})
        self.assertEqual(ledger.total, 300)

    def test_monster_count_recalculates_average_only(self):
        from hbr_capture.damage_stats import DamageLedger
        ledger = DamageLedger()
        ledger.add({"readings": [{"value": 100, "label": "平均"},
            {"value": 50, "label": "合计"}, {"value": 200, "label": "未知"}]})
        ledger.set_enemies("2")
        self.assertEqual(ledger.total, 250)
        ledger.set_enemies("3")
        self.assertEqual(ledger.total, 350)
        for value in ("", "0", "-1", "2.5", "abc", "100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ledger.set_enemies(value)
        self.assertEqual(ledger.enemies, 3)

    def test_unknown_label_recovery_reuses_event_identity(self):
        monitor, events = self.event_monitor()
        monitor.observe_damage((100,), 3, "frame", 0, 0)
        monitor.observe_damage((100,), 3, "frame", 0, .31)
        known = SimpleNamespace(value=100, text="100", label="合计",
            confidence=.9, label_score=.9, unresolved=0)
        monitor._read_damage_frame = lambda *args, **kwargs: [known]
        monitor._retry_unknown_label("clear_frame", (100,), 1)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_id"], events[1]["event_id"])
        self.assertEqual(events[1]["readings"][0]["label"], "合计")
        self.assertEqual(monitor.damage_hits, 1)


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

    def test_archive_includes_damage_count_and_revision_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "frame.png").write_bytes(b"frame")
            (root / "index.jsonl").write_text(json.dumps({
                "run": "runA", "file": "frame.png"}), encoding="utf-8")
            history = root / "session-runA-totals.jsonl"
            history.write_text('{"enemy_count": 2, "total": 100}\n', encoding="utf-8")
            dest = session.archive_run(root, "runA", root / "results")
            self.assertTrue((dest / history.name).exists())
            self.assertFalse(history.exists())

    def test_non_object_index_records_are_preserved_but_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "index.jsonl"
            index.write_text('null\n[]\n42\n{"run": "runA"}\n', encoding="utf-8")
            self.assertEqual(session.run_records(root, "runA"), [{"run": "runA"}])
            self.assertEqual(session.remove_run_from_index(root, "runA"), 1)
            self.assertEqual(index.read_text(encoding="utf-8"), 'null\n[]\n42\n')
