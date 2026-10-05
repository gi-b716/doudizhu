# 斗地主 Copilot

在真实对局中**实时给出可解释的出牌建议**——只建议，不代打、不自动提交。内置无界面模拟器与
自对弈竞技场用于验证策略强度。

实施计划见 [`PLAN.md`](PLAN.md)。`src/` 下的平台客户端与规则文档是只读参照，也是语义金标准。

## 现状

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 0 | 脚手架 + 规则引擎（`cards` / `patterns` / `moves`）+ rules 语料 | ✅ |
| 1 | `state` / `record` / `counter` / `/ingest` + `verify_fairness` | 待做 |
| 2 | 拆解器 + 启发式 + 叫分 + 模拟器/竞技场 | 待做 |
| 3 | 本地服务 + 油猴浮窗（仅启发式）→ 第一个可用版本 | 待做 |
| 4 | DouZero vendoring / bridge / agent / bench | 待做 |
| 5 | 残局求解器 + 威胁提示 + 理由润色 | 待做 |

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
  explain.py                      中文文案集中处
  engine/                         拆解器、启发式、叫分、残局求解器
  douzero/                        上游代码按路径装载、观测桥接、打分
  agents/ arena/ data/ service/ tools/
userscript/ddz-copilot.user.js    油猴浮窗（Shadow DOM）
src/                              平台客户端与规则文档（只读参照）
tests/                            语料 + 单测
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
