"""标签识别（合计伤害 / 平均伤害）的测试。

这个字段猜错的代价特别高 —— 把平均值当总和用会差十几倍，
而且数字本身读得没错，很难发现。所以重点测"不确定时会不会硬猜"。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hbr_recog import label  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FRAMES = ROOT / "frames"
LABEL_TEMPLATES = ROOT / "hbr_recog" / "label_templates.json"

has_data = FRAMES.is_dir() and LABEL_TEMPLATES.exists()


class TestMeanFilter(unittest.TestCase):
    def test_constant_image_unchanged(self):
        a = np.full((20, 20), 7.0, dtype=np.float32)
        np.testing.assert_allclose(label._mean_filter(a, 5), a, atol=1e-4)

    def test_kernel_one_is_identity(self):
        a = np.arange(25, dtype=np.float32).reshape(5, 5)
        np.testing.assert_array_equal(label._mean_filter(a, 1), a)

    def test_isolated_spike_is_smoothed(self):
        a = np.zeros((21, 21), dtype=np.float32)
        a[10, 10] = 100.0
        blurred = label._mean_filter(a, 9)
        self.assertGreater(blurred[10, 10], 0)
        self.assertLess(blurred[10, 10], 100)


class TestLabelRegion(unittest.TestCase):
    def test_extracts_relative_to_number_box(self):
        rgb = np.zeros((1152, 2048, 3), dtype=np.uint8)
        box = (1390, 564, 1690, 616)
        region = label.label_region(rgb, box)
        self.assertIsNotNone(region)
        height, width = region.shape[:2]
        self.assertEqual(width, label.ROI_DX1 - label.ROI_DX0)
        self.assertEqual(height, label.ROI_DY1 - label.ROI_DY0)

    def test_clipped_at_frame_edge(self):
        """数字框贴边时裁掉越界部分，而不是崩掉。"""
        rgb = np.zeros((200, 200, 3), dtype=np.uint8)
        region = label.label_region(rgb, (5, 30, 50, 80))
        self.assertIsNotNone(region)
        self.assertLessEqual(region.shape[0], 200)
        self.assertLessEqual(region.shape[1], 200)

    def test_tiny_region_returns_none(self):
        """裁完太小就没意义了，返回 None 让上层走"未知"。"""
        rgb = np.zeros((200, 200, 3), dtype=np.uint8)
        # 数字框在很靠上的位置，标签区落到画面外只剩一两行
        region = label.label_region(rgb, (100, 5, 150, 40))
        self.assertIsNone(region)


class TestRobustBBox(unittest.TestCase):
    def test_ignores_sparse_outlier_pixels(self):
        """一条只有一个像素的"列"不该把包围盒撑出去。

        这正是之前 6/12 错的原因：离群像素把包围盒撑歪几十像素，
        归一化之后整个位图错位。
        """
        mask = np.zeros((40, 200), dtype=bool)
        mask[10:30, 60:140] = True      # 真正的文字块
        mask[0, 0] = True               # 角落的孤立噪点
        mask[39, 199] = True            # 另一个角落
        box = label.robust_bbox(mask)
        self.assertIsNotNone(box)
        y0, y1, x0, x1 = box
        self.assertGreaterEqual(x0, 55)
        self.assertLessEqual(x1, 145)
        self.assertGreaterEqual(y0, 5)
        self.assertLessEqual(y1, 35)

    def test_empty_mask(self):
        self.assertIsNone(label.robust_bbox(np.zeros((10, 10), dtype=bool)))

    def test_all_sparse_returns_none(self):
        mask = np.zeros((10, 10), dtype=bool)
        mask[0, 0] = True
        self.assertIsNone(label.robust_bbox(mask))


class TestLabelBitmap(unittest.TestCase):
    def test_shape_and_empty_input(self):
        region = np.zeros((82, 320, 3), dtype=np.uint8)
        bitmap = label.label_bitmap(region)
        self.assertEqual(bitmap.shape, (label.GRID_H, label.GRID_W))
        self.assertFalse(bitmap.any())

    def test_blue_background_produces_nothing(self):
        """蓝天背景不该被当成文字（粉色判据会挡住它）。"""
        region = np.zeros((82, 320, 3), dtype=np.uint8)
        region[:, :, 2] = 200          # 纯蓝
        self.assertFalse(label.label_bitmap(region).any())

    def test_smooth_pink_area_produces_nothing(self):
        """大片平滑的粉色也不该被当成文字（高通会把平滑区域滤掉）。"""
        region = np.zeros((82, 320, 3), dtype=np.uint8)
        region[:, :, 0] = 200          # 粉
        region[:, :, 1] = 130
        region[:, :, 2] = 170
        self.assertFalse(label.label_bitmap(region).any())

    def test_pink_text_on_blue_is_detected(self):
        region = np.zeros((82, 320, 3), dtype=np.uint8)
        region[:, :, 2] = 180          # 蓝底
        region[30:50, 50:250] = (230, 140, 180)   # 一条粉色"文字"
        self.assertTrue(label.label_bitmap(region).any())


class TestLabelStore(unittest.TestCase):
    @staticmethod
    def make_store():
        a = np.zeros((label.GRID_H, label.GRID_W), dtype=bool)
        a[4:20, 4:30] = True
        b = np.zeros((label.GRID_H, label.GRID_W), dtype=bool)
        b[4:20, 40:68] = True
        return label.LabelStore({"合计": a, "平均": b})

    def test_classify_exact_match(self):
        store = self.make_store()
        self.assertEqual(
            store.classify(store.templates["合计"], min_margin=0.0).name, "合计"
        )
        self.assertEqual(
            store.classify(store.templates["平均"], min_margin=0.0).name, "平均"
        )

    def test_low_margin_reports_unknown_instead_of_guessing(self):
        """类间差不够大时必须说"未知"，不能硬猜。

        实测在已知样本上最小类间差只有 0.02 —— 硬猜迟早出错，
        而错一次的代价是把平均值当总和，差十几倍。
        """
        store = self.make_store()
        ambiguous = np.zeros((label.GRID_H, label.GRID_W), dtype=bool)
        ambiguous[4:20, 20:48] = True       # 正好夹在两者中间
        result = store.classify(ambiguous, min_margin=0.5)
        self.assertEqual(result.name, "未知")

    def test_empty_store_returns_unknown(self):
        self.assertEqual(label.LabelStore().classify(np.ones((24, 72), dtype=bool)).name,
                         "未知")

    def test_save_load_roundtrip(self):
        store = self.make_store()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.json"
            store.save(path)
            loaded = label.LabelStore.load(path)
        self.assertEqual(sorted(loaded.templates), ["合计", "平均"])
        np.testing.assert_array_equal(
            loaded.templates["合计"], store.templates["合计"]
        )

    def test_load_missing_file_is_empty(self):
        self.assertEqual(len(label.LabelStore.load(ROOT / "no_such_file.json")), 0)


@unittest.skipUnless(has_data, "需要 frames/ 和 label_templates.json")
class TestLabelOnRealFrames(unittest.TestCase):
    """在真实帧上回读。标签归属是我逐帧放大目视确认过的。"""

    KNOWN = {
        "合计": ["000014.png", "000030.png", "000035.png", "000036.png"],
        "平均": ["000037.png", "000038.png", "000039.png", "000042.png"],
    }

    @classmethod
    def setUpClass(cls):
        from hbr_recog import damage as damage_mod

        cls.reader = damage_mod.DamageReader()
        cls.store = label.LabelStore.load(LABEL_TEMPLATES)

    def _label_of(self, name):
        path = FRAMES / name
        if not path.exists():
            self.skipTest(f"{name} 不在了")
        from PIL import Image

        array = np.asarray(Image.open(path).convert("RGB"))
        found = self.reader.read(array)
        self.assertTrue(found, f"{name} 读不到伤害数字")
        return self.store.read(array, found[0].box)

    def test_never_misclassifies(self):
        """**宁可返回"未知"，也绝不能把两者搞混。**"""
        wrong = []
        for expected, names in self.KNOWN.items():
            for name in names:
                result = self._label_of(name)
                if result.name == "未知":
                    continue
                if result.name != expected:
                    wrong.append((name, expected, result.name))
        self.assertEqual(wrong, [], f"标签认错: {wrong}")

    def test_most_frames_are_classified(self):
        """大部分帧应该能认出来，不然这个功能没意义。"""
        classified = 0
        total = 0
        for names in self.KNOWN.values():
            for name in names:
                total += 1
                if self._label_of(name).name != "未知":
                    classified += 1
        self.assertGreaterEqual(classified, total // 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
