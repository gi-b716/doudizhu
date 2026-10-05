"""斗地主 Copilot：规则引擎 + 无界面对局模拟器 + 实时出牌建议（只建议，不代打）。

分层（见 PLAN §1）：

- :mod:`ddz.cards` / :mod:`ddz.patterns` / :mod:`ddz.moves` —— 规则引擎，合法性的唯一来源；
- :mod:`ddz.state` / :mod:`ddz.record` —— 平台 state ↔ 内部状态、对局库；
- :mod:`ddz.engine` —— 拆解器、启发式 agent、叫分、残局求解器；
- :mod:`ddz.douzero` —— 上游 DouZero 的按路径装载与打分（无 torch 时整体降级）；
- :mod:`ddz.arena` —— 无头自对弈评测；
- :mod:`ddz.service` —— 本地 FastAPI 服务（``python -m ddz.service``）。
"""

__version__ = "0.1.0"
