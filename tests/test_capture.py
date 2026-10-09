"""抓屏层里可以脱离游戏窗口验证的部分。

窗口枚举/抓图需要真实窗口，这里不测；测的是编码、比较、解析这些纯逻辑。

跑法（在 hbr/ 目录下）:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hbr_capture import Frame, frame_diff, parse_hotkey  # noqa: E402
from hbr_capture import png as png_mod  # noqa: E402


def decode_png(data: bytes):
    """最小 PNG 解码器，只为验证编码器写出来的东西真的能还原。"""
    assert data[:8] == png_mod.PNG_MAGIC, "PNG magic 不对"
    offset = 8
    idat = b""
    width = height = None
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        tag = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF, f"{tag} 的 CRC 不对"
        if tag == b"IHDR":
            width, height, depth, color_type = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and color_type == 2, "只支持 8bit RGB"
        elif tag == b"IDAT":
            idat += body
        offset += 12 + length

    raw = zlib.decompress(idat)
    stride = width * 3
    rows = []
    for y in range(height):
        base = y * (stride + 1)
        assert raw[base] == 0, f"第 {y} 行的 filter type 不是 0"
        rows.append(raw[base + 1 : base + 1 + stride])
    return width, height, rows


def solid_bgra(width: int, height: int, b, g, r, a=255) -> bytes:
    return bytes([b, g, r, a]) * (width * height)


class TestPngEncoder(unittest.TestCase):
    def test_single_pixel_colour_roundtrip(self):
        # BGRA 输入 -> PNG 里应该是 RGB，注意通道顺序要换过来
        bgra = bytes([0x11, 0x22, 0x33, 0xFF])  # b=0x11 g=0x22 r=0x33
        data = png_mod.encode_png(bgra, 1, 1)
        width, height, rows = decode_png(data)
        self.assertEqual((width, height), (1, 1))
        self.assertEqual(rows[0], bytes([0x33, 0x22, 0x11]))

    def test_multi_row_multi_colour(self):
        w, h = 3, 2
        px = [(1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12), (13, 14, 15), (16, 17, 18)]
        bgra = b"".join(bytes([b, g, r, 255]) for (r, g, b) in px)
        _, _, rows = decode_png(png_mod.encode_png(bgra, w, h))
        expected_row0 = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])
        expected_row1 = bytes([10, 11, 12, 13, 14, 15, 16, 17, 18])
        self.assertEqual(rows[0], expected_row0)
        self.assertEqual(rows[1], expected_row1)

    def test_alpha_is_dropped_not_misread(self):
        bgra = solid_bgra(2, 2, 0xAA, 0xBB, 0xCC, a=0x00)  # alpha=0 也不该影响 RGB
        _, _, rows = decode_png(png_mod.encode_png(bgra, 2, 2))
        self.assertEqual(rows[0], bytes([0xCC, 0xBB, 0xAA]) * 2)

    def test_wrong_buffer_size_rejected(self):
        with self.assertRaises(ValueError):
            png_mod.encode_png(b"\x00" * 10, 2, 2)

    def test_all_compression_levels_decode(self):
        bgra = solid_bgra(8, 4, 1, 2, 3)
        for level in (0, 1, 6, 9):
            with self.subTest(level=level):
                _, _, rows = decode_png(png_mod.encode_png(bgra, 8, 4, level))
                self.assertEqual(rows[0], bytes([3, 2, 1]) * 8)


class TestFrameDiff(unittest.TestCase):
    @staticmethod
    def frame(fill: int, size: int = 64) -> Frame:
        return Frame(
            width=size,
            height=size,
            bgra=bytes([fill, fill, fill, 255]) * (size * size),
        )

    def test_identical_frames_have_zero_diff(self):
        self.assertAlmostEqual(frame_diff(self.frame(10), self.frame(10)), 0.0)

    def test_fully_different_frames_approach_one(self):
        self.assertAlmostEqual(frame_diff(self.frame(0), self.frame(255)), 1.0, places=6)

    def test_half_different_is_about_half(self):
        diff = frame_diff(self.frame(0), self.frame(127))
        self.assertAlmostEqual(diff, 127 / 255, places=2)

    def test_size_mismatch_returns_one(self):
        a = self.frame(0, 64)
        b = Frame(width=32, height=32, bgra=b"\x00" * (32 * 32 * 4))
        self.assertEqual(frame_diff(a, b), 1.0)


class TestParseHotkey(unittest.TestCase):
    def test_function_keys(self):
        self.assertEqual(parse_hotkey("F1"), 0x70)
        self.assertEqual(parse_hotkey("F9"), 0x78)
        self.assertEqual(parse_hotkey("F12"), 0x7B)

    def test_letters_and_digits(self):
        self.assertEqual(parse_hotkey("a"), ord("A"))
        self.assertEqual(parse_hotkey("Z"), ord("Z"))
        self.assertEqual(parse_hotkey("5"), ord("5"))

    def test_named_keys(self):
        self.assertEqual(parse_hotkey("space"), 0x20)
        self.assertEqual(parse_hotkey("ENTER"), 0x0D)
        self.assertEqual(parse_hotkey("NUMPAD0"), 0x60)

    def test_unknown_name_rejected(self):
        with self.assertRaises(ValueError):
            parse_hotkey("MOUSE4")


class TestFrameSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_save_png(self):
        frame = Frame(width=4, height=4, bgra=solid_bgra(4, 4, 9, 8, 7))
        target = self.dir / "a.png"
        written = frame.save(target, fmt="png")
        self.assertTrue(target.exists())
        self.assertEqual(written, target.stat().st_size)
        _, _, rows = decode_png(target.read_bytes())
        self.assertEqual(rows[0], bytes([7, 8, 9]) * 4)

    def test_save_raw_writes_sidecar(self):
        frame = Frame(width=2, height=2, bgra=solid_bgra(2, 2, 1, 2, 3))
        target = self.dir / "b.bgra"
        written = frame.save(target, fmt="raw")
        self.assertTrue(target.exists())
        self.assertTrue((self.dir / "b.bgra.json").exists())
        self.assertEqual(written, 2 * 2 * 4)
        self.assertEqual(target.read_bytes(), frame.bgra)

    def test_save_rejects_unknown_format(self):
        frame = Frame(width=1, height=1, bgra=b"\x00" * 4)
        with self.assertRaises(ValueError):
            frame.save(self.dir / "c.webp", fmt="webp")

    def test_save_creates_missing_directories(self):
        frame = Frame(width=1, height=1, bgra=b"\x00" * 4)
        target = self.dir / "deep" / "nested" / "d.png"
        frame.save(target, fmt="png")
        self.assertTrue(target.exists())


class TestFrameDigest(unittest.TestCase):
    def test_digest_is_deterministic(self):
        a = Frame(width=2, height=2, bgra=b"\x01\x02\x03\x04" * 4)
        b = Frame(width=2, height=2, bgra=b"\x01\x02\x03\x04" * 4)
        self.assertEqual(a.digest(), b.digest())

    def test_different_pixels_differ(self):
        a = Frame(width=2, height=2, bgra=b"\x01\x02\x03\x04" * 4)
        b = Frame(width=2, height=2, bgra=b"\x01\x02\x03\x05" * 4)
        self.assertNotEqual(a.digest(), b.digest())

    def test_non_black_flag(self):
        self.assertFalse(Frame(1, 1, b"\x00" * 4, non_black_ratio=0.0).ok)
        self.assertTrue(Frame(1, 1, b"\xff" * 4, non_black_ratio=0.9).ok)


class TestTargetResolution(unittest.TestCase):
    """窗口定位：hwnd / match / 都不给（自动识别）三种方式。"""

    def test_grabber_defaults_to_auto_detect(self):
        """什么都不传 = 自动识别游戏窗口，不再报错。"""
        from hbr_capture import Grabber

        grabber = Grabber()
        self.assertIsNone(grabber.match)
        self.assertIsNone(grabber.hwnd)

    def test_grabber_accepts_match_only(self):
        from hbr_capture import Grabber

        self.assertEqual(Grabber("foo").match, "foo")

    def test_grabber_accepts_hwnd_only(self):
        from hbr_capture import Grabber

        self.assertIsNone(Grabber(hwnd=0x1234).match)

    def test_parse_hwnd_hex_and_decimal(self):
        from hbr_capture.cli import _parse_hwnd

        self.assertEqual(_parse_hwnd("0x00150692"), 0x150692)
        self.assertEqual(_parse_hwnd("14372"), 14372)

    def test_parse_hwnd_rejects_garbage(self):
        import argparse

        from hbr_capture.cli import _parse_hwnd

        with self.assertRaises(argparse.ArgumentTypeError):
            _parse_hwnd("not-a-handle")


class TestGameWindowScoring(unittest.TestCase):
    """自动识别游戏窗口的打分。

    国服的游戏和启动器**进程名一模一样**（都是 HeavenBurnsRed.exe），
    所以类名是唯一可靠的判据 —— 这条逻辑错了就会去抓启动器。
    """

    @staticmethod
    def make(title="", class_name="", process="", client=(1920, 1080), hwnd=1):
        from hbr_capture.win32 import WindowInfo

        return WindowInfo(
            hwnd=hwnd,
            title=title,
            class_name=class_name,
            pid=1,
            process=process,
            rect=(0, 0, client[0], client[1]),
            client_size=client,
            client_origin=(0, 0),
            minimized=False,
        )

    def test_game_beats_launcher_despite_same_process(self):
        from hbr_capture.win32 import score_game_window

        game = self.make(
            title="HeavenBurnsRed",
            class_name="UnityWndClass",
            process=r"E:\HeavenBurnsRed\HeavenBurnsRed Game\HeavenBurnsRed.exe",
            client=(2048, 1152),
        )
        launcher = self.make(
            title="炽焰天穹",
            class_name="CGameLauncherWnd",
            process=r"E:\HeavenBurnsRed\HeavenBurnsRed.exe",
            client=(1920, 1095),
        )
        self.assertGreater(score_game_window(game), score_game_window(launcher))

    def test_unrelated_window_scores_zero(self):
        from hbr_capture.win32 import score_game_window

        notepad = self.make(title="记事本", class_name="Notepad", process="notepad.exe")
        self.assertEqual(score_game_window(notepad), 0)

    def test_tiny_window_rejected(self):
        from hbr_capture.win32 import score_game_window

        dialog = self.make(
            title="bilibili游戏 防沉迷提示",
            class_name="CAntiAddictionDlg",
            process=r"E:\HeavenBurnsRed\HeavenBurnsRed.exe",
            client=(360, 320),
        )
        self.assertEqual(score_game_window(dialog), 0)

    def test_bigger_client_wins_on_ties(self):
        from hbr_capture.win32 import score_game_window

        small = self.make(class_name="UnityWndClass", client=(800, 600))
        big = self.make(class_name="UnityWndClass", client=(2048, 1152))
        self.assertGreater(score_game_window(big), score_game_window(small))


class TestMonitorSequenceResume(unittest.TestCase):
    """回归: 重跑 watch 时不能从 #0001 重新编号 —— 那会覆盖上一次的图。

    用户实际踩过这个坑：index.jsonl 里留下两条 seq 都是 1 的记录，
    而第二次的 000001.png 把第一次的覆盖了。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _monitor(self):
        from hbr_capture.monitor import Monitor, MonitorConfig

        return Monitor(MonitorConfig(match="x", outdir=self.dir, quiet=True))

    def test_no_index_starts_from_zero(self):
        monitor = self._monitor()
        monitor._resume_sequence()
        self.assertEqual(monitor.seq, 0)

    def test_continues_from_max_seq(self):
        (self.dir / "index.jsonl").write_text(
            '{"seq": 5, "file": "000005.png"}\n{"seq": 9, "file": "000009.png"}\n',
            encoding="utf-8",
        )
        monitor = self._monitor()
        monitor._resume_sequence()
        self.assertEqual(monitor.seq, 9)

    def test_tolerates_corrupt_and_empty_lines(self):
        (self.dir / "index.jsonl").write_text(
            '{"seq": 3}\n\n不是 JSON\n{"seq": 7}\n{"seq": "bad"}\n',
            encoding="utf-8",
        )
        monitor = self._monitor()
        monitor._resume_sequence()
        self.assertEqual(monitor.seq, 7)

    def test_run_id_is_set_and_stable(self):
        monitor = self._monitor()
        self.assertTrue(monitor.run_id)
        self.assertEqual(monitor.run_id, monitor.run_id)


