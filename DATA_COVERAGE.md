# HBR Quest 数据覆盖报告

采集开始（UTC）：2026-10-07T09:17:45.818322+00:00
快照：catalog-20261007T122341685843Z.sqlite3

成功 JSON 来源：14900；不可用：14。

## 主表记录数

| 服务器 | 类别 | 记录数 |
|---|---|---:|
| cn | characters | 59 |
| cn | enemies | 3882 |
| cn | skills | 643 |
| cn | styles | 343 |
| en | characters | 59 |
| en | enemies | 3706 |
| en | skills | 601 |
| en | styles | 325 |
| jp | characters | 63 |
| jp | enemies | 6427 |
| jp | skills | 720 |
| jp | styles | 374 |

主表记录数不包含递归展开的条件变体。数值索引包含 75,702 个技能实例、102,539 个效果段、82,404 条逐 Hit 记录，含多来源重复与历史变体，不可当成唯一技能数量。

## 全部已获取数据类别

| 类别 | 文件数 |
|---|---:|
| accessories | 3 |
| arts | 3 |
| arts_battle | 3 |
| arts_items | 3 |
| banners | 3 |
| battle_config | 1 |
| battles | 3 |
| boosters | 3 |
| chapter_names | 1 |
| characters | 3 |
| chips | 3 |
| conquest | 3 |
| cooking | 2 |
| define_values | 1 |
| dimension_battle | 3 |
| enemies | 3 |
| enemy_details | 14015 |
| events | 3 |
| items | 3 |
| latent_abilities | 1 |
| latest | 3 |
| localization | 2 |
| login_bonus | 3 |
| master_skills | 82 |
| mission_types | 1 |
| missions | 3 |
| packs | 3 |
| passives | 3 |
| rework_details | 498 |
| role_abilities | 1 |
| schedule_cn | 1 |
| schedule_en | 1 |
| schedule_hbr | 1 |
| score_attack | 3 |
| skill_names | 1 |
| skill_templates | 1 |
| skill_types | 3 |
| skills | 3 |
| special_status_types | 1 |
| special_statuses | 1 |
| style_details | 181 |
| style_reworks | 1 |
| styles | 3 |
| support_skills | 3 |
| title_badges | 3 |
| translation | 1 |
| tutorial | 3 |
| updated_before | 24 |
| wave_battle | 3 |

## 不可用来源

以下来源均返回 HTTP 404，不填造数据。它们来自网站公开模块引用或区域表映射；不代表所有地区都已发布相应功能。

- https://master.hbr.quest/v1/cn/chapters.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/cn/duel_cards.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/cn/latent_abilities.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/cn/role_abilities.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/cn/style_reworks.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/en/chapters.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/en/duel_cards.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/en/latent_abilities.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/en/role_abilities.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/en/style_reworks.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/chapters.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/cooking.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/duel_cards.json — HTTP Error 404: Not Found
- https://master.hbr.quest/v1/levels.json — HTTP Error 404: Not Found

范围为公开页面模块可发现的 JSON 表与表中引用的角色/敌人/调整详表，不包括图像、音频、视频以及未披露的端点。原始数据和每份文件的来源可查本地 data/quest/current.json。
