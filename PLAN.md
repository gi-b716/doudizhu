# 斗地主 Copilot：模拟器 + 实时出牌建议 — 实施计划

> 本文件是交给 Claude Code CLI 的实施计划（自包含）。仓库当前只有 `src/doudizhu.js`（平台前端客户端，只读参照）与 `src/rules.md`（规则，只读参照），二者是实现时的语义金标准。
>
> 目标：**Copilot 工具** —— 在真实对局中实时给出可解释的出牌建议（不代打、不自动提交），内置无界面模拟器与自对弈竞技场用于验证策略强度。
>
> 已确认决策：Python 核心 + 油猴薄层；交付形态 = 网站实时悬浮窗；引擎 = 启发式（基线/兜底/叫分/残局）+ 开源 DouZero（主力）；自训模型暂缓（用户可提供终局 state JSON 用于验证/调参）；运行机 = RTX 4060 Laptop (8GB, Windows, Python/Node/PyTorch)，开发机为低配公共电脑（Python 3.13，无 torch）。

---

## 0. 已核实的硬事实（决定设计，勿再假设）

### 0.1 平台客户端（`src/doudizhu.js`）

- **牌 id**：`id = rank*4 + suit`；rank 0..12 = 3,4,…,K,A,2；`52 = 小王`、`53 = 大王`；`rank(id) = id >> 2`（`cardName()` 用 `id & 3` 取花色）。
- **协议**：全部经 `POST /games/doudizhu/api`，`URLSearchParams{_token, action, game, version, chat_after, ...extra}`，`credentials: 'same-origin'`。action：
  - `state`（轮询：可见时 900ms、隐藏/终局 3000ms）→ 返回 `{state}` 或 `{unchanged:true, remaining}`；
  - `bid`（`value: '0'..'3'`）、`play`（`cards: '3,7,11'` 升序 + `choice: patternKey`）、`pass`、`auto`（`on: '0'|'1'`）、`chat`（`text`）。
- **首屏**：HTML 内嵌 `<script id="ddz-initial">{admin, state, chat}</script>`；`#ddz` 上有 `data-game` / `data-token` / `data-logged-in`。
- **状态在 IIFE 闭包内**，外部拿不到 → 悬浮窗必须**拦截 `window.fetch`**（克隆响应，不改变行为）+ 读 `#ddz-initial`。
- **state 字段**：`version, seat(旁观为 null), phase('bidding'|'playing'|'finished'), turn, remaining(ms), reserve_in_remaining, reserve[3], bid(当前最高叫分), landlord, must_bid, bottom(3 张，地主确定后公开), secret_bottom(仅管理员), hand(自己手牌，旁观为 null), hands(仅管理员，三家手牌), table[3]('pass'|{cards,pattern}|null), last{seat,pattern}, log[], multiplier, bombs, players[{username,count,bid,role,auto}], started, redeals, fairness{commitments[], deals[{deck[54],salt,commitment,first_bidder}]}, result{landlord_won,spring,anti_spring,base,multiplier,deltas[3],finished}, match{...}`。
- **log 条目**：`{t, kind:'redeal'|'bid'|'landlord'|'play'|'pass'|'auto_on'|'auto_off'|'finish', seat, value, cards, pattern, auto, timeout}` —— 每一步 play/pass 都在其中。
- **`leading` 语义**：`state.leading = phase==='playing' && (!last || last.seat === turn)`。
- **终局即完美信息样本**：`phase==='finished'` 的 state 含 `fairness.deals[]`（每副牌 54 张完整牌序 + salt）与全量 log → 抓历史对局 = 完美标注数据集。
- **客户端需重建的 `window.DoudizhuRules` API**（该文件未爬取、全盘无副本）：`RANK_NAMES`、`rank(id)`、`sortCards(cards)`、`describe(pattern)`、`classify(cards)`、`playable(cards, toBeat)`、`patternKey(pattern)`、`hints(hand, toBeat)`、`smartSelect(cards, follow)`。
- **牌面图**：站点同源 `/images/games/doudizhu/<id>.webp`（0..53），浮窗可直接复用。
- **重放语义参考**：客户端 `snapshot(k)`（约 L789–846）按 log 前缀重建任意时刻牌桌——我们的 `replay()` 逐行移植它。
- **手牌 UI 可被合成指针事件驱动**（支撑可选「帮我选中」）：`pointerdown` 处理器先赋值 `drag` 再 `setPointerCapture`（合成 pointerId 会抛异常但已被吞掉，`drag` 已生效）；`endDrag` → `renderHand()` 会 `replaceChildren` **整体重建节点**，所以每次迭代必须重新 `querySelector('[data-card=id]')`；清空选择可向 `.ddz-table` 派发 `contextmenu`（其处理器要求 `interactive()`）。

