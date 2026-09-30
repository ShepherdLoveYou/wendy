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

- **今天和明天的课**：带"还有多久上课 / 上课中"；当天公告说取消的课会标出来
- **两周内要交的作业**：按截止日期分组，每项都有实时倒计时，快到期的变橙、变红
- **逾期未交**：Canvas 上标为缺交的作业置顶
- **Canvas 之外的固定作业**：Top Hat、iMath 等 Canvas 读不到的，按规律生成，做完可以在页面上自己勾选
- **公告**：最近的课程公告和摘要；正文里写了截止日期的句子会被自动识别，并加进待办

### 怎么运作

```
GitHub Actions（定时）
  ├─ assistant/build_today.py   从 Canvas API 读作业、测验、公告 + 读课表 → 生成明文页面
  ├─ staticrypt                  用口令加密（固定 salt 在 .staticrypt.json，"记住我"不会失效）
  └─ 部署到 GitHub Pages         明文和课表只存在于构建机器上，不进公开仓库
```

需要的 GitHub Secrets（Settings → Secrets and variables → Actions）：

| Secret | 内容 |
|---|---|
| `CANVAS_TOKEN` | Canvas → Account → Settings → New Access Token（当前的在 2026-12-25 过期） |
| `SITE_PASSWORD` | 网站口令 |
| `SCHEDULE_TOML` | 课表，`assistant/schedule.toml` 的全部内容（格式见 `assistant/schedule.example.toml`） |

### 本地开发

```bash
mise run today            # 生成并打开今日页面（明文，只在本地）
mise run encrypt          # 预览加密后的口令页
mise run schedule-upload  # 改完 assistant/schedule.toml 后上传到 Secret 并重新部署
```

本地的 `.env`（已被 git 忽略）里放 `CANVAS_TOKEN` 和 `SITE_PASSWORD`。只用 Python 标准库，不需要装依赖。

### 注意

- 公开仓库的定时任务在仓库 **60 天没有任何提交** 后会被 GitHub 自动停用，需要到 Actions 页面重新启用。
- Canvas token 过期后页面顶部会显示提示，重新生成后更新 `CANVAS_TOKEN` 即可。
