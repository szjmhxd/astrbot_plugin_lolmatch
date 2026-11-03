# 数据来源

插件对外展示的每一个字段，都能在这份文档里找到出处。分四类：赛事数据、英雄名称、工作日日历、本地缓存。

## 总览

| 数据 | 来源 | 鉴权 | 失败时 |
|---|---|---|---|
| 赛程、赛果、逐局统计 | 玩加赛事 scoregg（主） | 无 | 自动切 PandaScore |
| 同上（兜底） | PandaScore（备） | API Key | 两个都挂则提示原因 |
| 英雄中文名 | Riot Data Dragon `zh_CN` | 无 | 退回英文 ID |
| 源站英雄编号 → 英雄名 | 本地自学习（由 scoregg 数据养成） | — | 显示 `—` / `？` |
| 法定节假日、调休 | `NateScarlet/holiday-cn` | 无 | 退化为「周一到周五」 |
| 队标、英雄头像、装备、召唤师技能、符文图标 | `img.scoregg.com`（图片卡片用） | 无 | 跳过该图，卡片照画 |

除 PandaScore 外全部无需注册、无需付费。四类数据互不依赖：英雄表拉不到不影响赛程，节假日缺失只影响提醒的时间判断。

---

## 一、赛事数据

### 主源：玩加赛事（scoregg）

站点 `www.scoregg.com`，真实数据后端是 `api-v2.scoregg.com`。这是站点前端自己在用的接口，没有公开文档，也没有 SLA。

所有接口都打到同一个 PHP 入口，靠 `api_path` 参数切换：

```
POST https://www.scoregg.com/services/api_url.php
```

固定带 `platform=web`、`api_version=9.9.9`、`gameID=1` 三个参数——缺了会 404 或返回空，这是前端的行为，照抄即可。

| 用途 | `api_path` | 关键参数 | 返回 |
|---|---|---|---|
| 联赛列表 | `services/match/web_tournament_group_list.php` | `language_id=1`、`type=all`、`page`、`limit` | 联赛 ID、中英文名、起止日期、状态、联赛图 |
| 赛程 | `services/match/web_math_list.php` | `date`、`tournament_id`（都可选） | 按日期分组的比赛列表 |
| 单场详情 | `services/match/match_info_new.php` | `matchID` | 比分、胜负方、逐局结果、双方五人精简统计 |

> `web_math_list.php` 里的 `math` 是源站原本的拼写，不是本项目的笔误，改了就 404。

赛程接口两个实测行为：

- **不传 `date`**：返回连续 7 天，键形如 `2026.11.02`，当天无比赛时值是空列表；
- **传 `date` + `tournament_id`**：只返回那一天，且只含该联赛。

逐局明细不在上面这个入口，在静态 CDN 上，一局一个请求：

```
GET https://img.scoregg.com/match/result/{resultID}.json
```

`resultID` 来自详情接口 `result_list[].resultID`。BO5 就是五个请求，代码里直接并发。任何一局取不到都只影响那一局的明细行，比赛本身照常显示。

### 字段溯源（scoregg）

**联赛** `Tournament` ← `web_tournament_group_list.php`

| 内部字段 | 源字段 |
|---|---|
| `id` | `tournamentID`，退化 `id` |
| `name` / `short_name` | `name` / `short_name` |
| `start_date` / `end_date` | `start_date` / `end_date` |
| `status` | `status`（`2` = 已结束） |
| `image` | `image_thumb`，退化 `image`（当前未在渲染中使用） |

**比赛** `Match` ← `web_math_list.php` 的每条 item + 所属 `tournamentinfo`

| 内部字段 | 源字段 |
|---|---|
| `id` | `match_id` |
| `tournament_id` | `tournamentID` |
| `tournament_name` | `tournament_name`，退化 `tournamentinfo.short_name` |
| `round_name` | `round_name` |
| `team_a` / `team_b` | `team_a_short_name` / `team_b_short_name` |
| `score_a` / `score_b` | `team_a_win` / `team_b_win` |
| `best_of` | `game_count` |
| `start_date` | 外层日期键 `2026.11.02` → 归一成 `2026-11-02` |
| `start_time` | `start_time`（`HH:MM`） |
| `status` | `status` |
| `winner` | `win_team_short_name` |

**详情补充** ← `match_info_new.php`

