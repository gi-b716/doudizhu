# 斗地主 Copilot

在真实对局中**实时给出可解释的出牌建议**——只建议，不代打、不自动提交。内置无界面模拟器与
自对弈竞技场用于验证策略强度。

实施计划见 [`PLAN.md`](PLAN.md)。`src/` 下的平台客户端与规则文档是只读参照，也是语义金标准。

## 现状

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 0 | 脚手架 + 规则引擎（`cards` / `patterns` / `moves`）+ rules 语料 | ✅ |
| 1 | `state` / `record` / `counter` / `archive` + `/ingest` + `verify_fairness` | ✅ |
| 2 | 拆解器 + 启发式 + 叫分 + 模拟器/竞技场 | ✅ |
| 3 | 本地服务 + 油猴浮窗（仅启发式）→ 第一个可用版本 | ✅ |
| 4 | DouZero vendoring / bridge / agent / bench | 待做 |
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
```

### 装浮窗（Phase 3 起可用）

1. 起服务：`uv run python -m ddz.service`（要一直开着；关掉后浮窗会显示「服务未启动」）。
2. 浏览器装好 Tampermonkey / Violentmonkey，新建脚本，把 `userscript/ddz-copilot.user.js`
   整个粘进去并保存。
3. 打开真实对局页（`/games/doudizhu/...`）：右下角出现浮窗。轮到你时约 10–40ms 内出建议
   （实测 p95 37ms，远早于 15 秒时限）；不是你的回合时显示「等待 X 出牌…」并把旧建议置灰，
   同时照常给记牌器与威胁提示。
4. 「帮我选中」是**显式 opt-in**：点一下才会替你在手牌上做选择（也可以勾上「轮到我时自动选」）。
   它绝不点「出牌」、绝不对游戏接口发任何请求——出不出牌永远是你自己的事。
5. 一局结束后自动归档到 `data/games.jsonl`（同一局重复抓到只留一行）。

### 已知战绩（seed 7，200 局，座位轮换）

| 对局 | 结果 |
| --- | --- |
| `heuristic` vs `random` ×2 | 胜率 **85.5%**，ADP +2.40，决策 p95 1.7ms |
| `heuristic` ×3 自对弈 | 地主胜率 43.0%（接近真实对局的量级），平均倍数 1.24 |

计分以 `src/rules.md` 为准：地主 ±2×底分×倍数、每位农民 ∓底分×倍数（PLAN §3.2 的示例差一个
系数 2，已确认按 rules.md；实现只有 `ddz/arena/simulator.py:score_deltas` 一处）。

启发式权重都在 `ddz.engine.heuristic.Weights`（默认值取自 PLAN §3.3）；调参时用竞技场看胜率、
用 `ddz.tools.eval_records`（Phase 3 起）看与人类着法的一致率。

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
- 与上游唯一的有意规则差异：上游「四带两对」允许 `3333+4444`，rules.md 禁止，由我们的合法集拦住。
- **合法性口径 vs 生成口径**：`ddz.patterns.classify` 认牌组本身的一切合法读法（含「拿炸弹里的
  两张当翅膀」）；而 `ddz.moves` 生成候选时**绝不拆炸弹、绝不拆出王炸**当翅膀——这与上游
  DouZero `MovesGener` 的口径一致，也让「炸弹是一手牌」这个直觉不被自家候选集破坏。
  生成集永远是合法集的子集（随机对照暴力枚举已验证）。