### 0.2 DouZero（已核对上游仓库，Apache-2.0，ICML 2021）

| 项 | 结论 |
| --- | --- |
| 仓库 | `github.com/kwai/DouZero`；**权重不在 git**（`baselines/` 是空占位） |
| 权重来源 | 官方 Google Drive 文件夹 `1NmM2cXnI5CIWHaLJeoDZMiwt6lOTV_UB` 或百度网盘 `18g-JUKad6D8rmBONXUDuOQ`（码 `4624`）；用 `douzero_WP`（winning-percentage） |
| PyPI | `douzero 1.1.0`（2021-08）sdist、无依赖声明，requirements 拉 `rlcard` 与已死包 `gitdb2` → **不要 pip 安装，clone 仓库 + 按路径 import** |
| 观测 | `z = (5,162)` int8（最近 15 步 × 54 摊平）+ 每合法动作一行 `x_batch`：**地主 373 维**（5×54 + 单向 17+17+15 + 54 动作面）、**农民 484 维**（7×54 + 20+17+15 + 54） |
| 动作空间 | **没有固定 54 动作空间**——合法动作是逐状态的牌组列表，网络对每个给定动作打分取 argmax |
| 牌码 | 3..14 = 3..A，**17 = '2'**，20 = 小王，30 = 大王（15/16 空）；`_cards2array` = 4×13 按 Fortran 序 + 2 王位 |
| 位置 | `landlord`；`landlord_down` = 地主**下家**（`(seat-landlord)%3==1`）；`landlord_up` = 地主上家 |
| other_hand_cards | = `54 − 我的手牌 − 已出牌`（实战可算，无信息泄露） |
| 推理 | `model.forward(z_batch, x_batch, return_value=True)['values']` → argmax，确定性 |
| 反序列化坑 | torch ≥ 2.6 `torch.load` 默认 `weights_only=True` 会挂 → 对**校验过 sha256 的本地权重**显式 `weights_only=False` |
| 规则差异 | 上游「四带两对」(type-14, size 8) 允许 `3333+4444`；`rules.md` 禁止带炸弹 → 由我们的合法集过滤兜住，并写明为已知且唯一的差异 |
| 叫分 | DouZero 无叫分模型、无流局概念 → **叫分永远走启发式** |

---

## 1. 总体架构

```
真实对局页 ──fetch 拦截──► 油猴脚本（Shadow DOM 浮窗）
                               │ POST /advise  (127.0.0.1:17876)
终局 state ─────────────────►  │
                               ▼
                        本地 FastAPI 服务
                         ├─ ddz.state   平台 state → GameState
                         ├─ ddz.moves   合法着法生成（唯一契约）
                         ├─ 启发式 agent（兜底/解释/叫分）
                         ├─ 残局求解器（少牌时完美信息 + 定拆）
                         └─ DouZero agent（主力；无 torch 自动降级启发式）
数据归档: /ingest → data/games.jsonl（完美信息对局库）
离线: ddz.arena（自对弈评测）· ddz.tools.eval_records（对局库回放评估）
```

关键设计原则：**合法着法永远由我们的引擎生成**；DouZero 只对给它的动作打分。这样规则差异、叫分、流局、版本抖动全部不进入模型。

## 2. 目录结构（新建；`src/` 保持只读）