class TestBgraToPpm(unittest.TestCase):
    """挂件预览用的 BGRA -> PPM 转换。tkinter 只认原始 PPM，不认 base64。"""

    @staticmethod
    def frame(width, height, pixels):
        """pixels: [(r, g, b), ...] 按行优先。"""
        data = b"".join(bytes([b, g, r, 255]) for (r, g, b) in pixels)
        return Frame(width=width, height=height, bgra=data)

    def test_header_and_no_scaling(self):
        from hbr_capture.widget import bgra_to_ppm

        frame = self.frame(2, 1, [(1, 2, 3), (4, 5, 6)])
        ppm, w, h = bgra_to_ppm(frame, 2)
        self.assertEqual((w, h), (2, 1))
        self.assertTrue(ppm.startswith(b"P6\n2 1\n255\n"))
        # PPM 里是 RGB 顺序，且 alpha 被丢掉
        self.assertEqual(ppm[len(b"P6\n2 1\n255\n"):], bytes([1, 2, 3, 4, 5, 6]))

    def test_downscale_picks_every_nth_pixel(self):
        from hbr_capture.widget import bgra_to_ppm

        # 4 宽，目标 2 宽 -> step = 2，应取第 0、2 列
        frame = self.frame(4, 1, [(10, 0, 0), (20, 0, 0), (30, 0, 0), (40, 0, 0)])
        ppm, w, h = bgra_to_ppm(frame, 2)
        self.assertEqual((w, h), (2, 1))
        self.assertEqual(ppm[len(b"P6\n2 1\n255\n"):], bytes([10, 0, 0, 30, 0, 0]))

    def test_target_wider_than_source_does_not_upscale(self):
        from hbr_capture.widget import bgra_to_ppm

        frame = self.frame(2, 1, [(1, 1, 1), (2, 2, 2)])
        ppm, w, h = bgra_to_ppm(frame, 999)
        self.assertEqual((w, h), (2, 1))

    def test_zero_target_width_is_clamped(self):
        from hbr_capture.widget import bgra_to_ppm

        frame = self.frame(2, 2, [(9, 9, 9)] * 4)
        ppm, w, h = bgra_to_ppm(frame, 0)
        self.assertGreaterEqual(w, 1)
        self.assertGreaterEqual(h, 1)
        self.assertTrue(ppm.startswith(b"P6\n"))


