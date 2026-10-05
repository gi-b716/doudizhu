# 测试样本

`test_fixtures.py` 会把本目录下**每个 `*.json`** 当成一份平台 state 跑完整条流水线：
适配 → 归档建模 → 逐前缀重放不变量（计数恒 54、leading 自洽、倍数单调）→ 发牌承诺复核 →
决策样本合法性。不是终局完美信息 state 的文件会被跳过（只提示，不算失败）。

## 放真实抓包

从浏览器里抓到的**终局** state（`phase == "finished"` 且带 `fairness.deals` 的那种）
直接丢进来即可，文件名随意。抓法二选一：

1. 对局结束后打开 DevTools 的 Network，找 `POST /games/doudizhu/api` 里 `action=state`
   的响应，把 `state` 字段整体存成 `.json`；
2. 或者等 Phase 3 的油猴脚本上线后，用它的 `/ingest --keep-raw` 落地到 `data/raw/`，
   再把需要的几份拷到这里。

有了真实样本，`ddz.state` 里"平台 pattern 对象形状不可知"的那条假设就能被真正验证：
如果平台发的 pattern 恰好能被 `parse_platform_pattern` 认出来，样本里的
`pattern_source` 会变成 `platform`。

## 现在这份

`finished_seed20261005.json` 是随机对局生成的（`tests/gamegen.py`），用作不依赖生成器
后续改动的回归样本。