```
D:\doudizhu\
├─ pyproject.toml / requirements.txt（不含 torch）/ requirements-cu.txt / README.md / .gitignore
├─ ddz\
│  ├─ __init__.py
│  ├─ cards.py patterns.py moves.py        # 规则引擎
│  ├─ state.py record.py counter.py explain.py
│  ├─ engine\
│  │  ├─ base.py decomposer.py heuristic.py bidding.py endgame.py
│  ├─ douzero\
│  │  ├─ vendored.py bridge.py agent.py weights.py
│  ├─ agents\
│  │  ├─ random_agent.py registry.py       # "heuristic"|"douzero:WP"|"random"|路径
│  ├─ arena\
│  │  ├─ simulator.py match.py metrics.py
│  ├─ data\
│  │  └─ archive.py
│  ├─ service\
│  │  ├─ app.py schemas.py advisor.py __main__.py   # python -m ddz.service
│  └─ tools\
│     ├─ bench_douzero.py verify_fairness.py eval_records.py
├─ userscript\ddz-copilot.user.js
├─ tests\（conftest.py + 各 test_*.py + fixtures\*.json）
├─ data\games.jsonl（gitignore）· data\raw\ · data\fixtures\
├─ runs\（竞技场输出，gitignore）
└─ third_party\DouZero\（clone 后锁定 commit：COMMIT.txt；权重放 baselines\douzero_WP\）
```

环境（开发机无 torch 也能跑除 DouZero 外全部）：

```bash
py -3.13 -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
# requirements.txt: fastapi>=0.115 uvicorn>=0.30 pydantic>=2.7 numpy>=1.26 pytest>=8 pytest-timeout requests

# 运行机（4060）追加 torch（按驱动选 CUDA index，以 pytorch.org 当前指路为准）：
pip install torch --index-url https://download.pytorch.org/whl/cu128
git clone https://github.com/kwai/DouZero.git third_party/DouZero
git -C third_party/DouZero rev-parse HEAD > third_party/DouZero/COMMIT.txt   # 锁定版本
# 手动下载 landlord.ckpt / landlord_up.ckpt / landlord_down.ckpt 放入 baselines/douzero_WP/
```

## 3. 模块设计要点

### 3.1 规则引擎（`cards / patterns / moves`）

**cards.py**：Card = int 0..53（与平台一致，零翻译）。提供 `rank/suit/is_joker/card_name/sort_cards/ranks_of/rank_counts/parse_cards("3,7,11")/format_cards`、`to_env_card/from_env_card`（→ DouZero 牌码双射）与 `full_deck()`。

**patterns.py**：

```python
@dataclass(frozen=True, slots=True)
class Pattern:
    type: str      # single|pair|triple|triple_single|triple_pair|straight|pair_straight|
                   # plane|plane_single|plane_pair|four_two|four_two_pair|bomb|rocket
    main_rank: int # 链条取最低点数
    length: int = 1
    def size(self) -> int; def label(self) -> str; def describe(self) -> str; def key(self) -> str
# key 形如 'triple:9:1' / 'straight:0:5'（稳定、可 JSON 存储、可 grep）
classify(cards) -> list[Pattern]       # 全部读法（333444555666 → 飞机/飞机带单 等）
classify_one(cards) -> Pattern | None
beats(a, b) -> bool; is_bomb(p); explodes_multiplier(p)
```

- 读法判定算法：一次点数直方图 + 分族测试（禁止子集暴力）：rocket → bomb → 单/对/三 → 三带一/三带对 → 顺子（5..12 张、全 count==1、连续、**max_rank ≤ 11**）→ 连对（L≥3、全 count==2、max_rank ≤ 11）→ 飞机族（对每个 count≥3 的连续段 rank ≤ 11，各子段长 L≥2 分别匹配 3L/4L/5L）→ 四带二族（6 张：一个四张 + 对子或两单；8 张：一个四张 + 恰两个对子）。
- 带牌硬规则（逐条来自 `rules.md`，全部为硬过滤）：
  - 带牌不得与主体同点；
  - **带牌不得含炸弹**（⇒ `3333+4444` 非法；上/下家口径与 DouZero 不同，在此拦住）；
  - `plane_pair` 翅膀必须恰为 L 个对子（四张=炸弹 → 拒绝）；
  - `plane_single` 翅膀可为单张/对子/三张（三张=三个单），四张不行；
  - `four_two` 翅膀可为一对；
  - 顺子/连对/飞机主体只能 3..A。
