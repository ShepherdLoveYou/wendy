# wendy

Wendy 的大学规划与进度表 · 口令保护的加密静态站点（UC Riverside 转学路线）。

- `index.html` 为 **Staticrypt 加密文件**（PBKDF2-SHA256 + AES-256），没有口令无法读取任何内容——包括查看源代码。
- 明文 HTML **不在本仓库中**，仓库公开也不泄露内容。
- 部署：GitHub Actions（`.github/workflows/deploy.yml`），push 到 `main` 自动发布到 GitHub Pages。

## 更新流程（本地）

1. 修改本地明文 HTML
2. 重新加密：`npx staticrypt@latest "<明文文件>.html" -p "<口令>" -d site_tmp`
3. 将输出重命名为 `index.html` 覆盖本仓库同名文件
4. `git add . && git commit -m "update" && git push`

## 今日助理（`/today/`）

地址：<https://shepherdloveyou.github.io/wendy/today/>（同样需要口令，可以勾选"30 天内记住我"）

每天自动更新 4 次（太平洋时间 00:05 / 06:00 / 12:00 / 18:00），内容包括：

- **下一节课 / 最近的截止**：打开页面时实时计算的两张卡片
- **今天的课**：时间线，带"还有多久上课 / 上课中"；当天公告说取消的课会标出来
- **两周内要交的作业**：按截止日期分组，每项都有实时倒计时，快到期的变橙、变红；逾期的置顶
- **Canvas 之外的固定作业**：Top Hat、iMath 等 Canvas 读不到的，按规律生成，做完可以在页面上自己勾选
- **课程表**：自动从 UCR 选课系统读取上课时间和教室，每天顶部一行是当天的截止；可以翻周、切换日视图和列表
- **公告**：最近的课程公告和摘要；正文里写了截止日期的句子会被自动识别，并加进待办
- **今日简报**（AI 智能体）：先做哪 3 件事、为什么、今天的空档怎么安排、有什么风险
- **和上次比**：新作业、刚交掉的、改了截止时间的、被撤下的、新公告、课表变化（代码算出，不依赖 AI）
- **历史快照**：保留最近 30 天，每天一份，可以翻看

所有时间都按加州时间显示，在别的时区打开也不会看错。页面跟随系统自动切换深色模式。

### 怎么运作

```
GitHub Actions（定时）
  ├─ assistant/build_today.py   Canvas API：作业、测验、公告、选了哪些班（课号里带班号）
  │                              UCR 选课系统（公开查询）：每个班的上课时间、教室、老师
  │                              → 按 assistant/template.html 生成明文页面
  ├─ staticrypt                  用口令加密（固定 salt 在 .staticrypt.json，"记住我"不会失效）
  └─ 部署到 GitHub Pages         明文和课表只存在于构建机器上，不进公开仓库
```

### 今日简报智能体（`assistant/agent.py`）

按业界通行做法设计（参考 Anthropic *Building Effective Agents*）：

- **确定的事实交给代码**：逾期、倒计时、课表、和上次比的变化都由代码算出并有测试；智能体只做需要判断的部分
- **只读工具白名单**：`list_tasks`、`get_schedule`、`get_changes`、`get_task_details`、`list_announcements`，不能提交或修改任何东西
- **结构化输出 + 双重校验**：Pydantic 结构由框架校验，引用不存在的作业会让模型重试一次；代码再过滤一遍（未知作业、和上课冲突或已过去的时间段、超长文字）
- **护栏**：最多 6 次请求、12 次工具调用，单个工具 30 秒超时；主模型失败自动换备用模型（`FallbackModel`）；工具出错返回给模型而不是中断
- **外部内容当数据**：公告正文、作业说明只作为工具结果返回，指令里明确要求忽略其中"让你改变行为"的文字
- **失败安全**：任何失败只是不显示简报，页面其他部分照常
- **可测试**：`tests/test_agent.py` 用 PydanticAI 的 `FunctionModel` 模拟模型，不联网
- **不泄露内容**：日志只记请求数、工具调用数、token 数和耗时

框架 [PydanticAI](https://ai.pydantic.dev)（MIT），模型 Gemini 免费版（`gemini-3.6-flash`，备用 `gemini-3.5-flash-lite`）。

### 快照

每次运行从线上取回上一份加密页面，在构建机器上用口令解密，读出里面嵌的状态来算变化；
这次的加密页面存成当天的快照（`today/archive/YYYY-MM-DD.html`，保留 30 天）。
不用缓存、不产生提交、不在任何地方留明文。

页面用的都是开源组件，从 jsDelivr CDN 加载，不需要构建：
[Tabler](https://tabler.io)（UI 套件，MIT）、[Tabler Icons](https://tabler.io/icons)（MIT）、
[FullCalendar](https://fullcalendar.io)（课程表，MIT，Forma 主题）。

需要的 GitHub Secrets（Settings → Secrets and variables → Actions）：

| Secret | 内容 |
|---|---|
| `CANVAS_TOKEN` | Canvas → Account → Settings → New Access Token（当前的在 2026-12-25 过期） |
| `SITE_PASSWORD` | 网站口令 |
| `GEMINI_API_KEY` | 可选：Google AI Studio 的免费 key（翻译、理解公告、今日简报） |
| `SCHEDULE_TOML` | 补充配置：学期信息、课的备注和链接、Canvas 之外的固定作业（格式见 `assistant/schedule.example.toml`；上课时间会自动读取，不用填） |

### 本地开发

```bash
mise run today            # 生成并打开今日页面（明文，只在本地）
mise run test             # 跑完整测试（状态机、不变量、智能体护栏、快照、出错降级）
mise run encrypt          # 预览加密后的口令页
mise run schedule-upload  # 改完 assistant/schedule.toml 后上传到 Secret 并重新部署
```

本地的 `.env`（已被 git 忽略）里放 `CANVAS_TOKEN`、`SITE_PASSWORD`，可选 `GEMINI_API_KEY`。依赖见 `assistant/requirements.txt`，mise 任务会用 uv 自动安装。

### 注意

- 公开仓库的定时任务在仓库 **60 天没有任何提交** 后会被 GitHub 自动停用，需要到 Actions 页面重新启用。
- Canvas token 过期后页面顶部会显示提示，重新生成后更新 `CANVAS_TOKEN` 即可。
