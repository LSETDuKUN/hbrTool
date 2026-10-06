"""识别层的测试。

最强的一条是端到端：拿真实帧读出伤害数字，断言和帧里实际显示的完全一致。
那几条数字是我逐帧目视核对过的。

依赖 frames/ 和 templates.json —— 缺了就跳过，不让整个测试套件挂掉
（这些帧是抓出来的，可能被清掉）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hbr_recog import damage, segment, templates  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FRAMES = ROOT / "frames"
TEMPLATE_FILE = ROOT / "hbr_recog" / "templates.json"

has_frames = FRAMES.is_dir() and any(FRAMES.glob("*.png"))
has_templates = TEMPLATE_FILE.exists()


# ---------------------------------------------------------------- 字形处理


class TestNormalize(unittest.TestCase):
    def test_keeps_aspect_ratio(self):
        """宽字形归一化后应该比高字形宽，不能被拉成一样。

        这正是之前的 bug：无视宽高比拉满到固定尺寸，导致同一个数字
        在 33px 和 45px 两种宽度下笔画粗细不一致，匹配直接崩掉。
        """
        narrow = np.ones((50, 20), dtype=bool)
        wide = np.ones((50, 45), dtype=bool)
        n = segment.normalize(narrow, 32, 32)
        w = segment.normalize(wide, 32, 32)
        self.assertLess(n.sum(), w.sum())

    def test_output_shape(self):
        out = segment.normalize(np.ones((50, 30), dtype=bool), 32, 32)
        self.assertEqual(out.shape, (32, 32))

    def test_empty_input(self):
        out = segment.normalize(np.zeros((0, 0), dtype=bool), 32, 32)
        self.assertEqual(out.shape, (32, 32))
        self.assertFalse(out.any())

    def test_very_wide_is_clamped_to_grid(self):
        out = segment.normalize(np.ones((10, 200), dtype=bool), 32, 32)
        self.assertEqual(out.shape, (32, 32))
        # 至少应该有内容，而不是被裁没了
        self.assertTrue(out.any())


class TestColumnExtents(unittest.TestCase):
    def test_full_column(self):
        mask = np.ones((10, 3), dtype=bool)
        np.testing.assert_array_equal(segment.column_extents(mask), [10, 10, 10])

    def test_empty_column_is_zero(self):
        mask = np.zeros((10, 2), dtype=bool)
        mask[2:6, 0] = True
        np.testing.assert_array_equal(segment.column_extents(mask), [4, 0])


class TestSplitWideRuns(unittest.TestCase):
    def test_narrow_runs_untouched(self):
        band = np.ones((10, 30), dtype=bool)
        runs = [(0, 10), (15, 25)]
        self.assertEqual(segment.split_wide_runs(band, runs, 12.0), runs)

    def test_wide_run_is_split(self):
        # 30 宽当成两个 15 的字
        band = np.ones((10, 32), dtype=bool)
        # 中间挖一条谷，切点应该落在这里
        band[:, 15:17] = False
        out = segment.split_wide_runs(band, [(0, 32)], 16.0)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][0], 0)
        self.assertEqual(out[-1][1], 32)
        # 切点应落在谷附近
        self.assertTrue(13 <= out[0][1] <= 19)


class TestDropSmallComponents(unittest.TestCase):
    def test_removes_attached_fragment(self):
        """实测见过 `7` 左边粘着一小块前一个数字的残笔，导致匹配全崩。"""
        mask = np.zeros((10, 20), dtype=bool)
        mask[:, 5:15] = True        # 主块 100 像素
        mask[0:2, 0:2] = True       # 角落 4 像素的碎片
        cleaned = segment.drop_small_components(mask, min_ratio=0.25)
        self.assertTrue(cleaned[:, 5:15].all())
        self.assertFalse(cleaned[0:2, 0:2].any())

    def test_keeps_similar_sized_components(self):
        """两块差不多大的都该留着 —— 某些字形天然由几块组成。"""
        mask = np.zeros((10, 20), dtype=bool)
        mask[:, 0:5] = True
        mask[:, 10:15] = True
        cleaned = segment.drop_small_components(mask)
        self.assertTrue(cleaned[:, 0:5].any())
        self.assertTrue(cleaned[:, 10:15].any())

    def test_single_component_unchanged(self):
        mask = np.ones((5, 5), dtype=bool)
        np.testing.assert_array_equal(segment.drop_small_components(mask), mask)

    def test_empty_mask(self):
        mask = np.zeros((5, 5), dtype=bool)
        self.assertFalse(segment.drop_small_components(mask).any())


# ---------------------------------------------------------------- 模板


class TestJaccard(unittest.TestCase):
    def test_identical(self):
        a = np.ones((4, 4), dtype=bool)
        self.assertAlmostEqual(templates.jaccard(a, a), 1.0)

    def test_disjoint(self):
        a = np.zeros((4, 4), dtype=bool)
        a[0, 0] = True
        b = np.zeros((4, 4), dtype=bool)
        b[3, 3] = True
        self.assertAlmostEqual(templates.jaccard(a, b), 0.0)

    def test_half_overlap(self):
        a = np.zeros((2, 2), dtype=bool)
        a[0, :] = True
        b = np.zeros((2, 2), dtype=bool)
        b[:, 0] = True
        # 交 1，并 3
        self.assertAlmostEqual(templates.jaccard(a, b), 1 / 3)

    def test_both_empty(self):
        a = np.zeros((2, 2), dtype=bool)
        self.assertAlmostEqual(templates.jaccard(a, a), 0.0)


class TestTemplateStore(unittest.TestCase):
    def setUp(self):
        a = np.zeros((32, 32), dtype=bool)
        a[5:25, 5:10] = True
        b = np.zeros((32, 32), dtype=bool)
        b[5:25, 20:28] = True
        self.store = templates.TemplateStore(
            [templates.Template("A", a), templates.Template("B", b)]
        )

    def test_classify_exact(self):
        self.assertEqual(self.store.classify(self.store.templates[0].bits)[0], "A")
        self.assertEqual(self.store.classify(self.store.templates[1].bits)[0], "B")

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            self.store.save(path)
            loaded = templates.TemplateStore.load(path)
        self.assertEqual(loaded.chars(), ["A", "B"])
        np.testing.assert_array_equal(
            loaded.templates[0].bits, self.store.templates[0].bits
        )

    def test_read_marks_low_confidence_as_question(self):
        glyph = segment.Glyph(0, 0, 1, 1, np.zeros((32, 32), dtype=bool))
        self.assertEqual(self.store.read([glyph], min_score=0.9), "?")

    def test_empty_store_classifies_as_unknown(self):
        empty = templates.TemplateStore()
        char, score = empty.classify(np.ones((32, 32), dtype=bool))
        self.assertEqual(char, "?")
        self.assertEqual(score, 0.0)


class TestClusterGlyphs(unittest.TestCase):
    def test_identical_glyphs_merge(self):
        bits = np.zeros((32, 32), dtype=bool)
        bits[4:28, 8:24] = True
        glyphs = [segment.Glyph(0, 0, 1, 1, bits.copy()) for _ in range(5)]
        clusters = templates.cluster_glyphs(glyphs, threshold=0.7)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]), 5)

    def test_different_glyphs_stay_separate(self):
        a = np.zeros((32, 32), dtype=bool)
        a[:8, :] = True
        b = np.zeros((32, 32), dtype=bool)
        b[:, :8] = True
        glyphs = [
            segment.Glyph(0, 0, 1, 1, a),
            segment.Glyph(0, 0, 1, 1, b),
        ]
        self.assertEqual(len(templates.cluster_glyphs(glyphs, threshold=0.9)), 2)


# ---------------------------------------------------------------- 端到端


class TestScaleAdaptation(unittest.TestCase):
    """窗口尺寸会变（见过 2048x1152 / 1920x1095 / 1365x768），
    坐标和尺寸门槛必须跟着缩放，否则会静默地什么都读不到。"""

    def test_for_frame_scales_bands(self):
        reader = damage.DamageReader()
        half = reader.for_frame(damage.REFERENCE_WIDTH // 2, 576)
        self.assertAlmostEqual(half.scale, 0.5)
        self.assertEqual(half.y_band, damage.scale_band(damage.DEFAULT_Y_BAND, 0.5))
        self.assertEqual(half.x_band, damage.scale_band(damage.DEFAULT_X_BAND, 0.5))

    def test_reference_size_returns_self(self):
        reader = damage.DamageReader()
        self.assertIs(reader.for_frame(damage.REFERENCE_WIDTH, 1152), reader)

    def test_min_row_pixels_never_drops_to_zero(self):
        reader = damage.DamageReader()
        tiny = reader.for_frame(200, 120)
        self.assertGreaterEqual(tiny.min_row_pixels, 1)


class TestLooksLikeDigitScaling(unittest.TestCase):
    @staticmethod
    def glyph(width, height):
        bits = np.zeros((32, 32), dtype=bool)
        return segment.Glyph(0, 0, width, height, bits)

    def test_normal_size_accepted_at_scale_1(self):
        self.assertTrue(damage.looks_like_digit(self.glyph(36, 54), 1.0))

    def test_same_glyph_rejected_when_frame_is_half_size(self):
        """半尺寸下 36x54 相当于参考分辨率里的 72x108，太大了。"""
        self.assertFalse(damage.looks_like_digit(self.glyph(36, 54), 0.5))

    def test_half_size_glyph_accepted_at_half_scale(self):
        self.assertTrue(damage.looks_like_digit(self.glyph(18, 27), 0.5))


class TestSplitByHeight(unittest.TestCase):
    """按高度把字形切成连续段。

    `BREAK!` 覆盖层和伤害数字高度差很明显（实测 67~68 vs 51~53），
    但横向可能挨得不够远，会被归进同一组，然后高度方差检查把整组否决 ——
    连带里面读得好好的数字一起丢掉（实测 `32774` 就是这么丢的）。
    """

    @staticmethod
    def glyph(x, height):
        return segment.Glyph(x, 0, x + 20, height, np.zeros((32, 32), dtype=bool))

    def test_splits_break_from_digits(self):
        glyphs = [self.glyph(x, 67) for x in (0, 50, 100)]
        glyphs += [self.glyph(x, 52) for x in (400, 450, 500)]
        runs = damage.split_by_height(glyphs, tolerance=4.0)
        self.assertEqual(len(runs), 2)
        self.assertEqual([g.height for g in runs[0]], [67, 67, 67])
        self.assertEqual([g.height for g in runs[1]], [52, 52, 52])

    def test_keeps_uniform_run_together(self):
        glyphs = [self.glyph(x, h) for x, h in ((0, 51), (50, 52), (100, 51), (150, 53))]
        self.assertEqual(len(damage.split_by_height(glyphs, tolerance=4.0)), 1)

    def test_empty(self):
        self.assertEqual(damage.split_by_height([]), [])


class TestGlyphGrouping(unittest.TestCase):
    """按列分组切字。这是绕开 `BREAK!` 那类覆盖层的关键。"""

    def test_large_gap_splits_groups(self):
        runs = [(0, 20), (30, 50), (400, 420), (430, 450)]
        groups = segment.group_column_runs(runs, split_gap=70)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0][0], (0, 20))
        self.assertEqual(groups[1][0], (400, 420))

    def test_close_runs_merged(self):
        runs = [(0, 10), (14, 30), (60, 80)]
        merged = segment.merge_close_runs(runs, merge_gap=6)
        self.assertEqual(merged, [(0, 30), (60, 80)])

    def test_weighted_runs_ignore_sparse_columns(self):
        """只有一两个像素的列不该算墨迹 —— 否则背景噪点会把不相干的东西连起来。"""
        mask = np.zeros((20, 100), dtype=bool)
        mask[5:15, 10:30] = True       # 真正的字
        mask[0, 60] = True             # 一列只有一个像素的噪点
        runs = segment.weighted_column_runs(mask, min_col_pixels=3, min_width=3)
        self.assertEqual(runs, [(10, 30)])

    def test_empty_mask(self):
        self.assertEqual(segment.weighted_column_runs(np.zeros((5, 5), dtype=bool)), [])


HARD_FRAMES = ROOT / "results" / "20261006-142335"
has_hard = HARD_FRAMES.is_dir() and has_templates


@unittest.skipUnless(has_hard, "需要 results/20261006-142335/ 里的回归帧")
class TestHardFrames(unittest.TestCase):
    """用户报"漏帧"之后挖出来的硬骨头。

    这些帧的共同点：画面里有巨大的 `BREAK!` 覆盖层和伤害数字**竖直重叠**。
    旧的行投影做法会把它俩粘成一行，然后较矮的数字被覆盖率过滤当成"小字后缀"
    丢掉 —— 4 帧伤害完全读不到。另外短数字（1~3 位）会被 min_run 门槛整行丢掉。

    期望值是我把每一帧放大逐字目视确认过的。
    """

    CASES = {
        "000043": 438986,      # 普通
        "000044": None,        # 资源管理器窗口，不该读出任何东西
        "000045": 32774,       # BREAK! + 5 位数
        "000046": 2032774,
        "000047": 8,           # BREAK! + **1 位数**
        "000048": 448,         # 3 位数
        "000049": 1829591,     # BREAK! + 7 位数（和 50/51 同值，是三重重复的来源）
        "000050": 1829591,
        "000051": 1829591,
        "000052": None,        # 战斗场景，没有伤害数字
        "000053": 4975257,
        "000054": 4975257,
    }

    @classmethod
    def setUpClass(cls):
        cls.reader = damage.DamageReader(min_run=1)

    def test_every_frame_read_exactly(self):
        from PIL import Image

        wrong = []
        for name, expected in self.CASES.items():
            path = HARD_FRAMES / f"{name}.png"
            if not path.exists():
                self.skipTest(f"{name}.png 不在了")
            rgb = np.asarray(Image.open(path).convert("RGB"))
            found = self.reader.read(rgb)
            got = found[0].value if found else None
            if got != expected:
                wrong.append((name, expected, got))
        self.assertEqual(wrong, [], f"读错/读漏: {wrong}")

    def test_no_false_positive_on_break_overlay(self):
        """`BREAK!` 会被读成 `8????` —— 绝不能把它当成伤害值 8。

        只要求"含数字"的话它就会蒙混过关，所以加了"未识别字符比例"的门。
        """
        from PIL import Image

        for name in ("000045", "000047", "000049"):
            path = HARD_FRAMES / f"{name}.png"
            if not path.exists():
                continue
            rgb = np.asarray(Image.open(path).convert("RGB"))
            for read in self.reader.read(rgb):
                self.assertLessEqual(
                    read.unresolved * 4,
                    len(read.text),
                    f"{name} 读出了未识别字符过多的结果: {read.text!r}",
                )


@unittest.skipUnless(has_frames and has_templates, "需要 frames/ 和 templates.json")
class TestReadRealFrames(unittest.TestCase):
    """端到端：拿真实帧读出伤害数字，和帧里实际显示的逐字对答案。

    下列数字是我逐帧放大目视核对过的，不是识别结果反过来当期望值。
    """

    EXPECTED = {
        "000014.png": 349297,
        "000015.png": 349297,
        "000028.png": 1819817,
        "000030.png": 5400615,
        "000033.png": 1765007,
        "000035.png": 5691092,
        "000036.png": 5691092,
    }

    @classmethod
    def setUpClass(cls):
        cls.reader = damage.DamageReader()

    def test_every_known_frame_reads_correctly(self):
        for name, expected in self.EXPECTED.items():
            path = FRAMES / name
            if not path.exists():
                self.skipTest(f"{name} 不在了")
            with self.subTest(frame=name):
                result = self.reader.read_one(path)
                self.assertIsNotNone(result, f"{name} 没读到伤害数字")
                self.assertEqual(
                    result.value, expected,
                    f"{name} 读成 {result.value}，应为 {expected}",
                )

    def test_confidence_is_reasonable(self):
        for name in self.EXPECTED:
            path = FRAMES / name
            if not path.exists():
                self.skipTest(f"{name} 不在了")
            with self.subTest(frame=name):
                result = self.reader.read_one(path)
                self.assertGreater(result.confidence, 0.5)

    def test_non_damage_frames_return_nothing(self):
        """指令阶段的帧不该读出伤害数字 —— 否则会凭空多算。"""
        path = FRAMES / "000005.png"
        if not path.exists():
            self.skipTest("000005.png 不在了")
        self.assertEqual(self.reader.read_file(path), [])

    def test_all_templates_cover_every_digit(self):
        self.assertEqual(set(self.reader.store.chars()), set("0123456789"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