- `beats`：王炸最大；炸弹 > 任何非炸；炸弹比点数；其余同 type、同 length、`main_rank` 更大。
- 多读法顺序：固定优先级（炸弹/王炸在前，然后长链、大 size 在前），单测断言精确输出。

**moves.py**：

```python
@dataclass(frozen=True, slots=True)
class Move:
    cards: tuple[int, ...]; pattern: Pattern
    def key(self) -> str
legal_moves(hand, to_beat: Pattern|None, *, canonical=False, limit=None) -> list[Move]
legal_leads(hand, *, canonical=True) -> list[Move]
legal_beats(hand, to_beat) -> list[Move]      # 含炸弹/王炸，不含 pass
has_any_beat(hand, to_beat) -> bool
winning_move(hand, to_beat) -> Move|None      # 一把走完
```

- 翅膀在**点数多重集**层枚举（`pick_wings`：最低点数优先、优先拆单张而非对子/三张、**绝不拆炸弹**），再展开为具体牌 id。
- `canonical=True`：每个 `(type, main_rank, length)` 只留最省一手（领出/启发式候选）；`canonical=False`：全部具体着法（**DouZero 打分与残局求解**），带 `limit` 防爆。
- 跟牌按类型定向生成（如跟顺子只生成同长更大 + 炸弹/王炸），绝不枚举子集。

### 3.2 状态适配与对局库（`state / record / counter / explain`）

- `GameState`：`version/seat/phase/turn/leading/hand/all_hands/bottom/secret_bottom/landlord/high_bid/must_bid/multiplier/bombs/players/table/last/events/deal_index/played_by_seat/remaining_ms/reserve/result/match/raw`；属性 `my_turn/partner_seat/is_landlord/to_beat/counts/unseen/threat_strings`。
- `from_platform_state(payload, prev=None)` 边界处理：旁观（`hand=null`，管理员有 `hands` 则可为 `turn` 座位建议）；叫分合法集 `{0} ∪ {v>high_bid}`（`must_bid` 禁 0）；`unchanged` 只刷新计时、不重建；流局事件截断 `events` 并递增 `deal_index`；version 跳跃不做增量假设；`last.pattern` 解析失败抛 `StateParseError`（服务转为降级响应，绝不崩溃）。
- `record.py`：`GameRecord.from_finished_state()/to_line()/from_line()`；`replay()` **移植客户端 `snapshot(k)`** 逐前缀重建；`iter_decisions()` → `(state, seat, 实际出牌)`（评估/训练样本的唯一生成器）。
- JSONL 存储（每行一局）：

```json
{"schema":"ddz.record.v1","game_id":"...","captured_at":1759600000.0,"source":"platform",
 "rules":"rules.md@2026-10","players":["u0","u1","u2"],"seat_of_interest":0,
 "match":{"id":123,"round":4,"rounds":9,"rating_before":[1502,1499,1500]},
 "result":{"landlord_won":true,"spring":false,"anti_spring":false,"base":3,"multiplier":4,"deltas":[12,-6,-6]},
 "deals":[{"index":0,"deck":[54 个 int],"salt":"hex","commitment":"hex","first_bidder":2}],
 "events":[{"t":9150,"kind":"play","seat":2,"cards":[41,45,49],"pattern":"triple:9:1"}]}
```

  去重键 = `sha256(deals[0].deck | salt | players)`；`pattern` 存 `key()` 字符串；`--keep-raw` 时另存 `data/raw/<game_id>.json`。
