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
| 历史交手记录 | `img.scoregg.com/match_pre/{matchID}.json` | 无 | 不显示交手区块 |
| 小龙 / 团战时间线 | `img.scoregg.com/lol/livedata/**`（LPL 官方直播数据） | 无 | 不画时间线，其余照常 |
| 比赛阶段（入围赛/瑞士轮/淘汰赛） | `web_math_list.php` 的 `round_name` | 无 | 归到「赛程」一组 |
| 晋级图（对阵树） | `web_math_list.php` 的占位场次 + 形状反推，见下文 | 无 | 该赛段不出图，只列赛程 |
| 瑞士轮战绩与出线 | 同一份赛程自己算（先到 3 胜/3 负） | 无 | 只列赛程，不判出线 |
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

### 赛程窗口：一次只回 7 天

`web_math_list.php` 无论传什么，一次都只回 7 天，键是 `2026.11.02` 这种格式：

| 参数 | 实际行为 |
|---|---|
| 都不传 | 今天往后的 7 天 |
| `date=D` | **`[D-3, D+3]` 这 7 天**——`date` 是窗口锚点，不是单日过滤 |
| `tournament_id=X` | 只含该联赛，窗口仍是 7 天 |

所以「只看某一天」必须自己按日期过滤（`_window(filter_day=True)`），而「看整个
联赛」得按联赛起止日期切锚点分几次拉再按 `match_id` 去重
（`_tournament_schedule`，最多 8 段 ≈ 56 天）。

这条以前记错过——文档写的是「传 `date` 只回那一天」，导致联赛详情实际只能看到
一周，已结束的赛事常常只回 2 场。实测（2026-11）：
`tournament_id=987&date=2026-07-12` 返回 07-06 ~ 07-12 共 8 场，不是 1 场。

### 晋级图：没有 bracket 接口，但能从赛程反推

先说结论：**源站不给对阵树接口，但树是可以重建的**——我一开始判成「做不了」，
是因为把占位场次当垃圾丢了。

#### 关键：淘汰赛后几轮是「占位场次」

`web_math_list.php` 会返回对阵未定的场次，`team_a_short_name`/`team_b_short_name`
是空串或 `TBD`：

```
2026.11.09 06:00 [淘汰赛] '' 0:0 ''         id=59537 status=0   ← 半决赛占位
2026.11.15 03:00 [淘汰赛] 'TBD' 0:0 'TBD'   id=59538 status=0   ← 决赛占位
```

以前解析层用 `if not (team_a or team_b): continue` 把它们丢了，于是
「11-08 半决赛」这个日期在插件里根本不存在，看上去就是源站没有树。

顺带一个坑：这些占位场次的 `team_a_image` / `team_b_image` 是**同一张默认队标**
（白圆图，`/m/393334/p/227/2817545758050.png`），不是任何队伍的徽章。画出来
就是两个白点，所以解析时把「队名是 TBD」的一侧的队标清空。

#### 树的结构：从末尾往前数 1、2、4

没有轮次字段（`round_name` 只到「入围赛 / 瑞士轮 / 淘汰赛」），但对阵树自身的
形状是自证的：从决赛往前永远是 1、2、4、8 场递减，多出来的归最左一列。

| 赛段 | 场次 | 反推的列 | 与官网截图 |
|---|---|---|---|
| S16 淘汰赛 | 7 | `[4, 2, 1]` | 一致 |
| S16 入围赛 | 6 | `[3, 2, 1]` | 一致 |
| S16 瑞士轮 | 33 | 反推成 `[18, 8, 4, 2, 1]` → **放弃画** | 官网也是列表，不是树 |

两道闸：首列超过 8 场（16 队）就判定不是淘汰赛形状；某列超过下一列的 2 倍也
判定为形状不成立。瑞士轮就是被这两道闸挡掉的——把瑞士轮画成八强树是错的。

#### 连线：按数据连，不按位置连

排版的纵向位置可以用形状推（子块对到两个父块的中心之间），但**画线不能**——
2026 MSI 的淘汰赛是 14 场双败，形状算出来 `[7, 4, 2, 1]`，照位置连线会连出
一堆根本不存在的对阵。

所以连线只看数据：一场比赛里某个队，上一次出现在同一赛段的哪一场，那就是它的
来路——赢了是晋级，输了是掉进败者组，反正它是从那儿来的。这条规则单败双败都成立。

