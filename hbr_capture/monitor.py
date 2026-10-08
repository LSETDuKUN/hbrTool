"""实时盯着游戏窗口，按需/按事件自动截帧存盘。

三种触发方式可以同时开:

  hotkey    按一下热键立刻抓一张**当前**的帧（不是缓冲帧），存下来。
            这样你不用 alt-tab，见到伤害帧就敲一下。

  settle    画面变化超过阈值后，等它「停下来」再存一张。
            这解决的是"动画播到一半截到糊图"的问题 —— 存的是每个稳定画面的
            最终形态。开着它挂机打，战斗界面/结算界面/伤害帧会被自动抓下来。

  interval  每隔 N 秒存一张，用于留底。

存盘的同时往 outdir/index.jsonl 追加一行元数据，崩溃了也不会丢。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import win32
from .capture import Frame, Grabber, frame_diff


@dataclass
class MonitorConfig:
    match: Optional[str] = None
    hwnd: Optional[int] = None
    outdir: Path = Path("frames")
    fps: float = 5.0
    method: str = "auto"
    hotkey_name: Optional[str] = "F9"
    on_change: Optional[float] = 0.02
    settle: float = 0.4
    interval: Optional[float] = None
    duration: Optional[float] = None
    max_frames: Optional[int] = None
    fmt: str = "png"
    png_level: int = 1
    dedup: bool = True
    min_client: int = 200
    quiet: bool = False
    minimize_console: Optional[bool] = None   # None = 只在控制台确实挡住游戏时才最小化
    stop_hotkey_name: Optional[str] = "F10"
    # 给 GUI 挂件用的回调。on_frame 每抓一帧调一次，on_log 每条日志调一次。
    on_frame: Optional[Callable] = None
    on_log: Optional[Callable] = None
    on_damage: Optional[Callable] = None
    # 外部（GUI）请求停止。返回 True 就收工。
    should_stop: Optional[Callable] = None
    should_pause: Optional[Callable] = None

    # ---- 内容触发：认出伤害数字就立刻存 ----
    # settle 的哲学是"等画面稳定"，而伤害数字恰好出现在动画过程中 ——
    # 实测 48 帧 settle 里只有 1 帧是伤害帧。所以要用识别结果直接触发。
    damage_trigger: bool = False
    event_pipeline: bool = False      # Widget enables the visual-event pipeline.
    event_fps: float = 30.0
    damage_recheck: int = 1            # 每 N 帧识别一次（1 = 每帧，实测 4ms 扛得住）
    #: 同一个数值"连续可见"多久之内算同一次命中。
    #: 注意用的是**上次看见**的时间，不是上次存盘的时间 ——
    #: 用存盘时间的话，动画长的伤害数字停留超过这个值就会被重复存。
    damage_gap_seconds: float = 2.0
    damage_min_run: int = 1            # 1 位也读（小额伤害真的存在），靠短串置信度门挡误报
    #: 一次"伤害事件"要连续可见这么久、数值也不变，才算定稿可以存。
    #: 伤害数字出场时会在屏幕上**滚动**（实测 495→501），中途还会切分不全
    #: （同一个 4238 读出过 428 / 238），所以要等它稳下来。
    damage_stable_seconds: float = 0.3
    #: 数字从画面上消失多久之后，才把缓冲里那一帧存下来。
    #: 留个余量是为了扛住特效一闪造成的"假消失"。
    damage_disappear_grace: float = 0.2
    # A brief OCR dropout of the same lingering value is not a new hit.
    damage_same_value_grace: float = 0.6
    reject_flash: bool = True          # 全白闪帧不要
    #: 提高到 0.85（原来是 0.55）。改成内容触发之后，闪白帧只有"恰好认出
    #: 伤害数字"才会被存 —— 也就是说它上面确实有可读的数字，不该丢。
    #: 阈值太低反而会把技能特效下的正常伤害帧误杀。
    flash_bright_ratio: float = 0.85
    #: 连续抓帧失败多少次才放弃。给得比较宽松是因为
    #: "游戏窗口最小化了"这种情况很常见，应该等它恢复而不是退出。
    max_grab_failures: int = 300


class Monitor:
    def __init__(self, config: MonitorConfig):
        self.cfg = config
        self.seq = 0
        self.counts: Dict[str, int] = {}
        self.written: List[Path] = []
        self.skipped_duplicates = 0
        self._last_digest: Optional[str] = None
        self._log_handle = None
        self._console_hwnd: Optional[int] = None
        self.run_id = time.strftime("%Y%m%d-%H%M%S")
        # 挂件的「抓一帧」按钮会从 UI 线程调用 _save，而主循环在抓图线程里也调，
        # 两个线程同时改 seq / 追加 index.jsonl 会撞车，所以加锁。
        self._save_lock = threading.Lock()
        self._peak_diff = 0.0
        # 内容触发用
        self._reader = None
        self._band = None
        self._last_damage_key = None
        self._last_damage_seen = 0.0
        self._current_damage_key = None    # 本帧看到的伤害数值（可能是重复）
        self._saved_damage_key = None      # 上一次真正存下来的伤害数值

        # ---- 伤害事件缓冲 ----
        # 伤害数字出场时会滚动、切分也会时全时缺（同一个 4238 读出过 428/238）。
        # 所以不看到就存，而是攒成一次"事件"，等它定稿了只存一帧。
        self._event_frame = None       # 缓冲里"最完整"的那一帧
        self._event_key = None         # 对应读数
        self._event_digits = 0         # 对应总位数（越多越完整）
        self._event_diff = 0.0
        self._event_since = 0.0        # 当前数值第一次出现的时间（算稳定性）
        self._event_last_seen = 0.0
        self._event_flushed = False
        self._missing_since = None     # 数字从哪一刻开始看不见了
        self._damage_visible = False   # 上一帧看到数字了吗（判"新的一次命中"靠它）
        self._replacement_key = None
        self._replacement_since = 0.0
        self._confirmed_readings = []
        self._event_serial = 0
        self._label_retry_at = 0.0

        self.damage_hits = 0
        self.flash_skipped = 0
        self.duplicate_damage_skipped = 0
        self.settle_skipped_same_damage = 0

    # ------------------------------------------------------------ 内部

    def _say(self, message: str) -> None:
        """同时写到屏幕、session.log 和 on_log 回调。

        最小化控制台之后就看不到屏幕输出了，所以日志落盘是必须的。
        GUI 挂件靠 on_log 拿到这些行。
        """
        line = str(message)
        if self._log_handle is not None:
            try:
                self._log_handle.write(line + "\n")
                self._log_handle.flush()
            except Exception:
                pass
        if not self.cfg.quiet:
            try:
                print(line, flush=True)
            except Exception:
                # pythonw.exe 启动时没有 stdout
                pass
        if self.cfg.on_log is not None:
            try:
                self.cfg.on_log(line)
            except Exception:
                pass

    def _report_occlusion(self, info) -> None:
        """说清楚到底是谁挡住了游戏，而不是只报一个百分比。

        返回遮挡比例，调用方据此决定要不要硬拦。
        """
        occluders = win32.occluding_windows(info.hwnd)
        others = [o for o in occluders if not o["self"] and o["fraction"] >= 0.02]
        if not others:
            self._say("  遮挡检查: 游戏完整可见 ✅")
            return 0.0

        console = win32.console_window()
        total = sum(o["fraction"] for o in others)
        self._say("  !! 有窗口压在游戏上面，被挡住的区域会抓到遮挡物：")
        for entry in others[:4]:
            name = Path(entry["process"]).name if entry["process"] else "?"
            title = entry["title"] or "(无标题)"
            if len(title) > 34:
                title = title[:32] + "..."
            tag = "   <-- 就是本工具的控制台" if entry["hwnd"] == console else ""
            self._say(f"       {entry['fraction']:5.1%}  {name}  {title!r}{tag}")
        return min(total, 1.0)

    def _resume_sequence(self) -> None:
        """接着已有 index.jsonl 的编号往下走。

        不加这一步的话，第二次跑 watch 会从 #0001 重新开始，
        **直接覆盖上一次的图**，而 index 里会留下两条 seq 相同的记录。
        """
        index = self.cfg.outdir / "index.jsonl"
        if not index.exists():
            return
        max_seq = 0
        try:
            for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    max_seq = max(max_seq, int(record.get("seq", 0)))
                except (TypeError, ValueError):
                    continue
        except OSError:
            return
        if max_seq:
            self.seq = max_seq
            self._say(
                f"目录里已有 {max_seq} 帧，从 #{max_seq + 1} 继续编号（不会覆盖旧图）。"
            )

    def _ensure_live_capture(self, grabber) -> None:
        """自检：当前抓图方法拿到的是活画面，还是冻结帧？

        只看 non_black_ratio 是分辨不出来的 —— 冻结帧也是"非全黑"。
        判定办法：
          同一方法连抓两次
            不同 -> 活画面，直接用
            相同 -> 可能只是画面静止，也可能冻住了。再用 screendc 抓两次对照：
                      screendc 在变 -> 画面其实在动，是当前方法冻结 -> 自动换 screendc
                      screendc 也不变 -> 画面是真的静止，当前方法没问题
        """
        method = grabber.method
        if method == "screendc":
            return
        gap = 1.0
        try:
            first = grabber.grab()
            time.sleep(gap)
            second = grabber.grab()
        except Exception as exc:
            self._say(f"  抓图自检跳过（抓不到图）: {exc}")
            return

        if first.bgra != second.bgra:
            self._say(f"  抓图自检: {grabber.method} 拿到的是活画面 ✅")
            return

        info = grabber.info
        try:
            third = win32.capture_window(info.hwnd, "screendc")
            time.sleep(gap)
            fourth = win32.capture_window(info.hwnd, "screendc")
        except Exception as exc:
            self._say(
                f"  抓图自检: {grabber.method} 两帧完全相同，"
                f"但 screendc 对照不可用（{exc}），无法判定是静止还是冻结。"
            )
            return

        if third.bgra != fourth.bgra:
            self._say(
                f"  !! 抓图自检: {grabber.method} 拿到的是**冻结帧** —— 画面其实在变。\n"
                f"     这个文件在 Unity/DirectX 窗口上很常见。已自动切换到 screendc。"
            )
            grabber.method = "screendc"
        else:
            self._say(f"  抓图自检: 画面确实静止，{grabber.method} 正常 ✅")

    def _write_session(self, info, method_used: str) -> None:
        cfg = self.cfg
        # 按会话命名，这样归档/删除时能整批带走（固定名会被下一次覆盖）
        (cfg.outdir / f"session-{self.run_id}.json").write_text(
            json.dumps(
                {
                    "run": self.run_id,
                    "match": cfg.match,
                    "hwnd": hex(cfg.hwnd) if cfg.hwnd else None,
                    "title": info.title,
                    "process": info.process,
                    "client_size": list(info.client_size),
                    "method_requested": cfg.method,
                    "method_used": method_used,
                    "fps": cfg.fps,
                    "event_pipeline": cfg.event_pipeline,
                    "event_fps": cfg.event_fps,
                    "hotkey": cfg.hotkey_name,
                    "on_change": cfg.on_change,
                    "settle": cfg.settle,
                    "interval": cfg.interval,
                    "format": cfg.fmt,
                    "png_level": cfg.png_level,
                    "dedup": cfg.dedup,
                    "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _save(self, frame: Frame, trigger: str, diff: float, dedup: bool = False) -> bool:
        """存一帧。dedup=True 时，如果和上一张存过的帧完全一样就跳过。

        返回是否真的落了盘。热键触发永远不跳过 —— 你按了就是想要这一帧。

        index 里同时记两个变化量:
          diff      存盘那一刻的画面变化（settle 帧这里几乎总是接近 0，信息量低）
          peak_diff 从上一张存盘到现在，画面变化最大到过多少
                    —— 这个才说明「这次场景切换有多剧烈」，筛图时更有用
        """
        with self._save_lock:
            digest = frame.digest()
            if dedup and self.cfg.dedup and digest == self._last_digest:
                self.skipped_duplicates += 1
                return False

            self.seq += 1
            self.counts[trigger] = self.counts.get(trigger, 0) + 1

            ext = ".png" if self.cfg.fmt == "png" else ".bgra"
            name = f"{self.seq:06d}{ext}"
            path = self.cfg.outdir / name
            size = frame.save(path, fmt=self.cfg.fmt, level=self.cfg.png_level)
            self.written.append(path)
            self._last_digest = digest
            peak = max(self._peak_diff, diff)
            self._peak_diff = 0.0

            record = {
                "seq": self.seq,
                "run": self.run_id,
                "file": name,
                "trigger": trigger,
                "ts": round(frame.timestamp, 3),
                "time": time.strftime("%H:%M:%S", time.localtime(frame.timestamp)),
                "width": frame.width,
                "height": frame.height,
                "method": frame.method,
                "non_black_ratio": round(frame.non_black_ratio, 4),
                "diff": round(diff, 5),
                "peak_diff": round(peak, 5),
                "digest": digest,
                "bytes": size,
                "broken": not frame.ok,
            }
            if trigger == "damage":
                record["damage"] = self._confirmed_readings
                record["damage_event_id"] = self._event_serial
            with (self.cfg.outdir / "index.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

            flag = "" if frame.ok else "  [注意: 画面接近全黑]"
            self._say(
                f"  #{self.seq:04d} [{trigger:<8}] {name}  "
                f"{frame.width}x{frame.height}  peak={peak:.3f}{flag}"
            )
            return True

    # ------------------------------------------------------------ 内容触发

    def _bright_ratio(self, frame: Frame, threshold: int = 200) -> float:
        """抽样估算"很亮"的像素占比。全白闪帧这个值会非常高。"""
        view = memoryview(frame.bgra)
        step = 997 * 4
        total = 0
        lit = 0
        for offset in range(0, len(view) - 4, step):
            total += 1
            if (
                view[offset] >= threshold
                and view[offset + 1] >= threshold
                and view[offset + 2] >= threshold
            ):
                lit += 1
        return lit / total if total else 0.0

    def _ensure_reader(self):
        if self._reader is None:
            from hbr_recog import damage as damage_mod

            self._reader = damage_mod.DamageReader(min_run=self.cfg.damage_min_run)
        return self._reader

    def _damage_already_saved(self) -> bool:
        """当前画面上的伤害数字是不是刚存过。

        `settle` 触发**完全不看内容** —— 只要画面稳下来就存。而动画长的伤害数字
        会在屏幕上停留好几秒，画面一稳 settle 就把它又存一遍。
        实测结果里同一个 `1829591` 被存了 3 张（damage 一张 + settle 两张）。
        """
        return (
            self._damage_visible
            or self._current_damage_key is not None
        )

    def _begin_event(self, key, digits: int, frame: Frame, diff: float, now: float) -> None:
        self._event_serial += 1
        self._event_key = key
        self._event_digits = digits
        self._event_frame = frame
        self._event_diff = diff
        self._event_since = now
        self._event_last_seen = now
        self._event_flushed = False
        self._missing_since = None
        self._replacement_key = None

    def _flush_event(self, now: float) -> None:
        """把缓冲的这一次伤害事件存下来（只存一帧）。"""
        frame = self._event_frame
        key = self._event_key
        if frame is None or key is None:
            return

        readings = [{"value": v, "text": str(v), "label": "未知",
                     "confidence": 0.0, "unresolved": 0} for v in key]
        if self.cfg.damage_trigger:
            try:
                found = self._read_damage_frame(frame, recognize_labels=True)
                recognized = [dict(value=r.value, text=r.text, label=r.label,
                    confidence=r.confidence, label_score=r.label_score,
                    unresolved=r.unresolved) for r in found]
                if recognized:
                    readings = recognized
            except Exception as exc:
                self._say(f"伤害标签读取失败，保留未知读数: {exc}")
        self._confirmed_readings = readings

        self._event_flushed = True
        self._event_frame = None          # 释放掉，别再占内存
        self._last_damage_key = key
        self._last_damage_seen = now
        self._saved_damage_key = key
        self.damage_hits += 1

        text = ", ".join(f"{value:,}" for value in key)
        self._say(f"      >> 伤害 {text}")
        if self._save(frame, "damage", self._event_diff, dedup=False) is not False:
            if self.cfg.on_damage is not None:
                self.cfg.on_damage({"run": self.run_id, "file": f"{self.seq:06d}.png",
                    "event_id": self._event_serial, "readings": readings})
        self._label_retry_at = now + 0.5

    def _retry_unknown_label(self, frame, key, now):
        if (not self._event_flushed or key != self._event_key
                or now < self._label_retry_at or not self._confirmed_readings
                or all(r["label"] != "未知" and not r.get("unresolved", 0)
                       for r in self._confirmed_readings)):
            return
        self._label_retry_at = now + 0.5
        try:
            found = self._read_damage_frame(frame, recognize_labels=True)
        except Exception as exc:
            self._say(f"遮挡后标签复核暂不可用: {exc}")
            return
        if tuple(r.value for r in found) != key:
            return
        if not any(r.label in ("合计", "平均") and not r.unresolved for r in found):
            return
        self._confirmed_readings = [dict(value=r.value, text=r.text, label=r.label,
            confidence=r.confidence, label_score=r.label_score,
            unresolved=r.unresolved) for r in found]
        self._say("      >> 遮挡后标签已确认，更新原伤害记录")
        if self.cfg.on_damage is not None:
            self.cfg.on_damage({"run": self.run_id, "event_id": self._event_serial,
                "update": True, "readings": self._confirmed_readings})

    def _read_damage_frame(self, frame, recognize_labels=False):
        import numpy as np
        reader = self._ensure_reader().for_frame(frame.width, frame.height)
        (y0, y1), (x0, x1) = reader.y_band, reader.x_band
        if frame.height < y1 or frame.width < x1:
            return []
        arr = np.frombuffer(frame.bgra, dtype=np.uint8).reshape(frame.height, frame.width, 4)
        return reader.read_band(arr[y0:y1, x0:x1, 2::-1].copy(),
                                recognize_labels=recognize_labels)

    def _check_damage(self, frame: Frame, diff: float) -> None:
        """看这一帧有没有伤害数字；有的话攒成"伤害事件"，定稿后只存一帧。

        为什么不看到就存：伤害数字出场时会**滚动**（实测 495→501），
        而且切分时全时缺（同一个 `4238` 读出过 `428` / `238`）。
        看到就存的话，同一串数字会被存好几张，读数还不一样 ——
        用户报的"重复了很多"就是这个。

        所以攒成事件：
          * 数值变了 = 还在滚动，重新计时，并**保留位数最多的那次读数**
            （位数少说明切分残缺，不是真的数字变短）
          * 数值稳住 `damage_stable_seconds` = 定稿，存
          * 数字从画面上消失 `damage_disappear_grace` 秒 = 也定稿，存缓冲里那帧
        """
        cfg = self.cfg
        try:
            import numpy as np
        except ImportError:
            return

        found = self._read_damage_frame(frame)
        now = time.time()

        if not found:
            self._current_damage_key = None
            self.observe_no_damage(now)
            return

        # 闪白帧上的数字通常是特效残留，存下来没意义
        if cfg.reject_flash:
            ratio = self._bright_ratio(frame)
            if ratio >= cfg.flash_bright_ratio:
                self.flash_skipped += 1
                return

        key = tuple(r.value for r in found)
        digits = sum(len(str(value)) for value in key)
        self._current_damage_key = key
        self.observe_damage(key, digits, frame, diff, now)
        self._retry_unknown_label(frame, key, now)

    def observe_damage(self, key, digits: int, frame: Frame, diff: float, now: float) -> None:
        """看到一个伤害读数：推进事件状态机，必要时定稿存盘。

        抽出来是为了能单测 —— 这一段的判据踩过好几种坑
        （滚动、切分残缺、特效一闪造成的假消失），值得有回归测试兜着。

        消失后重现或定稿后出现不同的稳定数字，都可以开启新事件。
        同一数字持续挂着、短暂漏识别及残缺切分不应重复入账。
        """
        if (self._missing_since is not None
                and now - self._missing_since >= self.cfg.damage_disappear_grace):
            self.observe_no_damage(now)

        if (not self._damage_visible and self._event_flushed
                and key == self._event_key
                and now - self._event_last_seen < self.cfg.damage_same_value_grace):
            self._damage_visible = True

        # A different stable number after a completed event is the next hit,
        # even when no empty frame was sampled between the two numbers.
        if self._damage_visible and self._event_flushed:
            fragment = (len(key) == len(self._event_key) and all(
                str(v) in str(old) for v, old in zip(key, self._event_key)))
            if key == self._event_key or fragment:
                self._replacement_key = None
            else:
                if key != self._replacement_key:
                    self._replacement_key = key
                    self._replacement_since = now
                elif now - self._replacement_since >= self.cfg.damage_stable_seconds:
                    self._begin_event(key, digits, frame, diff, self._replacement_since)

        if not self._damage_visible:
            # 数字刚出现 —— 新的一次命中
            self._begin_event(key, digits, frame, diff, now)
        elif not self._event_flushed:
            # 同一次命中还挂在屏幕上：可能还在滚动、也可能这次切分得更完整
            if key != self._event_key:
                self._event_since = now        # 数值变了，稳定性重新计时
            if digits >= self._event_digits:
                self._event_digits = digits
                self._event_frame = frame
                self._event_key = key
                self._event_diff = diff
        self._damage_visible = True
        self._missing_since = None
        self._event_last_seen = now

        if (
            not self._event_flushed
            and (now - self._event_since) >= self.cfg.damage_stable_seconds
        ):
            self._flush_event(now)

    def observe_no_damage(self, now: float) -> None:
        """这一帧没有伤害数字。

        不立刻收尾：特效一闪会造成"假消失"，所以先记下时刻，
        超过 `damage_disappear_grace` 还没回来才算真的消失、把缓冲那帧定稿。
        """
        if not self._damage_visible:
            return
        if self._missing_since is None:
            self._missing_since = now
            return
        if now - self._missing_since < self.cfg.damage_disappear_grace:
            return

        if not self._event_flushed:
            self._flush_event(now)
        self._damage_visible = False
        self._missing_since = None

    # ------------------------------------------------------------ 主循环

    def run(self) -> dict:
        cfg = self.cfg
        grabber = Grabber(
            cfg.match, hwnd=cfg.hwnd, method=cfg.method, min_client=cfg.min_client
        )

        info = grabber.info
        self._say(f"目标窗口: {info}")
        if info.minimized:
            self._say("  警告: 窗口当前是最小化的，抓到的大概率是黑屏。")
        if info.title_bar_height:
            self._say(
                f"  提示: 客户区相对窗口顶部偏移 {info.title_bar_height}px，"
                "抓图取的是客户区，坐标规范不受影响。"
            )

        cfg.outdir.mkdir(parents=True, exist_ok=True)
        self._resume_sequence()
        # 日志按会话分开，这样归档/删除时能整批带走
        self._log_path = cfg.outdir / f"session-{self.run_id}.log"
        self._log_handle = self._log_path.open("a", encoding="utf-8")

        hotkey_vk = win32.parse_hotkey(cfg.hotkey_name) if cfg.hotkey_name else None
        stop_vk = (
            win32.parse_hotkey(cfg.stop_hotkey_name) if cfg.stop_hotkey_name else None
        )

        self._say("开始抓图自检（约 2 秒）...")
        self._ensure_live_capture(grabber)
        if cfg.damage_trigger and cfg.event_pipeline:
            # Direct regional capture always uses visible screen pixels.
            grabber.method = 'screendc'

        minimized_console = None
        if grabber.method == "screendc":
            if not win32.is_foreground(info.hwnd):
                self._say("  提示: 游戏当前不是前台窗口。screendc 要求它可见且没被盖住。")
            occluded = self._report_occlusion(info)

            if occluded >= 0.9:
                # 全被挡住时 screendc 抓到的基本是遮挡物本身，存下来毫无意义。
                # 与其静默生成一文件夹垃圾，不如直接停下来。
                self._say("")
                self._say(
                    "  [中止] 游戏窗口有 {:.0%} 被挡住，screendc 现在只能抓到遮挡物。\n"
                    "         请把游戏切到前台、或把挡路的窗口移开再重跑。".format(occluded)
                )
                self._cleanup(None)
                raise RuntimeError(
                    "游戏窗口被完全遮挡，无法抓取有效画面"
                )

            console = win32.console_window()
            occluders = [o for o in win32.occluding_windows(info.hwnd) if not o["self"]]
            console_blocks = console is not None and any(
                o["hwnd"] == console and o["fraction"] >= 0.02 for o in occluders
            )
            want_minimize = (
                cfg.minimize_console
                if cfg.minimize_console is not None
                else console_blocks
            )
            if want_minimize and console is not None and console_blocks:
                minimized_console = win32.minimize_console()
                if minimized_console:
                    self._say(
                        "  已临时最小化本工具的控制台窗口，避免它挡住游戏。"
                        "结束后会自动还原。"
                    )
                    self._say(
                        "  停止方式: 按 [{}] 或 Ctrl+C（控制台在任务栏上）。".format(
                            cfg.stop_hotkey_name or "Ctrl+C"
                        )
                    )

        self._write_session(info, grabber.method)

        if cfg.damage_trigger and cfg.event_pipeline:
            try:
                from .event_monitor import EventMonitor
                return EventMonitor(self, grabber).run(hotkey_vk, stop_vk)
            finally:
                self._cleanup(minimized_console)

        self._say(
            "监视中"
            + (f"，热键 [{cfg.hotkey_name}] 抓当前帧" if cfg.hotkey_name else "")
            + (f"，[{cfg.stop_hotkey_name}] 停止" if cfg.stop_hotkey_name else "")
            + (f"，画面稳定后自动存" if cfg.on_change is not None else "")
            + f"。抓图方式 [{grabber.method}]。Ctrl+C 结束。"
        )
        self._say(f"完整日志: {self._log_path.resolve()}")

        period = 1.0 / cfg.fps if cfg.fps > 0 else 0.0
        deadline = time.time() + cfg.duration if cfg.duration else None
        next_interval = time.time() + cfg.interval if cfg.interval else None

        prev: Optional[Frame] = None
        prev_key_down = False
        last_change = time.time()
        scene_saved = False
        started = time.time()
        grabbed = 0
        failed = 0
        stopped_by_limit = False
        stopped_by_user = False

        try:
            while True:
                loop_start = time.time()
                if deadline and loop_start >= deadline:
                    self._say("\n到达 --duration 时限，收工。")
                    stopped_by_limit = True
                    break
                if cfg.max_frames and self.seq >= cfg.max_frames:
                    self._say(f"\n已存满 {cfg.max_frames} 帧，收工。")
                    stopped_by_limit = True
                    break

                # 停止热键 —— 控制台被最小化时按这个，不用去点任务栏
                if stop_vk is not None and win32.key_down(stop_vk):
                    stopped_by_user = True
                    self._say(f"\n按下 [{cfg.stop_hotkey_name}]，收工。")
                    break

                # 外部（GUI）请求停止
                if cfg.should_stop is not None and cfg.should_stop():
                    stopped_by_user = True
                    self._say("\n收到停止请求，收工。")
                    break

                if cfg.should_pause and cfg.should_pause():
                    time.sleep(.05)
                    continue

                try:
                    frame = grabber.grab()
                    grabbed += 1
                except Exception as exc:
                    failed += 1
                    message = str(exc)
                    # 窗口最小化时客户区是 0x0，这是**可恢复**的 ——
                    # 玩家切出去干别的事很常见，应该等它回来，而不是放弃退出。
                    minimized = "0x0" in message or "最小化" in message
                    if failed <= 3 or failed % 100 == 0:
                        hint = "  （游戏窗口最小化了？我在这儿等它回来）" if minimized else ""
                        self._say(f"  抓帧失败({failed}): {exc}{hint}")
                    if not minimized and failed > cfg.max_grab_failures:
                        self._say(
                            f"连续抓帧失败超过 {cfg.max_grab_failures} 次，放弃。"
                        )
                        break
                    time.sleep(max(period, 0.5))
                    continue

                if prev is None:
                    diff = 1.0
                else:
                    diff = frame_diff(prev, frame)
                if diff > self._peak_diff:
                    self._peak_diff = diff

                # ---- 触发 0: 内容触发（认出伤害数字就立刻存）
                if cfg.damage_trigger and grabbed % max(1, cfg.damage_recheck) == 0:
                    self._check_damage(frame, diff)

                # ---- 触发 1: 热键
                if hotkey_vk is not None:
                    down = win32.key_down(hotkey_vk)
                    if down and not prev_key_down:
                        try:
                            fresh = grabber.grab()
                        except Exception:
                            fresh = frame
                        # 热键永远不跳过：你按了就是想要这一帧
                        self._save(fresh, "hotkey", diff)
                    prev_key_down = down

                # ---- 触发 2: 画面稳定
                if cfg.on_change is not None:
                    if diff > cfg.on_change:
                        last_change = loop_start
                        scene_saved = False
                    elif (
                        not scene_saved
                        and prev is not None
                        and (loop_start - last_change) >= cfg.settle
                    ):
                        # 画面稳下来时如果伤害数字还挂着、而且刚存过，
                        # 这一帧就是已经存过的那张，别重复存。
                        if self._damage_already_saved():
                            self.settle_skipped_same_damage += 1
                        else:
                            self._save(frame, "settle", diff, dedup=True)
                        scene_saved = True

                # ---- 触发 3: 定时
                if next_interval is not None and loop_start >= next_interval:
                    self._save(frame, "interval", diff, dedup=True)
                    next_interval = loop_start + cfg.interval

                # ---- 给 GUI 的实时回调
                if cfg.on_frame is not None:
                    try:
                        cfg.on_frame(
                            frame,
                            {
                                "diff": diff,
                                "peak_diff": self._peak_diff,
                                "grabbed": grabbed,
                                "saved": self.seq,
                                "skipped": self.skipped_duplicates,
                                "damage_hits": self.damage_hits,
                                "flash_skipped": self.flash_skipped,
                                "duplicate_damage_skipped": self.duplicate_damage_skipped,
                                "settle_skipped_same_damage": self.settle_skipped_same_damage,
                                "failed": failed,
                                "method": grabber.method,
                                "elapsed": loop_start - started,
                                "broken": not frame.ok,
                            },
                        )
                    except Exception:
                        pass

                prev = frame

                elapsed = time.time() - loop_start
                if period > elapsed:
                    time.sleep(period - elapsed)

        except KeyboardInterrupt:
            stopped_by_user = True
            self._say("\n收到 Ctrl+C，收工。")
        except Exception:
            # 出意外也要把控制台还回来，不能让它一直最小化着
            self._cleanup(minimized_console)
            raise

        if cfg.damage_trigger and self._event_frame is not None and not self._event_flushed:
            self._flush_event(time.time())
        total_time = time.time() - started
        summary = {
            "saved": self.seq,
            "by_trigger": dict(self.counts),
            "skipped_duplicates": self.skipped_duplicates,
            "damage_hits": self.damage_hits,
            "flash_skipped": self.flash_skipped,
            "grabbed": grabbed,
            "failed": failed,
            "seconds": round(total_time, 2),
            "fps_actual": round(grabbed / total_time, 2) if total_time else 0.0,
            "outdir": str(cfg.outdir),
            "stopped_by_limit": stopped_by_limit,
            "stopped_by_user": stopped_by_user,
        }
        self._say(
            "\n汇总: 存了 {saved} 帧 {by_trigger}，跳过 {skipped_duplicates} 张重复图，"
            "抓帧 {grabbed} 次，实际 {fps_actual} fps，用时 {seconds}s".format(**summary)
        )
        if cfg.damage_trigger:
            self._say(
                f"      内容触发命中 {self.damage_hits} 次，"
                f"同一数值重复跳过 {self.duplicate_damage_skipped} 次，"
                f"闪白帧丢弃 {self.flash_skipped} 次"
            )
            if self.settle_skipped_same_damage:
                self._say(
                    f"      画面稳定时因伤害数字已存过而跳过 "
                    f"{self.settle_skipped_same_damage} 次"
                )
        self._say(f"输出目录: {cfg.outdir.resolve()}")
        self._cleanup(minimized_console)
        return summary

    def _cleanup(self, minimized_console) -> None:
        """把控制台还回来、把日志关掉。无论正常结束还是异常都要走到。"""
        if minimized_console:
            win32.restore_console(minimized_console)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