- `counter.py`：`unseen / unseen_by_rank / possible_bombs / rocket_possible / threats[]`（对手剩 1-2 张、某点数 4 张全未见、双王未见、我方最大单牌被未见牌压制、队友濒临走完、必须拦等）。
- `explain.py`：所有中文文案集中一处（我们的描述不可避免与平台不同——`DoudizhuRules` 缺失，平台的 `choice/patternKey` 具体格式不可知）。

### 3.3 启发式 agent（Phase 2–3 主力，之后永远兜底）

- `decomposer.py`：`greedy_decompose`（多种抽取顺序取最少手数、绝不拆炸弹/王炸）+ `exact_min_plays`（记忆化 DFS、节点/时间预算，默认 20k 节点 / 40ms），返回 `Decomposition{plays, n_plays, control, bombs, rockets}`；`hand_strength(hand)`（0..100，叫分用）。
- `heuristic.py` 打分（每项都能生成中文理由）：

```
score(m) = +W_RED·(手数减少) − W_COST·牌力消耗 + W_CTRL·保留控制
           −W_SPLIT·拆结构 −W_BOMB·炸弹代价 +W_TEMPO +W_THREAT +W_COOP
score(pass) = 0；默认权重 W_RED=3.0 W_COST=0.30 W_CTRL=1.6 W_SPLIT=0.9 W_BOMB=6.0 W_TEMPO=4.0 W_THREAT=2.5 W_COOP=3.0（用对局库/竞技场调）
```

- **硬规则覆盖**（同名单测，不参与打分）：无牌可管→不出；能一把走完→立刻走；地主对 ≤2 张的农民必须拦（拦不住则警告）；上家是队友→默认放行（除非我能一波走完 / 队友>6 张而我显著更少需接管倒牌 / 队友≤2 张绝不压）；炸弹纪律（只用于赢牌、拦 ≤2 张对手、或评分显著占优，绝不炸队友）。
- `bidding.py`：`hand_strength = 2.2·#王 + 1.4·#2 + 0.8·#A + 3.0·#炸 + 4.0·王炸 + (17 − 3.2·手数) + 0.5·顺子加成` → 阈值映射 0..3 后夹取到合法集；`must_bid` 强制 ≥1，理由「已连续流局，本轮必须叫分」。阈值三档同为 10（见 `bidding.py` 的 `THRESHOLDS` 注释）：叫 1、2 分的地主实测胜率 34% / 39%，期望为负；收益正比于叫分，所以只保留叫 3 分。
- `endgame.py`：触发条件 `sum(counts) ≤ 16 且 我 ≤ 8 张 且 |unseen| ≤ 10`；状态打包成单个 int 记忆化（三家 15 位五进制计数向量拼接 + turn + to_beat 小 id）；α-β、走完优先排序、赢得更快优先；**暗牌**：对手牌多重集已知、拆分未知 → ≤64 种确定化（小局面全枚举，否则有种子采样），取各拆分最差值（悲观）；全部拆分都输时返回「劣势局面，尽量拖延」并给最长存活着法；任何异常/超时静默回退启发式。

### 3.4 DouZero 接入（Phase 4，纯增量升级，不改变兜底路径）

- `vendored.py`：`importlib.util.spec_from_file_location` 按路径加载 `third_party/DouZero` 的 ~8 个叶子模块（`env/{env,game,move_detector,move_generator,move_selector,utils}.py`、`dmc/models.py`、`evaluation/deep_agent.py`）；`is_available()` = torch 存在且权重存在 → **无 torch 时整个服务以启发式运行**。
- `bridge.py`：
  - `position_of(gs, seat)`；`legal_actions = legal_moves(hand, to_beat, canonical=False)` 映射为 DouZero 牌码（启动时断言牌码双射）；
  - InfoSet（duck-typed SimpleNamespace，上游 `get_obs` 只读属性）：`player_position / player_hand_cards / legal_actions / card_play_action_seq / last_move / last_move_dict / played_cards / num_cards_left_dict / other_hand_cards(=unseen) / three_landlord_cards / bomb_num`；
  - **直接调用上游 `get_obs()` 生成观测**，不重写编码器；`choose()`：单动作短路，否则 `forward(z,x,return_value=True)['values']` → argmax → 映射回我方 `Move`；`win_prob = (v+1)/2`，界面标注「DouZero 估值」。
