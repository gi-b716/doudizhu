# 斗地主 Copilot

在真实对局中**实时给出可解释的出牌建议**，也可以**代打**（自动叫分 / 自动出牌 / 自动续局）。
代打分两个开关——「自动打牌」（叫分+出牌）与「自动续局」——默认都开，互不影响。
内置无界面模拟器与自对弈竞技场用于验证策略强度。

实施计划见 [`PLAN.md`](PLAN.md)。`src/` 下的平台客户端与规则文档是只读参照，也是语义金标准。

## 现状

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 0 | 脚手架 + 规则引擎（`cards` / `patterns` / `moves`）+ rules 语料 | ✅ |
| 1 | `state` / `record` / `counter` / `archive` + `/ingest` + `verify_fairness` | ✅ |
| 2 | 拆解器 + 启发式 + 叫分 + 模拟器/竞技场 | ✅ |
| 3 | 本地服务 + 油猴浮窗（启发式）+ **自动代打** | ✅ |
| 4 | DouZero vendoring / bridge / agent / bench | ✅（**差真权重**） |
| 5 | 残局求解器 + 威胁提示 + 理由润色 | 待做 |

## 用法

```bash
uv run python -m ddz.service            # 起本地服务（127.0.0.1:17876）
curl http://127.0.0.1:17876/health      # {ok, version, torch, records, ...}

# 归档一份终局 state（油猴脚本在 Phase 3 会自动打这个接口）
curl -X POST http://127.0.0.1:17876/ingest \
     -H 'Content-Type: application/json' \
     -d '{"state": <终局 state>, "game_id": "12345", "keep_raw": true}'

uv run python -m ddz.tools.verify_fairness --records data/games.jsonl

# 自对弈竞技场：胜率 / ADP / 延迟报表 → runs/<名字>/summary.json
uv run python -m ddz.arena.match --players heuristic,random,random --games 200 --seed 7 --rotate
uv run python -m ddz.arena.match --players douzero:WP,heuristic,heuristic --games 300 --seed 11      --rotate --out runs/dz_vs_heur        # Phase 4
# 叫分门槛 A/B：候选 vs 基线，同一桌 1 候选 + 2 基线，配对种子，给 95% 置信区间
uv run python -m ddz.tools.bid_ab --candidate 10 --baseline -2,2,6 --games 6000 --seed 5000
# DouZero 模式（本机有 torch + 权重时）：DouZero 不叫分，门槛作用于它的兜底启发式
uv run python -m ddz.tools.bid_ab --engine douzero:WP --candidate 10 --baseline -2,2,6 --games 3000
```

### 装浮窗（Phase 3 起可用）

1. 起服务：`uv run python -m ddz.service`（要一直开着；关掉后浮窗会显示「服务未启动」）。
2. 浏览器装好 Tampermonkey / Violentmonkey，新建脚本，把 `userscript/ddz-copilot.user.js`
   整个粘进去并保存。
3. 打开真实对局页（`/games/doudizhu/...`）：右下角出现浮窗。轮到你时约 10–40ms 内出建议
   （实测 p95 37ms，远早于 15 秒时限）；不是你的回合时显示「等待 X 出牌…」并把旧建议置灰，
   同时照常给记牌器与威胁提示。
4. 浮窗工具栏上有两个引擎可选：**启发式**（现在就可用）和 **DouZero**（Phase 4 权重就位后可用）。
   选中 DouZero 但环境没装好时，面板会说明原因并提示切回启发式，而不是静默失败。
   服务端的 `auto` 会在 DouZero 注册后自动切过去，不需要改脚本。
5. **代打**是两个独立开关，默认都开，互不影响（所以「只续局不代打」「只代打不续局」都配得出来）：
   「**自动打牌**」管叫分 + 出牌，「**自动续局**」管终局后点「再来一局」。
   四条护栏：只按页面自己的按钮（绝不自己构造请求打给游戏接口）、出手前留约 0.7 秒可视延迟
   且局面一变就作废、选完牌要核对 DOM 里真的选中了建议的牌（对不上就放弃）、同一局面只动一次手。
   动作没做成（按钮/手牌还没渲染出来）会自动重试，并把原因显示在状态行上。
