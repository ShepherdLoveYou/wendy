<p align="center">
  <img src="purrfessor/assets/logo.svg" width="96" alt="">
</p>

<h1 align="center">Wendy 的今日</h1>

<p align="center">
  Wendy 的大学规划和 UCR 每日看板 · 口令保护的加密静态网站<br>
  基于 <a href="https://github.com/ShepherdLoveYou/purrfessor">Purrfessor · 喵教授</a>
</p>

<p align="center"><a href="#english">English</a> · <a href="#中文">中文</a></p>

## 中文

| 页面 | 地址 |
|---|---|
| **首页**：大学规划与进度表 | <https://shepherdloveyou.github.io/wendy/> |
| **今日**：课、作业、公告、成绩、AI 简报 | <https://shepherdloveyou.github.io/wendy/today/> |

两个页面都需要口令，可以勾选"30 天内记住我"。今日页面每天自动更新 4 次（太平洋时间 00:05 / 06:00 / 12:00 / 18:00），
页面上有什么见[喵教授的说明](https://github.com/ShepherdLoveYou/purrfessor#中文)。

### 常用操作

- **改今日页面的设置**（课程简称、课的备注、每周固定作业、假期）：编辑本地的 `purrfessor.toml`（私密，不进仓库），
  然后运行 `purrfessor config-upload`，会上传并重新部署。
- **更新首页**：修改本地的明文 HTML，在仓库根目录重新加密（会沿用 `.staticrypt.json` 里的盐），
  再用输出覆盖 `site/index.html`，提交并推送：
  ```bash
  npx staticrypt@3 "<明文文件>.html" -d site_tmp --remember 30   # 会提示输入口令
  ```
  明文 HTML 不要放进仓库。
- **更新喵教授框架**：`git pull upstream main`（upstream 是 ShepherdLoveYou/purrfessor）。
  这份 README 不会被覆盖：`.gitattributes` 里设置了 `merge=ours`，新的电脑上先运行一次
  `git config merge.ours.driver true`。
- **本地测试**：`mise run setup && mise run test`。

GitHub 上的设置：Secrets `CANVAS_TOKEN`、`SITE_PASSWORD`、`GEMINI_API_KEY`、`PURRFESSOR_CONFIG`，
变量 `PURRFESSOR_ENABLED = true`。

## English

| Page | URL |
|---|---|
| **Home**: college plan and progress | <https://shepherdloveyou.github.io/wendy/> |
| **Today**: classes, to-dos, announcements, grades, AI brief | <https://shepherdloveyou.github.io/wendy/today/> |

Both pages need the password. The daily page is rebuilt 4 times a day by
[Purrfessor](https://github.com/ShepherdLoveYou/purrfessor#english).

- **Change the daily page's settings**: edit the local, git-ignored `purrfessor.toml`, then run
  `purrfessor config-upload`.
- **Update the home page**: re-encrypt the plaintext HTML from the repo root (the salt in `.staticrypt.json` is
  reused), replace `site/index.html` with the output, then commit and push. Never commit the plaintext.
- **Update the framework**: `git pull upstream main`. This README is kept by `merge=ours` in `.gitattributes`
  (run `git config merge.ours.driver true` once per clone).