- `agent.py`：启动加载 + 假前向预热；`ThreadPoolExecutor` + `future.result(timeout=timeout_ms/1000)`（默认 400ms，Windows 无 SIGALRM）；异常/超时 → 启发式兜底并在响应标注 `fallback`；`bid()` 永远走启发式；单锁串行化模型调用；`status` 暴露 loaded/device/last_error；双方首选不一致时界面显示「策略分歧」。
- `weights.py`：定位三个 ckpt、首次加载打印 sha256、按形状校验；`torch.load(..., weights_only=False)` 优先，失败再试 `weights_only=True`，错误信息指路 `TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1`（仅对本地校验过的文件允许）。

### 3.5 本地服务（FastAPI + uvicorn，绑定 `127.0.0.1:17876`）

- 中间件：`allow_origins=['*']` + 显式 OPTIONS 返回 **`Access-Control-Allow-Private-Network: true`**（Chrome PNA 预检）；脚本侧用字面量 `127.0.0.1`（Windows `localhost` 先解析 `::1`）。
- 端点：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | `{ok, version, torch, cuda, douzero_loaded, models, uptime_s, records}` |
| POST | `/advise` | 主入口 |
| POST | `/ingest` | 归档终局 state |
| POST | `/config` | 运行时切换 agent 等 |

- `/advise` 请求：`{session, state(原始平台 state), options:{agent:"auto|heuristic|douzero", top_k:3, deadline_ms:400, for_seat:null}}`

  响应（逻辑问题也返回 200，便于脚本处理）：

```json
{"ok":true,"version":842,"seat":0,"phase":"playing","is_my_turn":true,"leading":false,
 "to_beat":"pair:5:1",
 "recommended":{"cards":[3,7],"pattern_key":"pair:0:1","description":"对子 3",
   "score":1.83,"win_prob":0.61,"source":"douzero",
   "rationale":["手数从 7 降到 6","保留大王作为控制牌"]},
 "alternatives":[{"cards":[],"description":"不出","score":0.0,"source":"heuristic","rationale":["上家是队友，让他走"]}],
 "card_counter":{"unseen_by_rank":[...15 个数],"possible_bombs":[9],"rocket_possible":false},
 "threats":[{"level":"warn","text":"下家只剩 2 张，你若无大牌需抢先出完"}],
 "hand_analysis":{"n_plays":6,"control":["大王","2"],"bombs":["3333"]},
 "bidding":null,
 "timing_ms":{"total":48.2,"parse":1.1,"moves":2.4,"agent":12.9,"obs":5.0},
 "engine":{"name":"douzero:WP","device":"cuda","fallback":false}}
```

  失败形态：`{"ok":false,"reason":"spectator|not_my_turn|parse_error|no_engine","recommended":null,"message":"..."}`。

- `advisor.py`：会话状态（按 session 存上一份 GameState）、手牌拆解 LRU、建议缓存（键 `(version, seat, agent)`）；非我方回合只回记牌/牌力（跳过 agent）；agent 调用放线程池不阻塞事件循环。延迟预算：解析+生成+启发式 ~5ms，DouZero ~5–40ms → 远小于 15s 出牌时限。

### 3.6 油猴脚本（`userscript/ddz-copilot.user.js`）

