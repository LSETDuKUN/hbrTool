# 头像与敌人图标覆盖

数值快照：catalog-20261007T122341685843Z.sqlite3

只使用主表披露的 image 文件名；不下载立绘。头像采用网站 CDN 与备用路径，敌人同时检查 enemy/card 图像类别。
本地去重后共 442 个原始图片文件，其中敌人图片 4 个。

| 服务器 | 类别 | 状态 | 关联记录数 |
|---|---|---|---:|
| cn | characters | ready | 59 |
| cn | enemies | missing | 3810 |
| cn | enemies | ready | 72 |
| cn | styles | ready | 343 |
| en | characters | ready | 59 |
| en | enemies | missing | 3647 |
| en | enemies | ready | 59 |
| en | styles | ready | 325 |
| jp | characters | ready | 63 |
| jp | enemies | missing | 6221 |
| jp | enemies | ready | 206 |
| jp | styles | ready | 374 |

ready 表示图像下载并解码校验成功；missing 表示本次检查的候选路径均返回 404。没有网络错误未重试项。多条敌人记录可共享一张图，关联数不代表独立头像数。

原始文件与 SHA256 保存在 data/quest/portraits/，记录关联、候选 URL 与具体失败原因保存在 data/quest/portraits.json。可通过 AssetStore 接口读取，也可在资料室的来源与覆盖中查看汇总。
