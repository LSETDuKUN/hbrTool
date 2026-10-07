# HBR Quest 本地资料库

在挂件底部点 **资料室 ↗**，或双击 `启动资料室.bat`。资料室是独立窗口，不改变挂件的战斗统计或位置。

## 使用

- **查资料**：选服务器、类别，输入名称、ID、角色名或原文关键词，点击搜索。每页 250 条。日服额外支持网站提供的繁体中文参考名；各服务器数值独立保存。
- **数值与说明**：显示 Hit 数、SP、技能等级上限、各效果段的基础威力、上限属性差、DP/HP/破坏倍率、属性权重、成长、逐 Hit 分配和条件。
- **完整 JSON**：保留全部嵌套字段，包括当前尚未解释的字段。
- **SQL 查询**：支持 SQLite 的 SELECT、JOIN、聚合和 JSON 函数；只读连接与授权器禁止修改、附加数据库和加载扩展。每次最多显示 500 行，执行预算 3 秒。
- **来源与覆盖**：显示采集时间、各服务器的数据类别和不可用 URL。
- **同步网站资料**：后台下载新版本。已有未完成下载时续传；网络失败不替换旧资料库。首次完整同步包含上万份文件，需要较长时间。

## 数据存储与后续计算接口

下载源为 https://hbr.quest/ 实际页面模块引用的 https://master.hbr.quest/v1/ 公开 JSON。按日服（jp）、国服（cn）、国际服（en）区分；跟随角色、敌人、调整历史主表引用下载详表，不猜测未披露 ID。覆盖范围是可发现的公开数据接口，不包括图片、音视频，也不声称覆盖服务器未公开或未引用的文件。

`data/quest/` 是本地缓存，不提交 Git。原始 JSON 字节和发现入口的页面脚本以 SHA256 命名保存到 `objects/`；每份来源记录 URL、抓取时间、字节数。原始文件可用于重新建立索引和审计字段。`manifest-*.json`、`catalog-*.sqlite3` 保留历史版本；只有新数据库建立成功才原子替换 `current.json`。下载阶段记录在 `staging.json`。

SQLite 结构版本为 1。数值投影可演进，原始快照不能由投影反向覆盖。SQLite REAL 用于数值检索，精确原始表示以原始 JSON 字节为准。缺字段存 NULL，不擅自填 0；重复技能保留其来源和 JSON Pointer，不凭 ID 覆盖条件变体。

| 表 / 视图 | 用途 |
|---|---|
| `sources` | 来源、区域、数据类别、文件路径、SHA256 |
| `records` | 所有主表和详表的顶层记录，含完整 `raw_json`、中文参考名 |
| `translations` | 本地化类别、字段、标签、语言与文本 |
| `skills` | 从全部来源递归提取的技能及变体，`skill_key = URL#JSONPointer` |
| `skill_parts` | 每个技能的效果段，数值化威力、差值、倍率、六维权重；条件、成长和完整原文同时保留 |
| `skill_hits` | 技能级（`part_index=-1`）及效果段级 Hit，独立保存 `hit_id`、`hit_type`、`power_ratio` |
| `skill_elements` | 按效果段关联的元素 |
| `characters`, `styles`, `enemies` | 对应主表的查询视图 |
| `style_skills` | 风格主表与其技能实例的关联 |
| `skill_catalog`, `skill_damage` | 限定技能主表的技能 / 效果段视图；仍包含表内条件变体 |

`skill_parts.skill_key` → `skills.skill_key`；`skills.source_url` → `sources.url`。使用 `Catalog.skill_variants(region, skill_id)` 取得带来源的所有主表变体，调用者必须显式选择服务器与条件，不默认选任意一份。

## 字段含义与边界

| 来源字段 | SQL 字段 | 含义 |
|---|---|---|
| `hit_count` | `skills.hit_count` | 来源给出的技能 Hit 数 |
| `parts[i].power[0:2]` | `power_min`, `power_max` | 来源威力两端值；仅攻击效果可视为基础威力，治疗/增益有不同含义 |
| `diff_for_max` | `diff_for_max` | 达到上限的属性差；不是已经代入角色和敌人的实际差值 |
| `multipliers.dp/hp/dr` | `dp_multiplier`, `hp_multiplier`, `destruction_multiplier` | DP、HP 与破坏倍率，保留原比例数值 |
| `parameters` | `str/dex/wis/spr/luk/con_weight` | 各属性权重 |
| `hits[].power_ratio` | `skill_hits.power_ratio` | 逐 Hit 威力分配；空列表不等于平均分配 |
| `growth`, `value`, `effect`, `cond` 等 | JSON 字段 / `raw_json` | 保留原结构，未确认的公式不自行解释 |

基础威力不等于实战伤害。技能等级、Buff、敌人参数、特殊条件和逐 Hit 结算仍需计算层明确处理。本轮不将静态技能数据直接用于自动扣 DP/HP，以免错误套用。现有挂件的累计扣条规则保持原行为。

```sql
SELECT region, name, hit_count, part_index,
       power_min, power_max, diff_for_max,
       dp_multiplier, hp_multiplier, destruction_multiplier
FROM skill_damage
WHERE region = 'jp' AND id = '46001102';

SELECT s.name, h.hit_index, h.hit_type, h.power_ratio
FROM skill_catalog s JOIN skill_hits h USING (skill_key)
WHERE s.region = 'jp' AND s.id = '46001106'
ORDER BY h.part_index, h.hit_index;
```

命令行：`F:\python\python.exe -m hbr_data.sync` 全量更新，`--resume` 续传未完成版本。若同步时强制退出进程导致 `sync.lock` 目录残留，先确认没有同步进程，再移除该空目录后续传。旧版本与原始文件不会自动清理。

本次抓取数量与不可用来源见 [DATA_COVERAGE.md](DATA_COVERAGE.md)。