只有形状**严格整倍递减**（4→2→1）时，才额外补一波按位置的连线，用来兜住
「对阵未定」的场次（它们没有队名，只能靠形状连）。

轮次名同理：只有严格整倍递减才标「八强 / 半决赛 / 决赛」，否则老老实实写
「第 1 轮 / 第 2 轮 / 第 3 轮」。S16 淘汰赛是 4→2→1，标了八强；
S16 入围赛（3→2→1，4 队双败）和 MSI 淘汰赛（7→4→2→1，8 队双败）都只写轮次。

#### 瑞士轮：不是树，是一条按轮次铺开的跑道

S16 瑞士轮 33 场，按 1、2、4 反推得到 `[18,8,4,2,1]`——首列 18 场，
根本不是树。硬画就是对读者的欺骗：那 18 场之间没有「谁淘汰谁」的关系。

瑞士轮的真实结构是「先到 3 胜出线、先到 3 负出局」：

```
晋级 8：2 个 3-0 + 3 个 3-1 + 3 个 3-2
淘汰 8：2 个 0-3 + 3 个 1-3 + 3 个 2-3
```

所以它画成**轮次跑道**而不是树：一轮一列，列内按赛前战绩分档（胜多在上），
每场比赛一个格子；列间连线是「某队上一场在这里，这一场在那里」——赢了走实线，
输了走虚线。右侧积分榜给出名次、大场、小局和晋级线。全部从赛程算出来，
不需要任何新接口：

- **轮次靠回溯**：一支队打第几场就是第几轮（`played + 1`），不信任何静态标签；
  同一场两边已赛场次不等、出现未定对阵、轮次编号断档，都不画。
- 赛前战绩决定分档和这场性质（晋级战 / 淘汰战 / 生死战），**算完再更新**，
  否则 3-0 出线那场会被记成「常规轮」；判定必须双向，只看胜场会漏掉 2-2 生死战
  败者的淘汰标记。
- 小局分是逐场 `score_a` / `score_b` 累加（BO1 记 1-0，BO3 记 2-1），
  和大场分并列——同为 3-2，6-4 和 6-5 是两种状态。
- 队数是 2 的幂、没有队打过 5 场以上，才认定是瑞士轮——S 赛小组赛也是 16 队、
  也有人三胜，但双循环会打到 6 场，这条能把它们分开。

核对方式：`img.scoregg.com/tr_round/p_2262.json`（`2262` 是 S16 瑞士轮的
`roundID`）一次给回整轮 33 场，与「按 7 天窗口拼赛程」的结果逐场一致，
分档也逐列对得上（`8/8/8/6/3`，档位 `0-0`、`1-0|0-1`、`2-0|1-1|0-2`、
`2-1|1-2`、`2-2`），16 队战绩和官网榜单一致。

#### 赛段出图的取舍：一场没打完就不画

`/lol 联赛详情` 现在**一个赛段一张图**，赛程和晋级关系在同一张里。但刚开赛的
赛段整段都是占位场次（两个队名都是 TBD），画成树只有「待定」，画成跑道没有连线——
没有结果可看，不如不出，配文里写一句「XX 起 尚无赛果，未出图」。

判断标准很简单：`any(match.finished)`。S16 在 11-04 当天就是这种情况——
入围赛、瑞士轮已打完，淘汰赛 7 场一场未打，所以只出两张图。

#### 明确不做的

| 想拿的东西 | 结论 |
|---|---|
| 轮次细分字段 | 没有。`round_name` 只到赛段这一层，八强四强是反推出来的 |
| bracket 接口 | 没有。盲探 `web_tournament_vs.php` 等一批路径全是 404 |
| `rounds-menu-wrap` 那套轮次菜单 | 是按轮次**筛选列表**的标签页，不是树；数据来自 `moreGame/round_match_list.php`，必须带 `roundID` + `round_son_id`，而这两个 ID 只在联赛页内嵌的 `php_data` 里，不是接口 |
| `team_point_list`（积分/晋级榜） | 嵌在联赛页 HTML 的 `php_data` 里，形状还不固定：1023 是按 `roundID` 分组的 dict，987 是空 list |

也就是说：**分组（谁跟谁打、谁赢了）全部来自真实赛程**，只有「第几列」是按形状
推的。瑞士轮不走这条路，改用战绩 + 出线判定（见上一节）。

### 历史交手：赛前分析 JSON

```
GET https://img.scoregg.com/match_pre/{matchID}.json
```