6. 「帮我选中」是手动版：点一下才替你在手牌上做选择。
7. 一局结束后自动归档到 `data/games.jsonl`（同一局重复抓到只留一行）。

### DouZero（Phase 4）

```bash
uv pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU 够用，4060 也可装 cu128
git clone https://github.com/kwai/DouZero.git third_party/DouZero       # 已 clone，commit 记在 COMMIT.txt
# 然后把官方 Drive / 百度网盘下的三件套放进 third_party/DouZero/baselines/douzero_WP/
uv run python -m ddz.tools.bench_douzero --games 10                     # 验收线：p95 < 50ms
```

⚠️ **装了 torch 之后别再裸跑 `uv sync`**——它会把这不在 lockfile 里的包当多余依赖删掉。
继续用 `uv sync --inexact`（或者干脆不管，torch 是可选依赖）。

权重没到位时**不会报错**：`/health` 报 `douzero_loaded:false`，浮窗上选 DouZero 会说明原因并
提示切回启发式，所有建议照常由启发式给出。权重一放好，`auto` 会自动切到 DouZero（脚本不用改）。

用随机初始化的替身权重验过整条管道（装载 1.4s；248 个真实局面 p50 0.5ms / p95 1.5ms，
CPU）——**那只证明管道通，不证明棋力**；棋力要等真权重。

### 已知战绩（seed 7，200 局，座位轮换）

| 对局 | 结果 |
| --- | --- |
| `heuristic` vs `random` ×2 | 胜率 **87.5%**，ADP +2.74，决策 p95 1.7ms |
| `heuristic` ×3 自对弈 | 地主胜率 45.0%（接近真实对局的量级），平均倍数 1.24 |

计分以 `src/rules.md` 为准：地主 ±2×底分×倍数、每位农民 ∓底分×倍数（PLAN §3.2 的示例差一个
系数 2，已确认按 rules.md；实现只有 `ddz/arena/simulator.py:score_deltas` 一处）。

启发式权重在 `ddz.engine.heuristic.Weights`（默认值取自 PLAN §3.3）。

**规则引擎已被平台自己的引擎验证过**：`src/doudizhu-rules.js`（平台 `UOJDoudizhuRules.php`
的镜像，服务器说了算）到手后，`tests/test_platform_parity.py` 在 node 里跑它，与我们的引擎比对
**全部读法（含顺序）、14×14 种两两压牌、patternKey 逐字**。自动出牌提交的 `choice` 就是
`Pattern.platform_key()`（平台格式是 `类型:最大点数:长度`，类型名是 `trio1`/`plane1` 那套）。

两条纪律（都是踩过坑之后加的）：

- **`version` 只在一局之内递增**，换局会从 0 重来。任何按 version 做键的缓存（建议缓存、去重）
  都必须带上局标识——本项目的做法是浮窗把页面 `#ddz` 的 `data-game` 一起发过来
  （`/advise` 的 `game_id`），服务端换局即清空该会话的局面与全部缓存；没给 `game_id` 时靠
  version 回退兜底。
- **上家是队友不等于一律不出**：队友 ≤2 张绝不压；队友还没到收尾阶段时，可以把「拆解里本来
  就要打的孤张」顺手放掉（牌权还在自家）；我三手内走完时接管；其余情况压队友要付"抢牌权"代价。

**启发式就此定稿**：它是基准引擎、也是 DouZero 不可用时的兜底，不再做权重网格搜索之类的打磨。
两个引擎的分工是「启发式（永远可用）」与「DouZero（更强，但要有 torch + 权重）」，
浮窗上直接切换；服务端 `auto` 在有 DouZero 时自动用它。

真实抓到的终局 state 放进 `tests/fixtures/` 就会自动进测试流水线（见该目录的 README）。

## 环境

