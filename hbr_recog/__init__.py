"""HBR 帧识别层。

建立在 `hbr_capture` 拿到的帧之上，负责"看懂画面"：
  1. 二值化 + 切分（segment.py）
  2. 模板匹配认字（templates.py）
  3. 业务层：认伤害数字、OD 条、DP 值……（后续）

这一层**依赖 numpy 和 Pillow**（capture 层则是零依赖）——
图像处理手写不现实，而且这两个在目标环境里都有。
"""