| 内部字段 | 源字段 |
|---|---|
| `start_date` / `start_time` | `start_time`（秒级时间戳），退化 `start_time_string`（`11-01 07:00`，无年份） |
| `winner` | `win_teamID` 比对 `teamID_a` / `teamID_b` |
| `games[]` | `result_list[]` |

**单局** `Game` ← 详情 `result_list[]` + CDN 明细，两段合并

| 内部字段 | 精简版（`result_list[]`） | 明细版（CDN） |
|---|---|---|
| `win_side` | `win_teamID` 比对 `teamID_a` | `{red,blue}_result == "1"` |
| `kills_a/b` | `kills_a` / `kills_b` | `{red,blue}_kill` |
| `gold_a/b` | `money_a` / `money_b` | `{red,blue}_money` |
| `baron_a/b` | `baron_a` / `baron_b` | `{red,blue}_big_dargon` |
| `dragon_a/b` | `dragon_a` / `dragon_b` | `{red,blue}_small_dargon` |
| `duration_min` | `game_time_minute` | `game_time_m` |
| `duration_sec` | — | `game_time_m * 60 + game_time_s` |
| `tower_a/b` | — | `{red,blue}_tower` |
| `herald_a/b` | — | `{red,blue}_riftHeraldKills` |
| `grub_a/b` | — | `{red,blue}_void_grub` |
| `elder_a/b` | — | `{red,blue}_elder` |
| `damage_a/b` | — | `{red,blue}_attack` |
| `sides` | — | `red_name` / `blue_name` |
| `bans_a/b` | — | `{red,blue}_ban_{1..5}`（图标地址） |
| `players_a/b` | `record_list_a/b`（仅 KDA、MVP） | `{red,blue}_star_{a-e}_*`（全量） |

明细按**红蓝方**组织，模型按 a/b 侧组织，映射靠 `red_teamID` 与 `teamID_a` 比对确认 a 侧是红还是蓝。这一步错了会导致整局的红蓝数据对调。

**里程碑** `Game.first_*` ← 同一个 JSON 顶层的 `dragon_list`

`dragon_list.{red,blue}` 里是 `firstBloodKill` / `firstTowerKill` / `firstDragonKill` /
`firstBaronKill` / `firstHerald`，值是 `"0"` / `"1"`。哪个方是 1 就记到哪一侧。

**天赋符文** `PerkSet` ← `data.perks[]`

`perks` 在 `data` 顶层，和 `result_list` 平级，按 `playerID` 索引十名选手。
每人是 `data: [{key, name, perkID, image, desc}]`，按 `key` 认位置：

| `key` | 含义 |
|---|---|
| `perkPrimaryStyle` | 主系（精密 / 主宰 / 巫术 …） |
| `perk0` | 基石符文 |
| `perk1..3` | 主系三枚小符文 |
| `perkSubStyle` | 副系 |
| `perk4..5` | 副系两枚小符文 |

**召唤师技能** 只给图标地址 `.../lol/skill/{id}.png`，那个 `id` 是拳头官方
summoner spell ID（4 = 闪现、12 = 传送、11 = 惩戒 …），跨版本稳定，
所以直接按表翻译，见 `lol/spells.py`。不额外请求，也不怕新英雄。

**出装** 只给 8 个图标地址，**没有道具 ID，也没有名字**——文件名是上传时间戳，
反查不到任何东西。所以卡片上直接画图标，文字版只写件数。

> 空装备格的地址是 `.../items/none.png`（一张灰底图）。不过滤的话卡片上会摆一排灰块，
> 看起来像加载失败。代码里按文件名过滤掉。

**队标** 赛程接口给 `team_a_image`（100X100 缩略图），详情接口给
`team_a_image_thumb`，逐局 JSON 里是 `{red,blue}_flag`。三处都有，优先用逐局的。

**选手** `PlayerStat` ← CDN `{side}_star_{a..e}_*`