- 头：`@run-at document-start`、`@grant none`（页面上下文才能包真实 fetch）、`@include /^https?:\/\/[^/]+\/games\/doudizhu/`、`@noframes`。
- 捕获：包装 `window.fetch`，命中 `/games/doudizhu/api` 时 `r.clone().json()` 异步旁路（**绝不改写请求/响应**）；`DOMContentLoaded` 读 `#ddz-initial` 兜底首屏。
- 分发：`data.state` → `accept()`；`finished` → `/ingest` 一次（按 version 去重）并停更建议；`turn===seat 且未终局` → 防抖 150ms 调 `/advise`（同一时刻最多一个在飞，旧响应按 version 丢弃）；否则显示「等待 X 出牌…」并把旧面板置灰。换局是整页跳转，脚本随页面重建；session 存 `sessionStorage`。
- 浮窗：`fixed` 右下、可拖拽/折叠（localStorage 持久化）、Shadow DOM 自带样式；内容 = 轮次横幅、推荐牌（同源 `<img src="/images/games/doudizhu/<id>.webp">`）、中文描述、备选 1–3、理由 bullets、记牌器、威胁提示、页脚 `douzero:WP · cuda · 48ms`。
- `帮我选中`（**默认关闭、绝不点出牌/绝不调 API**）：先向 `.ddz-table` 派发 `contextmenu` 清空选择 → 对每张建议牌派发 `pointerdown`+`pointerup`（`{bubbles, cancelable, pointerId:0, button:0}`）→ 每轮 `await requestAnimationFrame` 并**重新查询** `#ddz-hand .ddz-card[data-card="<id>"]`（页面会整体重建节点）；仅当本地状态 `playing && turn===seat` 且手牌 DOM 存在时执行。

### 3.7 评测

- `arena/simulator.py`：完整规则无头对局（发牌/叫分/流局/must_bid/加倍计分/春天反春），每步断言 agent 着法合法（同时是 moves.py 的持续集成）。
- `arena/match.py` CLI：

```bash
python -m ddz.arena.match --players heuristic,random,random --games 200 --seed 7
python -m ddz.arena.match --players douzero:WP,heuristic,heuristic --games 300 --seed 11 --rotate --out runs/dz_vs_heur
```

  输出 `runs/<name>/summary.json`：分座位/角色胜率、ADP、平均倍数、延迟 p50/p95/p99、兜底/错误计数。
- **DouZero 正确性两道闸**（必须过）：
  1. `tests/test_douzero_bridge.py`：用上游自己的 `GameEnv` 全信息重放一局，在每个决策点断言 `get_obs(我们的 InfoSet)` 与 `get_obs(上游 InfoSet)` **逐元素相等**（含 `(N,373)/(N,484)` 形状）；
  2. `tests/test_douzero_legality_parity.py`：2000 手随机牌对比 `legal_moves` 与上游 `MovesGener`，差异必须全部属于已知且有意的一条（`3333+4444`），出现新差异即失败。
- 离线（用户对局库）：

```bash
python -m ddz.tools.verify_fairness --records data/games.jsonl   # sha256(deck.join(',')+'|'+salt)
python -m ddz.tools.eval_records --records data/games.jsonl --agent douzero:WP --top-k 3
```

  指标：精确/前 3 一致率、人类「本可管却不出」的放行一致率（最有信息量）、人类着法 vs agent 的分数差、按开局/中局/残局分解；启发式权重用它做网格搜索、竞技场胜率做 tie-break；能达残局阈值的局面用求解器给**真实 regret**（标注），其余标注「估算」。

## 4. 分阶段实施

| 阶段 | 交付 | 验证 | 量级 |
| --- | --- | --- | --- |
| 0 | 脚手架 + `cards/patterns/moves` + rules 语料 | `pytest -q` 全绿；语料覆盖 rules.md 每条边界 | M |
| 1 | `state/record/counter/archive` + `/ingest` + verify_fairness | 终局 state 重放不变量：计数恒 54、leading 一致、倍数单调、JSONL 往返 | M |
| 2 | 拆解器 + 启发式 + 叫分 + 模拟器/竞技场 | `heuristic vs random ×200` 胜率远超 50%；硬规则单测全绿 | L |
| 3 | **服务 + 油猴脚本（仅启发式）→ 第一个可用悬浮窗** | 真实对局：轮到我 ~1s 内出建议；`帮我选中` 两种排序都正确 | M |
| 4 | DouZero vendoring/bridge/agent/bench | 两道闸通过；`bench_douzero` p95 < 50ms；竞技场 douzero vs heuristic | L |
| 5 | 残局求解器 + 威胁提示 + 理由润色 + 策略分歧 | 求解器 vs ≤10 张暴力枚举一致；残局阶段胜率有可测提升 | M |
| 6（暂缓） | 行为克隆微调（8GB 显存只够小规模）、`/analyze`、独立 GUI、自动出牌、MCTS | — | XL |

