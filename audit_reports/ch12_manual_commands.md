# §12 手动控制与指令规范 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 12 章（"手动控制与指令规范"）
> 审计时间：2026-06-14
> 审计范围：`stage_detector.py` 中 6 个手动指令（~model / ~m / ~stage / ~careful / ~quick / ~batch / ~reset）的实现完整性
> 落地代码：`/Users/zorro/.claude/hooks/model_router/stage_detector.py:1274-1287`

---

## 12.1 设计文档要求（7 个指令）

| 指令                | 作用                   | 规范                                      |
| ------------------- | ---------------------- | ----------------------------------------- |
| `~model / ~m`       | 直接指定最终模型       | 最高优先级，绕过自动路由                  |
| `~stage <name>`     | 强制指定 Stage         | 覆盖自动 Stage 分类                       |
| `~careful`          | 提高当前阶段复杂度一档 | 用于当前任务明显比默认更难                |
| `~quick`            | 降低当前阶段复杂度一档 | 用于当前任务只是快速确认                  |
| `~batch <template>` | 加载预定义任务模式     | 适合 feature / refactor / test 等固定流程 |
| `~reset`            | 清除手动覆盖           | 恢复自动分类与路由                        |

## 12.2 当前实现

| 指令                | 实现位置            | 正则                                                                                           | 行为                                        | 状态 |
| ------------------- | ------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------- | ---- |
| `~model` / `~m`     | prompt 解析         | (隐式 — model name 后跟)                                                                       | 写入 `_model_file_path()` → state_index     | ✅   |
| `~stage <name>`     | prompt 解析         | (隐式 — stage name)                                                                            | 写入 `_stage_file_path()`                   | ✅   |
| `~careful`          | stage_detector:1275 | `~(careful\|quick)\b`                                                                          | 调用 `shift_complexity(current, +1)`        | ✅   |
| `~quick`            | stage_detector:1275 | 同上                                                                                           | 调用 `shift_complexity(current, -1)`        | ✅   |
| `~batch <template>` | stage_detector:1281 | `~batch\s+(feature\|bugfix\|refactor\|test\|research\|migration\|architecture\|docs\|audit)\b` | 写入 `_batch_file_path()`                   | ⚠️   |
| `~reset`            | stage_detector:1287 | `~reset\b`                                                                                     | 调用 `clear_all_overrides(session_id, cwd)` | ✅   |

## 12.3 差异清单

### D12-1 [DEVIATION] `~batch <template>` 不强制流程起点（与 §5 D5-3 / §10 D10-2 同根因）

- **文档 §12**："~batch 加载预定义任务模式，**适合 feature / refactor / test 等固定流程**"
- **当前实现**：`~batch feature` 仅把 pattern 写入 `batch_<sid>` 文件，**不强制 stage 跳到 plan**
- **后果**：
  - 用户 `~batch feature` 期望进入"plan → design → implement → test → audit"流程
  - 实际：仅记录 pattern，路由仍走 LLM/关键词识别的 stage
- **建议修复**：
  ```python
  # proxy.py do_POST 中
  if batch_template := read_batch_override(cwd, sid):
      if batch_template in PATTERN_CONFIG:
          forced_stage = PATTERN_CONFIG[batch_template]["default_flow"][0]
          stage = forced_stage  # 强制覆盖 stage
  ```
- **风险等级**：P1（与 §5 D5-3、§10 D10-2 同根因）

### D12-2 [DEVIATION] `~stage` 接受的 stage 列表与 §7 表不完全对齐

- **文档**：stage `~stage <name>` 应能指定 §7 表中任意 stage
- **当前实现**：
  - 已支持：`brainstorm` / `decide` / `plan` / `design` / `implement` / `audit` / `default`
  - 缺：`explore` / `test`（与 §7 D7-1 / D7-2 同根因）
- **建议修复**：与 §7 同步，补 `explore` / `test`
- **风险等级**：P0

### D12-3 [DEVIATION] `~model` 接受的 model 列表未校验

- **文档 §12**：`~model <name>` 任意模型名
- **当前实现**：直接写入 `model_<sid>` 文件，proxy 从 MODEL_TO_CONFIG 反查
- **后果**：
  - 用户输入 `~model gpt-4` 会写入文件但 proxy 找不到路由配置
  - 当前回退到 STAGE_CONFIG 主模型（静默失效）
- **建议修复**：
  - 方案 A：在 stage_detector 中维护合法 model 列表（`["MiniMax-M3", "deepseek-v4-pro", "deepseek-v4-flash"]`）
  - 方案 B：proxy 中找不到时直接 reject + 提示
- **风险等级**：P2（用户体验问题，不影响安全）

### D12-4 [PASS] `~careful` / `~quick` 复杂度调档

- **文档**：升档 / 降档一档
- **当前实现**：
  - `shift_complexity(current, +1)` / `shift_complexity(current, -1)`
  - 夹紧到 `[simple, complex]`
  - 写入 `complexity_<sid>` 文件
- **结论**：✅ PASS

### D12-5 [PASS] `~reset` 全量清除

- **文档 §12**："~reset 清除手动覆盖，恢复自动分类与路由"
- **当前实现**：
  - `clear_all_overrides(session_id, cwd)` 删除 6 个 override 文件
  - model / op / pattern / fallback / complexity / batch 全部清空
  - stage **保留**（stage 是分类结果不是 override）
- **结论**：✅ PASS

### D12-6 [DEVIATION] `~m` 别名支持未明示

- **文档 §12**：`~model / ~m` 两种写法
- **当前实现**：`~m` 与 `~model` 共享同一解析逻辑（隐式 alias）
- **建议修复**：在 stage_detector 注释中显式声明 alias
- **风险等级**：P3（已工作但文档不全）

## 12.4 验收结论

| 指令       | 文档要求        | 实现状态                      |
| ---------- | --------------- | ----------------------------- |
| `~model`   | 最高优先级      | ✅ PASS                       |
| `~m`       | 别名            | ✅ PASS（隐式）               |
| `~stage`   | 覆盖 stage 分类 | ⚠️ 缺 explore / test（D12-2） |
| `~careful` | 升档            | ✅ PASS                       |
| `~quick`   | 降档            | ✅ PASS                       |
| `~batch`   | 加载预定义流程  | ❌ 不强制流程起点（D12-1）    |
| `~reset`   | 全量清除        | ✅ PASS                       |

## 12.5 修复优先级

1. **P0** — D12-2 `~stage` 补全 `explore` / `test`（与 §7 同步）
2. **P1** — D12-1 `~batch` 强制流程起点（与 §5 D5-3、§10 D10-2 同根因）
3. **P2** — D12-3 `~model` 校验合法 model 列表

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