| 内部字段 | 源字段 |
|---|---|
| `nickname` | `{side}_star_{x}_name` |
| `champion` | `{side}_hero_{x}_name`，退化选人图标文件名 |
| `hero_id` | `{side}_hero_{x}_heroID` |
| `kills` / `deaths` / `assists` | `{side}_star_{x}_kills` / `_deaths` / `_assists` |
| `mvp` | `{side}_star_{x}_mvp == "1"` |
| `gold` | `{side}_star_{x}_money`（`"14.2k"` 这类带单位，需换算） |
| `cs` | `{side}_star_{x}_hits` |
| `damage` | `{side}_star_{x}_atk_o` |
| `damage_pct` | `{side}_star_{x}_atk_p` |
| `part`（参团率） | `{side}_star_{x}_part` |
| `score` | `{side}_star_{x}_score` |
| `beiguo`（背锅） | `{side}_star_{x}_beiguo == "1"` |
| `hero_level` | `{side}_hero_{x}_lv` |
| `hero_icon` | `{side}_hero_{x}_pic` |
| `damage_taken` / `damage_taken_pct` | `{side}_star_{x}_def_o` / `_def_p` |
| `gold_per_min` | `{side}_star_{x}_money_M` |
| `damage_per_min` | `{side}_star_{x}_atk_m` |
| `honor` | `{side}_star_{x}_honor`（源站给的是 `3,5` 这种编号） |
| `player_id` | `{side}_star_{x}_playerID` |
| `items` | `{side}_star_{x}_equip_1..8`（**只有图标地址，没有道具 ID**） |
| `spells` / `spell_icons` | `{side}_{slot}_skill_1..2`（注意键里没有 `star_`） |
| `perks` | `data.perks[]` 按 `playerID` 匹配 |

### 备源：PandaScore

```
GET https://api.pandascore.co
Authorization: Bearer <token>
```

| 用途 | 端点 |
|---|---|
| 联赛列表 | `/lol/tournaments` |
| 进行中 / 未开始联赛 | `/lol/tournaments/running`、`/lol/tournaments/upcoming` |
| 某联赛的比赛 | `/lol/tournaments/{id}/matches` |
| 赛程（未开始） | `/lol/matches/upcoming` |
| 赛果（已结束） | `/lol/matches/past` |
| 单场详情 | `/lol/matches/{id}` |

PandaScore 没有「按日期拉」的简单参数（`range[scheduled_at]` 这种带方括号的查询串在不同版本行为不一致），所以改成拉「未开始 + 已结束」两头，再按本地日期过滤。

**套餐差异是硬限制**：免费 Fixtures 档只有赛程、赛果、BO 数、战队与联赛元信息；逐局统计与选手数据在 Historical 档（400€/月起）。所以切到备源后「详情」只能看比分，这是套餐决定的，不是解析漏了。免费档限流 1000 请求/小时，代码对同一 URL 做了 60 秒内存缓存。

状态映射：`not_started` / `postponed` → 0，`running` → 1，`finished` / `canceled` → 2。

### 切源逻辑

1. 会话用 `/lol 设置 源 ...` 指定的源优先，没指定就用插件配置的默认源；
2. 当前源抛**可重试**的 `ProviderError` 时，换另一个源重试一次；
3. PandaScore 没配 Key 直接跳过，不阻塞查询；
4. Key 无效（401）属于不可重试错误，直接告诉用户，不白跑另一个源。

只换一次。两个源同时挂时反复重试只会拉长等待、刷满日志。

---

## 二、英雄名称与编号

中文环境有两套编号会混用，必须分清：

| 编号 | 例子 | 来源 | 查询方式 |
|---|---|---|---|
| 英文 ID | `Rumble` | 图标文件名 | Data Dragon `id` |
| Data Dragon key | `84` | 图标的纯数字文件名（`champion/84.png`） | Data Dragon `key` |
| 源站自有编号 | `101` | `champion_new/101.png`、`heroID` | 本地学习映射 |

**同一个数字在两套编号里不是同一个英雄**（`101` 一边是辛德拉、一边是卡尔玛），混查会认错。代码按图标目录区分，`champion_new/` 走学习映射，`champion/` 数字走 Data Dragon key。

### Data Dragon

```
GET https://ddragon.leagueoflegends.com/api/versions.json          # 取当前版本号
GET https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_CN/champion.json
```

拳头官方静态资源 CDN，无需密钥、无调用配额。取到后落盘到 `champions.json`，之后按需读取，不重复拉。

### 源站编号（本地生成，非外部来源）

新英雄的图标路径是纯数字，源站不给英文名。但同一个编号永远对应同一个英雄，所以插件在解析选手数据时，把「`heroID` + 当时拿到的英雄名」记下来，攒成一张映射表。查的比赛越多，认得的越全。

这张表是本插件自己产生的，不来自任何第三方；认不出的显示 `—`，禁选里认不出的显示 `？`，禁选整行认出来不足 3 个时不显示——不拿猜的名字糊弄。

---

## 三、工作日日历