静态 CDN，无需鉴权。这是比赛页「赛前分析」面板的数据源，一份响应里带着
`strength_index`（实力指数）、`match_record`（近 6 次交手）、
`match_record_a` / `match_record_b`（双方各自近 6 场战绩）。

插件只用 `match_record`：

```
match_record = {
  team_a_win_count: 1,
  team_b_win_count: 2,
  list: [{ win_team_id, win_team_name, start_time, matchID, title,
           teamID_a, teamID_b, team_a_short_name, team_b_short_name,
           team_a_win, team_b_win, result_list[] }]
}
```

**`team_a_win_count` 的 a / b 指的是「当前这场比赛」的 a / b**，但 `list[]` 里每条的
`team_a_short_name` 是**那场比赛自己**的主客排法——两者不一定同向。
实测 59531：当前 a = HLE，而 07-12 那条记录把 BLG 排在左边。
所以解析时必须先归一化（左队不是当前 a 队就整条对调），否则同一支队伍的胜负
会一半记在左、一半记在右。

对已结束的比赛，这个文件仍是**赛前生成的快照**，不含本场——正好就是「交手记录」要的东西。

### 小龙时间线：LPL 官方直播数据

两段取数：

```
GET https://img.scoregg.com/lol/livedata/{matchID}.json     # 局列表
GET <该局的 web_url>                                        # 事件流
```

局列表每项形如：

```
{ status, blue_clan_name, red_clan_name, blue_teamID, red_teamID,
  win_teamID, web_url, url, source }
```

`status = 1` 表示这局有完整数据，`-1` 表示没有（此时 `web_url` 为空）。
`web_url` 通常指向 `lol/livedata/2/{livedata_key}.json`。

事件流里有用的两块：

| 数据 | 字段 | 结构 |
|---|---|---|
| 小龙击杀 | `team_a.dragons[]` / `team_b.dragons[]` | `{dragon_type, game_time, game_time_txt, dragon_image}` |
| 团战区间 | `team_battle[]` | `{seq, begin_time, end_time, begin_time_txt, end_time_txt, data[]}` |

**关键坑：`group_id` 和 `groupId` 是互为镜像的两个字段。**
`groupId` 是自己所在的分组（100 = 蓝方，200 = 红方），`group_id` 是**对方**的分组。
取错的话双方拿龙记录整片对调。

**元素龙只取两队的 `dragons[]`，不要用顶层 `dragon_event[]`。**
顶层那个混着别的东西：59531 的第 1 局里，15:17 有一条 `dragon_type=6`、
36:11 有一条 `dragon_type=0`，两条都不计入任何一方的元素龙数（`small_dargon` 3:4）。
照单全画会出现「时间线上 5 条龙、资源格写着 3 条」的自相矛盾。
两队的 `dragons[]` 才是源站自己统计过的口径，跟 `small_dargon` 天然一致。

**`dragon_type` 不当名字用。** 源站前端那份 iconfont 映射（0 = 大龙、1 = 火龙、
2 = 风龙、3 = 土龙、4 = 水龙、5 = 小龙、6 = 远古龙）与实际数据矛盾——
按它念，15:17 就会出现「远古龙」。所以卡片只按编号取图标
（`images/lol/dragon/icon1-{type}.png`，1/2/3 张图实测都有），文字版只写时刻。

**覆盖范围有限**：只有录到官方直播数据的局才有时间线。同一场比赛可能
第 1 局有、第 2 局没有（59531 就是这样），代码按局序号对齐，对不上就整块不画。

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
| scoregg 晋级图接口 | 没有对阵树接口，也没有轮次细分字段。但树能从赛程反推（占位场次 + 1/2/4 形状），所以没走「找接口」这条路。详见「晋级图：没有 bracket 接口，但能从赛程反推」 |

## 七、换源要动哪些文件

`lol/` 包不依赖 AstrBot 运行时对象，换源只需新增一个 provider：

1. 新建 `lol/<name>.py`，实现 `tournaments` / `upcoming_tournaments` / `schedule` / `week` / `today` / `detail` / `close`，把源数据归一化成 `lol/models.py` 里的结构；
2. 在 `lol/provider.py` 的 `PROVIDERS`、`PROVIDER_LABEL`、`_pick()` 里注册；
3. 失败时抛 `ProviderError`，可重试的保持默认 `retryable=True`。

指令层 (`main.py`)、渲染层 (`lol/render.py`) 不需要任何改动，它们只认 `models.py`。
