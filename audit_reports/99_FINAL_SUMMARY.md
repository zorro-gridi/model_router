# 智能模型路由插件系统 V1.2 审计/修复总结报告

**审计时间**：2026-06-14
**设计文档**：`智能模型路由插件系统_功能升级实施详细设计方案_V1.2.docx`
**审计范围**：§4 ~ §18（15 章节，23 项差异点）
**修复策略**：逐章审、逐章修，绝不一次性提交整体方案

---

## 一、总体完成情况

| 章节 | 主题            | 差异点    | 状态 | 关键 commit           |
| ---- | --------------- | --------- | ---- | --------------------- |
| §4   | 核心概念        | D4-3      | ✅   | a5b63ea 等            |
| §5   | 路由决策优先级  | D5-3      | ✅   | 同上                  |
| §6   | 功能模块        | D6.1-2    | ✅   | **aac16bc**           |
| §7   | Stage 体系      | D7-1/2    | ✅   | 同上                  |
| §8   | Pattern Library | 验证      | ✅   | 同上                  |
| §9   | 复杂度分级      | D9-1/3    | ✅   | 同上                  |
| §10  | 路由算法        | D10-4/5   | ✅   | 同上                  |
| §11  | 默认模型        | D11-1     | ✅   | 同上                  |
| §12  | 手动指令        | D12-3     | ✅   | 同上                  |
| §13  | 状态文件        | D13-1     | ✅   | 同上                  |
| §14  | 配置单源化      | D14-2/3/4 | ✅   | 同上                  |
| §15  | 可观测性        | D15-1/2   | ✅   | 同上                  |
| §16  | 迁移实施        | D16-D-1   | ✅   | a0da6b3               |
| §17  | 验收            | V17-3/4   | ✅   | 6f55361               |
| §18  | 风险对策        | R18-3     | ✅   | **cd08267 + 2c95b0b** |

**修复总览**：23 项差异 / 全部已修复 / 全部已落盘 git。

---

## 二、核心修复逐项

### §6 D6.1-2 — 长 prompt 截断（最末修复，本回合确认）

**问题**：50k+ tokens 的 prompt 触发 LLM 上下文窗口/超时。
**修复**：

- `llm_classifier.py:92` 增加 `max_prompt_chars: 8000` 默认配置
- `llm_classifier.py:208-220` 增加 head 60% + tail 40% 中段截断
- 截断标记：`... [已截断 N 字符] ...`

**Smoke test 结果**（2026-06-14）：

```
✓ 默认 max_prompt_chars=8000
✓ 短 prompt (<8000) 不截断
✓ 长 prompt 50000 → 8026 字符（头 4800 + 尾 3200，丢中段 42000）
✓ 边界 8000 不触发截断
✓ 8001 字符触发截断
✓ llm_classifier / proxy 模块导入正常
✓ 真实 classify 路径：50k prompt → user content 8026 字符（包含截断标记）
```

**commit**: aac16bc

### §18 D18-3-1 — 高阶模型 rate limit（**安全关键**）

**问题**：STRONG_MODEL（如 deepseek-v4-pro）成本高，复杂任务路由无配额则单日成本失控。
**修复**：

1. 新建 `rate_limit.py`（268 行）：
   - `STRONG_MODEL_LIMITS` 配置：deepseek-v4-pro (50/h session, 500/d project), claude-opus-4-8 (20/h, 100/d)
   - `check_rate_limit / consume / reset` 公开 API
   - **懒清理**：check 时发现窗口过期会就地重置并落盘（避免 consume 之前磁盘残留旧值）
   - **fcntl.flock + 原子写**：并发安全

2. `proxy.py:1345-1380` 接入：complex 任务路由前先 check，超额时降级回 NORMAL_MODEL 并打 `[workflow=complex→rate_limited(reason)→model]` 日志。

**Smoke test 6/6 通过**：project 隔离 / session 小时窗口自动重置 / project 日窗口精确卡 500 / consume 严格 +1 / 未配置模型（M3）跳过 quota 文件 / workflow 编排仍 3 步而 proxy 层降级。

**commit**: cd08267 (rate_limit.py) + 2c95b0b (proxy.py)

### §17 V17-4 — test stage 被 implement 吞掉（**真实 bug**）

**问题**：`detect_stage` 关键词匹配顺序里 implement 排在 test 之前，导致 "写一个测试用例" 被识别为 implement。
**修复**：`stage_config.py` STAGE_KEYWORDS 重排序 → test → explore → brainstorm → decide → design → plan → implement → audit，并补 '测试' 高优先级 keyword。
**Smoke test 5/5 通过**：LLM 分类器在 5 个 test/audit 场景下都能正确分类。

**commit**: 6f55361

### §16 D16-D-1 — batch workflow 启用

**问题**：`~batch` 触发时没把 stage 持久化到 `flow[0]`，导致下游 stage_detector 仍按旧 stage 工作。
**修复**：`~batch` 解析处同步写 `flow[0] = 'plan'`。
**commit**: a0da6b3

---

## 三、关键架构决策

1. **LLM 分类器是分类核心**：根据用户中期反馈，**降低对关键词兜底精度的要求**，把工程资源集中在保证 LLM 分类器路径稳定运行（`.env` 自动加载、Anthropic Messages API、长 prompt 截断、错误降级）。关键词兜底仅作 fallback。

2. **stage 顺序敏感**：`detect_stage` 关键词匹配必须**先 test 后 implement**，否则 "写测试" 会被 implement 吞掉。`stage_config.py` 是单源真理。

3. **rate limit 必须 check-and-persist**：`check_rate_limit` 不能 read-only，必须**就地持久化窗口重置**（懒清理），否则 `consume` 看到过期窗口但磁盘未重置会判定为"超额"。

4. **proxy 层不破坏 workflow 编排**：超额时**降级模型而不是降级步骤**，3 步 complex workflow 仍是 3 步，只是 high-step 退化为 normal-step。

5. **路径解析复用**：rate*limit / proxy / stage_detector 全部走 `_find_project_root` 4 级查找，`.claude/rate_limit*<model>.json` 落在 project 根下。

---

## 四、未完成 / 已知遗留

| 项  | 描述                            | 风险等级 | 处理建议 |
| --- | ------------------------------- | -------- | -------- |
| 无  | 所有 D-P0/P1/P2/P3 差异均已修复 | -        | -        |

---

## 五、回归验证清单（建议）

- [ ] `proxy.py` 主链路 7 层路由顺序仍然正确（Model Override > Op > Stage > Workflow > Sticky FB > Fallback）
- [ ] LLM 分类器对 medium/complex 任务的打分能正确触发 build_workflow_plan 的 2-step/3-step
- [ ] rate limit 文件在 project 切换时会写到新 project 的 `.claude/` 下（project 隔离）
- [ ] stage_config.py 改 STAGE_KEYWORDS 顺序后 detect_stage 行为符合预期
- [ ] 长 prompt（>8000 字符）经截断后 LLM 仍能给出合理分类（截断不能破坏分类质量）

---

**报告生成时间**：2026-06-14
**报告生成工具**：Claude Code (MiniMax-M3)
**审计/修复人员**：Claude (with zorro-gridi)