来源 [`NateScarlet/holiday-cn`](https://github.com/NateScarlet/holiday-cn)，每年一份 JSON，由国务院公告整理，结构是 `days: [{name, date, isOffDay}]`。

按顺序试三个地址，第一个成功就用：

```
https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json      # 默认，可在配置里改
https://fastly.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json   # jsDelivr 备用线
https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json
```

判断规则：

1. 在 `makeup`（调休上班）里 → 工作日，即使这天是周六；
2. 在 `holidays`（放假）里 → 休息日，即使这天是周三；
3. 其余按星期几：周一到周五工作日，周六周日休息。

**次年安排一般要到 10 月底到 11 月才公布，公布前那条 JSON 是 200 + 空数组**，所以判断「有没有数据」只能看 `days` 是否非空，不能看 HTTP 状态码。策略是从开放日（默认 `11-01`）起每天试一次，拿到就停手；数据缺失的年份退化成纯周末规则，`/lol 状态` 里会标成「未就绪」。

---

## 四、时间与时区

| 源 | 原始时区 | 处理 |
|---|---|---|
| scoregg 赛程 `start_time` | 站点给定，实测为北京时间 | 直接使用，不做换算 |
| scoregg 详情 `start_time` | 秒级时间戳 | `datetime.fromtimestamp()` → **服务器本地时区** |
| scoregg 详情 `start_time_string` | `11-01 07:00`，无年份 | 按当前年份补齐，跨年时前后偏移修正 |
| PandaScore | ISO 8601 UTC（`2026-11-04T06:00:00Z`） | `astimezone()` → **服务器本地时区** |

有两条路径依赖服务器本地时区。**部署在中国境外的机器（系统时区为 UTC）会让这两个时间相差 8 小时**——赛程接口给的是北京时间，详情接口按 UTC 解释，同一天的两条指令会显示不同的开赛时间。

在中国境内的机器（`Asia/Shanghai`）上不存在偏差。如果要在境外部署，把系统时区设成 `Asia/Shanghai` 即可，代码不用改。

---

## 五、本地持久化

全部写在 `StarTools.get_data_dir("astrbot_plugin_lolmatch")` 下，不写插件自身目录。

| 文件 | 内容 | 来源 | 更新时机 |
|---|---|---|---|
| `sessions.json` | 会话级设置、订阅列表、@ 策略、可提醒时段 | 用户指令 | 每次改设置 |
| `champions.json` | Data Dragon `id`/`key` → 中文名；源站编号学习表 | Data Dragon + 自学习 | 拉取英雄表时、每场详情后 |
| `holidays.json` | 各年放假日与调休日 | holiday-cn | 从开放日起每天探测一次 |
| `icons/<hash前2位>/<hash>.img` | 卡片用的队标、英雄、装备等图标 | img.scoregg.com | 首次用到时下载，上限 3000 个文件 |

三个文件都是原子写入（先写 `.tmp` 再 `replace`），中途断电不会留下半截 JSON。

---

## 六、评估过但未采用的来源

留个记录，避免以后重复调研。

| 来源 | 结论 |
|---|---|
| lolesports 官方 REST | 旧公开 key 全部 403；前端已改为同源 GraphQL，只接受 persisted query，第三方无法稳定调用 |
| Riot Games API | 只有玩家战绩，没有职业赛事数据；开发 key 24 小时过期，限流 20 次/1 秒 |
| Leaguepedia | 数据最全，但限流策略严苛，机房 IP 基本被直接拒绝 |
| `esports.op.gg` | 有可用的 GraphQL 赛程与双版队标，但**没有逐局数据**，撑不起「详情」；且返回时间为 UTC |
| `static.lolesports.com` 队标 | 路径无规律，只能猜中个别战队，覆盖率不可用。改用 scoregg 自带的 `team_a_image`（480–500px 透明底，主流联赛覆盖完整） |

## 七、换源要动哪些文件

`lol/` 包不依赖 AstrBot 运行时对象，换源只需新增一个 provider：

1. 新建 `lol/<name>.py`，实现 `tournaments` / `upcoming_tournaments` / `schedule` / `week` / `today` / `detail` / `close`，把源数据归一化成 `lol/models.py` 里的结构；
2. 在 `lol/provider.py` 的 `PROVIDERS`、`PROVIDER_LABEL`、`_pick()` 里注册；
3. 失败时抛 `ProviderError`，可重试的保持默认 `retryable=True`。

指令层 (`main.py`)、渲染层 (`lol/render.py`) 不需要任何改动，它们只认 `models.py`。