Phase 3 结束即可真实使用；Phase 4 是纯增量（失败也自动回退到已验证的启发式）。

## 5. 测试清单（至少覆盖）

- `test_rules_md_corpus.py`：声明式语料——每种牌型；`34567` 合法 / `JQKA2` 非法 / `10JQKA` 合法 / `23456` 非法；`334455` 合法 / `KKAA22` 非法；飞机 2..4 组、带单/带对；翅膀同点非法、含炸弹非法、三张作单翅膀合法；`four_two` 带对合法；`four_two_pair` 带第二个四张非法；王炸；`333444555666` 多读法；`beats` 矩阵（炸 > 四带两对、王炸 > 所有炸）。
- `test_moves.py`：每个返回的 Move 自洽重分类；`legal_beats ∪ {None}` 与跟牌时 `legal_moves` 一致；`≤10 张手牌` 暴力子集枚举交叉验证完备性。
- `test_state_adapter.py` / `test_record.py`：重放不变量（含与 `snapshot()` 语义对照的用例，fixture 用真实抓取状态）。
- `test_decomposer/heuristic/endgame/arena`：硬规则逐条；求解器 vs 暴力；模拟器计分数学（底分×倍数、春天/反春）与确定性（同 seed 同结果）。

## 6. 主要风险与对策

1. **权重不在 git、官方托管下载不便**：README 写明 Drive/百度两种方式；`weights.py` 校验 sha256；缺失时服务以启发式运行（`douzero_loaded:false`）。**不要用来源不明的镜像**（我们要以 `weights_only=False` 反序列化）。
2. **torch ≥ 2.6 反序列化默认值翻转**：显式 `weights_only=False`（仅本地校验过的文件），失败重试 `weights_only=True`。
3. **规则差异**（`3333+4444` 等）：DouZero 只打分不生成 + 差异测试兜底；任何新差异视为 bug。
4. **平台 `choice/patternKey` 格式不可知**（规则引擎缺失）：中文描述自产；自动提交本就不做（符合 Copilot 定位与安全姿态）。
5. **ToS 灰区**：默认仅建议；不改请求、不增频率、不自动点击出牌；「帮我选中」显式 opt-in；单账号自用，风险由用户知悉。
6. **协议抖动**：version 只透传、`unchanged` 不重建、解析失败降级返回、每次整页加载重新 bootstrap；`/health` 探活，服务未启动时浮窗显示「服务未启动」而非静默失败。
7. **数据质量**（低质量对局、托管出牌）：归档记录 `players[i].auto` 与 `match.rating_before`；`eval_records` 支持 `--exclude-auto --min-rating 1500` 过滤（将来训练同用）。
8. **计时**：建议必须远早于 15s；模型启动预热 + 按 `(version,seat)` 缓存 + 响应带 `timing_ms` 与生成时间，过期建议视觉置灰。

## 7. 需要用户准备

1. 运行机（4060）装好 CUDA 版 torch 与 Node；开发机无需 torch。
2. 从官方渠道下载 `douzero_WP` 三件套放入 `third_party/DouZero/baselines/douzero_WP/`。
3. 把已收集的终局 state JSON 放入 `data/raw/`（或后续用脚本从历史回放页批量抓取），供 `eval_records` 验证与调参。

## 8. 首个可运行里程碑（最小验收）

1. `pytest -q` 全绿（Phase 0–2）。
2. `python -m ddz.service` 启动，`GET /health` 返回 `torch:false` 但 `ok:true`。
3. 浏览器安装油猴脚本，打开真实对局页：轮到我时浮窗在 ~1s 内显示推荐牌与理由；`帮我选中` 能正确选中。
4. 一局结束后 `data/games.jsonl` 新增一行，`verify_fairness` 校验通过。