class TestSessionArchive(unittest.TestCase):
    """会话归档/清理。这段会搬动和删除用户的文件，所以要测得严一点。

    最关键的一条：归档一次会话时，**绝不能碰别的会话的帧**。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outdir = Path(self.tmp.name) / "frames"
        self.outdir.mkdir()
        self.results = Path(self.tmp.name) / "results"

    def _write_index(self, records):
        lines = [json.dumps(r, ensure_ascii=False) for r in records]
        (self.outdir / "index.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def _make_frames(self, records):
        for r in records:
            (self.outdir / r["file"]).write_bytes(b"png-ish")

    @staticmethod
    def _record(seq, run, name):
        return {"seq": seq, "run": run, "file": name, "bytes": 100}

    def test_read_index_missing_file(self):
        from hbr_capture.session import read_index

        self.assertEqual(read_index(self.outdir), [])

    def test_read_index_skips_corrupt_lines(self):
        from hbr_capture.session import read_index

        (self.outdir / "index.jsonl").write_text(
            '{"seq": 1}\n\nnot json\n{"seq": 2}\n', encoding="utf-8"
        )
        self.assertEqual([r["seq"] for r in read_index(self.outdir)], [1, 2])

    def test_run_records_filters(self):
        from hbr_capture.session import run_records

        self._write_index([
            self._record(1, "runA", "000001.png"),
            self._record(2, "runB", "000002.png"),
            self._record(3, "runA", "000003.png"),
        ])
        self.assertEqual(
            [r["file"] for r in run_records(self.outdir, "runA")],
            ["000001.png", "000003.png"],
        )

    def test_archive_moves_only_this_run(self):
        from hbr_capture.session import archive_run, read_index

        records = [
            self._record(1, "runA", "000001.png"),
            self._record(2, "runB", "000002.png"),
            self._record(3, "runA", "000003.png"),
        ]
        self._write_index(records)
        self._make_frames(records)

        dest = archive_run(self.outdir, "runA", results_root=self.results)

        self.assertEqual(dest, self.results / "runA")
        self.assertTrue((dest / "000001.png").exists())
        self.assertTrue((dest / "000003.png").exists())
        self.assertFalse((dest / "000002.png").exists())
        # 别的会话原封不动
        self.assertTrue((self.outdir / "000002.png").exists())
        self.assertFalse((self.outdir / "000001.png").exists())
        # 主 index 里只剩别的会话
        self.assertEqual([r["run"] for r in read_index(self.outdir)], ["runB"])
        # 归档目录里有自己的 index（本次会话有 2 帧，所以是 2 行）
        archived_lines = [
            json.loads(line)
            for line in (dest / "index.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(
            [r["file"] for r in archived_lines], ["000001.png", "000003.png"]
        )

    def test_archive_moves_sidecar_and_metadata(self):
        from hbr_capture.session import archive_run

        records = [self._record(1, "runA", "000001.bgra")]
        self._write_index(records)
        self._make_frames(records)
        (self.outdir / "000001.bgra.json").write_text("{}", encoding="utf-8")
        (self.outdir / "session-runA.log").write_text("log", encoding="utf-8")
        (self.outdir / "session-runA.json").write_text("{}", encoding="utf-8")
        # 别的会话的元文件不该被带走
        (self.outdir / "session-runB.log").write_text("other", encoding="utf-8")

        dest = archive_run(self.outdir, "runA", results_root=self.results)

        self.assertTrue((dest / "000001.bgra").exists())
        self.assertTrue((dest / "000001.bgra.json").exists())
        self.assertTrue((dest / "session-runA.log").exists())
        self.assertTrue((dest / "session-runA.json").exists())
        self.assertTrue((self.outdir / "session-runB.log").exists())

    def test_archive_without_records_returns_none(self):
        from hbr_capture.session import archive_run

        self._write_index([self._record(1, "runA", "000001.png")])
        self.assertIsNone(archive_run(self.outdir, "nope", results_root=self.results))
        self.assertFalse(self.results.exists())

    def test_discard_deletes_only_this_run(self):
        """删除走回收站。测试里把 recycle 换成假的，免得污染真实回收站。"""
        from hbr_capture import session

        records = [
            self._record(1, "runA", "000001.png"),
            self._record(2, "runB", "000002.png"),
        ]
        self._write_index(records)
        self._make_frames(records)

        recycled = []

        def fake_recycle(paths):
            for p in paths:
                Path(p).unlink()
            recycled.append(sorted(Path(p).name for p in paths))
            return True

        original = session.recycle
        session.recycle = fake_recycle
        try:
            removed = session.discard_run(self.outdir, "runA")
        finally:
            session.recycle = original

        self.assertEqual(removed, 1)
        self.assertEqual(recycled, [["000001.png"]])
        self.assertFalse((self.outdir / "000001.png").exists())
        self.assertTrue((self.outdir / "000002.png").exists())
        self.assertEqual(
            [r["run"] for r in session.read_index(self.outdir)], ["runB"]
        )

    def test_discard_refuses_to_fall_back_to_permanent_delete(self):
        """回收站失败时必须抛异常并保留文件，绝不能退化成永久删除。"""
        from hbr_capture import session

        records = [self._record(1, "runA", "000001.png")]
        self._write_index(records)
        self._make_frames(records)

        original = session.recycle
        session.recycle = lambda paths: False
        try:
            with self.assertRaises(RuntimeError):
                session.discard_run(self.outdir, "runA")
        finally:
            session.recycle = original

        # 文件还在，index 也没被动
        self.assertTrue((self.outdir / "000001.png").exists())
        self.assertEqual([r["run"] for r in session.read_index(self.outdir)], ["runA"])

    def test_discard_empty_run_is_noop(self):
        from hbr_capture import session

        self._write_index([self._record(1, "runA", "000001.png")])
        self.assertEqual(session.discard_run(self.outdir, "nope"), 0)

    def test_current_run_id(self):
        from hbr_capture.session import current_run_id

        self._write_index([
            self._record(1, "runA", "000001.png"),
            self._record(2, "runB", "000002.png"),
        ])
        self.assertEqual(current_run_id(self.outdir), "runB")

    def test_current_run_id_empty(self):
        from hbr_capture.session import current_run_id

        self.assertIsNone(current_run_id(self.outdir))


class TestWidgetPlacement(unittest.TestCase):
    """挂件摆位。

    这段踩过坑：不留边框余量的话，窗口右边缘会伸出屏幕外面
    （实测屏幕 2560，窗口右边缘到了 2572）。
    """

    SCREEN = (2560, 1600)
    # 游戏 2048x1152 摆在 (192, 199)，右边只剩约 320px
    GAME = (192, 199, 2240, 1351)

    def test_lands_right_of_game_and_fits_on_screen(self):
        from hbr_capture.widget import compute_placement

        x, y, overlaps, spare = compute_placement(
            self.GAME, 280, 960, *self.SCREEN
        )
        self.assertFalse(overlaps)
        self.assertGreater(x, self.GAME[2])          # 在游戏右边
        self.assertLessEqual(x + 280 + 16, self.SCREEN[0])   # 含边框也不出屏

    def test_reports_remaining_space(self):
        from hbr_capture.widget import compute_placement

        _, _, _, spare = compute_placement(self.GAME, 280, 960, *self.SCREEN)
        self.assertEqual(spare, 2560 - 2240 - 280 - 8)

    def test_falls_back_to_left_side_when_no_room_right(self):
        from hbr_capture.widget import compute_placement

        # 游戏贴着屏幕右边缘，右边一点空间都没有
        game = (900, 100, 2500, 1400)
        x, _, overlaps, _ = compute_placement(game, 280, 960, *self.SCREEN)
        self.assertLess(x + 280, game[0])            # 摆到了左边
        self.assertFalse(overlaps)

    def test_overlap_detected_when_neither_side_fits(self):
        from hbr_capture.widget import compute_placement

        # 游戏几乎占满整屏，两边都放不下
        game = (0, 0, 2560, 1600)
        _, _, overlaps, _ = compute_placement(game, 280, 960, *self.SCREEN)
        self.assertTrue(overlaps)

    def test_y_is_clamped_into_screen(self):
        from hbr_capture.widget import compute_placement

        game = (192, 900, 2240, 1900)                 # 游戏超出屏幕下边
        _, y, _, _ = compute_placement(game, 280, 960, *self.SCREEN)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(y + 960 + 16, 1600)

    def test_border_allowance_prevents_off_screen_window(self):
        """反证：不带边框余量的话这个宽度就会出屏。"""
        from hbr_capture.widget import compute_placement

        # 右边只剩 296px，放 280 宽 + 8 边距刚好，但再加边框必须回退
        game = (192, 199, 2264, 1351)
        x, _, _, _ = compute_placement(game, 280, 960, *self.SCREEN, border=16)
        self.assertLessEqual(x + 280 + 16, 2560)

    def test_minimized_game_does_not_throw_widget_off_screen(self):
        """回归：游戏最小化时 Windows 返回 (-32000, -32000, ...)。

        照着这个坐标算 `x = right + margin` 会把挂件扔到屏幕外几千像素 ——
        表现就是"挂件启动后直接消失，但进程还活着"。
        """
        from hbr_capture.widget import compute_placement

        minimized = (-32000, -32000, -31840, -31960)
        x, y, overlaps, _ = compute_placement(minimized, 280, 960, *self.SCREEN)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + 280 + 16, 2560)
        self.assertLessEqual(y + 960 + 16, 1600)
        self.assertFalse(overlaps)

    def test_result_always_inside_screen(self):
        """不管游戏窗口在哪（包括各种畸形坐标），结果都必须落在屏幕内。"""
        from hbr_capture.widget import compute_placement

        weird = [
            (-32000, -32000, -31840, -31960),      # 最小化
            (-10000, 200, -9000, 1000),            # 在另一侧很远
            (2560, 0, 4000, 800),                  # 完全在主屏右边
            (0, 0, 100, 100),                      # 很小的窗口
            (2400, 1500, 2560, 1600),              # 右下角
        ]
        for rect in weird:
            with self.subTest(rect=rect):
                x, y, _, _ = compute_placement(rect, 280, 960, *self.SCREEN)
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + 280 + 16, 2560)
                self.assertLessEqual(y + 960 + 16, 1600)


class TestWidgetSmoke(unittest.TestCase):
    """挂件的集成冒烟测试：真的建窗口、真的开始/停止。

    这两个 bug 都是它该抓住的：
      * 挂件传了 MonitorConfig 里不存在的字段 -> 一点「开始」就 TypeError，
        而 pythonw 下错误被吞掉，表现是"双击没反应"
      * 游戏窗口最小化时按 (-32000,-32000) 算落点 -> 窗口被扔到屏幕外，
        表现是"挂件启动后直接消失"
    """

    def setUp(self):
        # 必须在建 Tk 之前设 DPI 感知，否则 winfo_screenwidth() 给的是逻辑尺寸(1707)
        # 而窗口位置是 Tk 单位，两者对不上，断言就没意义了。
        try:
            from hbr_capture import win32

            win32.set_dpi_aware()
        except Exception:
            pass
        try:
            import tkinter as tk

            probe = tk.Tk()
            probe.destroy()
        except Exception as exc:
            self.skipTest(f"没有可用的显示: {exc}")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _pump(self, widget, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                widget.root.update()
            except Exception:
                break
            time.sleep(0.05)

    def test_start_stop_start_keeps_window_on_screen(self):
        from hbr_capture.widget import Widget, WidgetConfig

        config = WidgetConfig(
            outdir=Path(self.tmp.name) / "frames",
            width=280,
            height=400,          # 矮一点，别在测试机上占太多地方
            ask_keep=False,      # 否则停止时会弹模态框，测试卡死
            preview=False,
            topmost=False,
            damage_trigger=False,
        )
        widget = Widget(config)
        self.addCleanup(lambda: self._safe_destroy(widget))

        widget.start()
        self._pump(widget, 2.0)

        # 窗口必须还在，而且落在屏幕内
        self.assertTrue(widget.root.winfo_exists())
        screen_w = widget.root.winfo_screenwidth()
        screen_h = widget.root.winfo_screenheight()
        # 用 _physical_bounds 拿物理像素，才能和 winfo_screen* 比
        x, y, w, h = widget._physical_bounds()
        self.assertGreaterEqual(x, -50, f"窗口被放到屏幕左边太远: x={x}")
        self.assertGreaterEqual(y, -50, f"窗口被放到屏幕上边太远: y={y}")
        self.assertLess(x, screen_w, f"窗口跑到屏幕右边外面: x={x}")
        self.assertLess(y, screen_h, f"窗口跑到屏幕下边外面: y={y}")

        # 停止，再重新开始 —— 用户报的就是这一步之后挂件不见了
        widget.stop()
        self._pump(widget, 1.5)
        self.assertTrue(widget.root.winfo_exists(), "停止后窗口就没了")

        widget.start()
        self._pump(widget, 1.5)
        self.assertTrue(widget.root.winfo_exists(), "重新开始后窗口就没了")
        x2, y2, _, _ = widget._physical_bounds()
        self.assertGreaterEqual(x2, -50, f"重开之后窗口跑到屏幕外: x={x2}")
        self.assertLess(x2, screen_w, f"重开之后窗口跑到屏幕外: x={x2}")
        self.assertLess(y2, screen_h, f"重开之后窗口跑到屏幕外: y={y2}")

    @staticmethod
    def _safe_destroy(widget):
        try:
            widget._want_running = False
            widget._stop.set()
            if widget.thread is not None:
                widget.thread.join(timeout=5)
            widget.root.destroy()
        except Exception:
            pass


class TestDamageEventBatching(unittest.TestCase):
    """伤害事件攒批。

    用户报的"重复了很多"来自两件事叠在一起：
      * 伤害数字出场会在屏幕上**滚动**（实测 495→501）
      * 同一个数字切分时全时缺（同一个 `4238` 读出过 `428` / `238`）
    所以不能看到就存，要攒成一次"事件"、定稿后只存一帧，
    并保留**位数最多**的那次读数（位数少说明切分残缺，不是数字真的变短）。
    """

    def _monitor(self, stable=0.8, grace=0.6):
        from hbr_capture.monitor import Monitor, MonitorConfig

        monitor = Monitor(
            MonitorConfig(
                outdir="_event_test",
                damage_stable_seconds=stable,
                damage_disappear_grace=grace,
            )
        )
        monitor.saved = []
        monitor._save = lambda frame, trigger, diff, dedup=True: monitor.saved.append(
            (trigger, frame)
        )
        return monitor

    @staticmethod
    def _digits(key):
        return sum(len(str(v)) for v in key)

    def _observe(self, monitor, key, frame, now):
        monitor.observe_damage(key, self._digits(key), frame, 0.1, now)

    def test_unstable_segmentation_saves_once_with_full_value(self):
        """同一个 4238 被切成 428 / 4238 / 238 —— 只该存一帧，而且是完整的那个。"""
        monitor = self._monitor(stable=0.8)
        sequence = [
            (0.0, (428,), "a"),
            (0.2, (4238,), "b"),
            (0.4, (238,), "c"),
            (0.6, (4238,), "d"),
            # 之后稳定下来（要连续稳定 damage_stable_seconds 才定稿）
            (0.8, (4238,), "e"),
            (1.0, (4238,), "f"),
            (1.2, (4238,), "g"),
            (1.4, (4238,), "h"),
            (1.6, (4238,), "i"),
        ]
        for now, key, tag in sequence:
            self._observe(monitor, key, tag, now)
        self.assertEqual(len(monitor.saved), 1, f"存了 {len(monitor.saved)} 次")
        self.assertEqual(monitor._saved_damage_key, (4238,))
        # 位数最多的是 b/d 那两帧，不能存 c（只有 238）
        self.assertIn(monitor.saved[0][1], ("b", "d", "e", "f", "g", "h", "i"))
        self.assertNotIn(monitor.saved[0][1], ("a", "c"))

    def test_rolling_number_saves_once_with_final_value(self):
        """数字滚动（495 → 501）—— 存最后定稿的那个值。"""
        monitor = self._monitor(stable=0.8)
        for now, key, tag in [
            (0.0, (495,), "f495a"),
            (0.2, (495,), "f495b"),
            (0.4, (501,), "f501a"),
            (0.6, (501,), "f501b"),
            (0.8, (501,), "f501c"),
            (1.0, (501,), "f501d"),
            (1.2, (501,), "f501e"),
            (1.4, (501,), "f501f"),
            (1.6, (501,), "f501g"),
            (1.8, (501,), "f501h"),
        ]:
            self._observe(monitor, key, tag, now)
        self.assertEqual(len(monitor.saved), 1)
        self.assertEqual(monitor._saved_damage_key, (501,))
        # 存的必须是稳定之后的帧，不能是滚动中途的 495
        self.assertTrue(
            monitor.saved[0][1].startswith("f501"),
            f"存的是滚动中途的帧: {monitor.saved[0][1]}",
        )

    def test_never_flushes_while_still_rolling(self):
        """一直在滚动就一直在等，不要中途存下半成品。"""
        monitor = self._monitor(stable=0.8)
        for step in range(30):
            now = step * 0.2
            self._observe(monitor, (490 + step,), f"roll{step}", now)
        self.assertEqual(len(monitor.saved), 0, "滚动过程中不该存")
        # 停下来之后才定稿
        for extra in range(1, 8):
            self._observe(monitor, (519,), f"stop{extra}", 6.0 + extra * 0.2)
        self.assertEqual(len(monitor.saved), 1)
        self.assertEqual(monitor._saved_damage_key, (519,))

    def test_long_lingering_number_after_flush_is_not_saved_again(self):
        """定稿之后数字还挂在屏幕上一段时间 —— 不该再存。"""
        monitor = self._monitor(stable=0.8)
        for step in range(10):
            self._observe(monitor, (4238,), f"f{step}", step * 0.2)
        self.assertEqual(len(monitor.saved), 1)
        # 继续观察同样的数值 4 秒
        for step in range(1, 20):
            self._observe(monitor, (4238,), f"more{step}", 2.0 + step * 0.2)
        self.assertEqual(len(monitor.saved), 1, "定稿后不该重复存")

    def test_new_event_after_disappearing_is_saved(self):
        """数字消失一会儿之后再出现 —— 是新的一次命中，要存。"""
        monitor = self._monitor(stable=0.8, grace=0.6)
        for step in range(10):
            self._observe(monitor, (4238,), f"first{step}", step * 0.2)
        self.assertEqual(len(monitor.saved), 1)

        # 数字消失
        for now in (2.2, 2.4, 2.6, 3.0, 3.4):
            monitor.observe_no_damage(now)

        # 再次出现同样的数值
        for step in range(10):
            self._observe(monitor, (4238,), f"second{step}", 4.0 + step * 0.2)
        self.assertEqual(len(monitor.saved), 2, "第二次命中应该另存一帧")

    def test_brief_flicker_does_not_split_the_event(self):
        """特效一闪造成的"假消失"不该把一次命中拆成两次。"""
        monitor = self._monitor(stable=0.8, grace=0.6)
        for step in range(10):
            self._observe(monitor, (4238,), f"f{step}", step * 0.2)
        self.assertEqual(len(monitor.saved), 1)

        monitor.observe_no_damage(2.2)      # 只闪掉一帧（0.2 秒）
        for step in range(4):
            self._observe(monitor, (4238,), f"g{step}", 2.4 + step * 0.2)
        self.assertEqual(len(monitor.saved), 1, "闪一下不该多存一张")

    def test_number_vanishing_without_stabilising_is_still_saved(self):
        """数字出现后很快就消失（没等到稳定）—— 消失也要把缓冲那帧存下来。"""
        monitor = self._monitor(stable=5.0, grace=0.6)
        self._observe(monitor, (448,), "onlyframe", 0.0)
        self.assertEqual(len(monitor.saved), 0)
        monitor.observe_no_damage(0.2)
        monitor.observe_no_damage(0.4)
        self.assertEqual(len(monitor.saved), 0)
        monitor.observe_no_damage(0.9)      # 超过 grace
        self.assertEqual(len(monitor.saved), 1)
        self.assertEqual(monitor.saved[0][1], "onlyframe")


if __name__ == "__main__":
    unittest.main(verbosity=2)