用 [uv](https://docs.astral.sh/uv/) 管理虚拟环境（开发机无 GPU / 无 torch 也能跑除 DouZero 外全部）：

```bash
uv sync                 # 建 .venv 并装齐依赖（含 dev 组）
uv run pytest           # 规则引擎与后续各阶段的测试
```

运行机（RTX 4060 Laptop, 8GB）追加：

```bash
uv pip install torch --index-url https://download.pytorch.org/whl/cu128
uv pip install -r requirements-cu.txt   # 等价写法，见下
```

想用 pip 而不是 uv 时：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` 由 `uv export --no-hashes --no-dev -o requirements.txt` 生成，
`requirements-cu.txt` 在其上追加 CUDA 版 torch。**不要** `pip install douzero`：PyPI 上的
1.1.0 是无依赖声明的 sdist，会拉进已死的 `gitdb2`；DouZero 一律 clone 仓库后按路径 import。

```bash
git clone https://github.com/kwai/DouZero.git third_party/DouZero
git -C third_party/DouZero rev-parse HEAD > third_party/DouZero/COMMIT.txt   # 锁定版本
# 从官方 Google Drive / 百度网盘下载 douzero_WP 三件套（landlord / landlord_up / landlord_down）
# 放入 third_party/DouZero/baselines/douzero_WP/
```

权重不在 git 里；缺失或没有 torch 时服务照常启动、`/health` 报 `torch:false`，全部建议由启发式给出。

## 目录

```
ddz/
  cards.py patterns.py moves.py   规则引擎：牌、牌型、合法着法（合法性的唯一来源）
  state.py record.py              平台 state → 内部状态；对局库建模与逐前缀重放
  counter.py explain.py           记牌器/威胁提示；全部中文文案
  engine/                         拆解器、启发式、叫分、残局求解器
  douzero/                        上游代码按路径装载、观测桥接、打分
  data/archive.py                 games.jsonl 归档（按发牌去重）
  service/                        FastAPI：/health /ingest（Phase 3 加 /advise）
  tools/                          verify_fairness / bench_douzero / eval_records
  agents/ arena/
userscript/ddz-copilot.user.js    油猴浮窗（Shadow DOM）
src/                              平台客户端与规则文档（只读参照）
tests/                            语料 + 单测 + fixtures（真实抓包放这里）
data/ runs/ third_party/          对局库、竞技场输出、DouZero（均 gitignore）
```

## 设计要点

- **合法着法永远由 `ddz.moves` 生成**，DouZero 只对给定动作打分。规则差异、叫分、流局、
  版本抖动全部不进模型。
- 牌 id 与平台一致（`id = rank * 4 + suit`，52/53 为两张王），零翻译；`rank` 把两张王拆成
  13/14，因为牌型比较要求点数全序。
- 与上游的规则差异**只有两类**，都是 rules.md 的规定、我们有意照它实现：① 生成时不拆炸弹当翅膀；
  ② 翅膀里不能带王炸。PLAN §0.2 里说的「上游四带两对允许 `3333+4444`」经核对**不成立**
  （上游 `gen_type_14_4_22` 要求两个翅膀点数不同），闸二把真实的差异类别钉住了。
- **规则引擎被平台自己的实现验证过**：`src/doudizhu-rules.js` 在 node 里跑，与我们的引擎比对
  全部读法、14×14 两两压牌、`patternKey` 逐字（`tests/test_platform_parity.py`）。
- **DouZero 接的是上游原版**：按文件路径装载叶子模块（绕开 `dmc/__init__.py` 里那条 `import git`
  的死依赖），观测**直接调用上游 `get_obs()`**，合法动作永远由 `ddz.moves` 生成。两道正确性闸：
  ① 用上游 `GameEnv` 重放同一局、逐决策点比对观测（`test_douzero_bridge.py`）；② 与上游
  `MovesGener` 逐手对照合法集（`test_douzero_legality_parity.py`）。
- **合法性口径 vs 生成口径**：`ddz.patterns.classify` 认牌组本身的一切合法读法（含「拿炸弹里的
  两张当翅膀」）；而 `ddz.moves` 生成候选时**绝不拆炸弹、绝不拆出王炸**当翅膀——这与上游
  DouZero `MovesGener` 的口径一致，也让「炸弹是一手牌」这个直觉不被自家候选集破坏。
  生成集永远是合法集的子集（随机对照暴力枚举已验证）。
