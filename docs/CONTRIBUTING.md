# 贡献指南

感谢你考虑为本项目贡献代码。

## 贡献前须知

本项目有几条**不可破坏的设计红线**，修改前请确认你的改动不会违反它们：

1. **禁止编造数据** — 所有金额必须来自 MCP 原始返回。
   接口没给的信息就显示「未获取到」，不允许估算或猜测。
2. **禁止自动下单** — 不引入任何下单接口的调用。
3. **领券需确认** — 写操作必须先获得用户明确同意。
4. **禁止静默降级** — 任何降级都必须在输出中显式声明。
5. **金额用整数分** — 全程 `int` 计算，仅渲染层除 100。

违反上述任意一条的 PR 会被拒绝，即使功能看起来更好。

---

## 开发环境

```bash
git clone https://github.com/zhangaoyu3940/mcd-deal-finder.git
cd mcd-deal-finder

# 无需安装依赖（零第三方依赖）
python --version   # 需要 3.9+
```

## 开发流程

```bash
# 1. 在你的环境探测 MCP 能力
python mcd-deal-finder/scripts/mcd_client.py --probe

# 2. 跑基线回归，确保起点是绿的
python mcd-deal-finder/scripts/mock_mcp_server.py --run-regression
```

### 目录职责

| 路径 | 职责 | 改动影响 |
|---|---|---|
| `scripts/mcd_client.py` | MCP 适配层：探测、参数适配、字段归一 | 高，涉及所有 MCP 交互 |
| `scripts/deal_optimizer.py` | 优惠计算引擎：叠加、打分、试算 | 高，涉及所有金额计算 |
| `scripts/mock_mcp_server.py` | mock 与回归测试 | 改接口契约时**必须**同步 |
| `references/*.md` | 契约与规则文档 | 需与代码保持一致 |
| `SKILL.md` | Skill 主定义 | 影响 Skill 触发与行为 |

## 提交前必做

```bash
# 1. 语法检查
python -m py_compile mcd-deal-finder/scripts/*.py

# 2. 完整回归（9 场景 + 5 项不变量，必须全绿）
python mcd-deal-finder/scripts/mock_mcp_server.py --run-regression
```

⚠️ **修改任何计算逻辑后必须重跑回归**，并在 PR 描述中附上结果。

## 提交信息规范

采用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
feat: 支持券名反推 buy-送一 类型
fix: 修正 beType=1 时误传 beCode 导致的报错
docs: 补充 calculate-price 金额口径说明
test: 新增 time_conflict 场景回归用例
perf: 候选组合生成去重逻辑优化
```

---

## 提交 Pull Request

1. **开分支**：`feat/xxx` / `fix/xxx` / `docs/xxx`
2. **一 PR 一件事**，不要把无关改动混在一起
3. **PR 描述必须包含**：
   - 改了什么、为什么
   - 回归测试结果截图或输出
   - 是否改动了金额计算逻辑（若是，说明如何验证）
4. 关联相关 Issue

## 需要重点测试的改动

| 改动类型 | 必须验证 |
|---|---|
| 金额计算 | 三层优惠是否自洽；`共省 = 划线价 - 实付` 是否成立 |
| MCP 字段映射 | 用真实接口调用验证，不只依赖 mock |
| 打分公式 | 1 人不应只推饮料；N 人不应推超量方案 |
| 券叠加 | 过期/领完/无金额券必须被排除 |

---

## 报告 Bug

请附上：

- 环境（Python 版本、Agent 运行时、MCP 来源）
- `--probe` 的输出
- 你输入的需求描述
- 实际输出 vs 预期输出
- 完整错误栈（如有）

## 代码风格

- Python 3.9+，类型注解
- 中文注释说明**为什么**，而非复述代码
- 单文件不超过约 1200 行；超出请拆分模块
- 关键金额计算处加不变量断言

---

## 不接受的贡献

- 引入重量级依赖（本项目坚持零依赖）
- 自动下单 / 自动领券等绕过用户确认的功能
- 硬编码菜单价格、优惠券面额（必须运行时获取）
- 移除或削弱「无金额信息不参与计算」的安全降级

---

感谢贡献！🎉