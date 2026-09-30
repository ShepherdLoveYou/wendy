<p align="center">
  <img src="purrfessor/assets/logo.svg" width="96" alt="">
</p>

<h1 align="center">Wendy's Today / Wendy 的今日</h1>

<p align="center">
  <a href="https://github.com/ShepherdLoveYou/wendy/actions/workflows/deploy.yml"><img src="https://img.shields.io/github/actions/workflow/status/ShepherdLoveYou/wendy/deploy.yml?branch=main&label=deploy&logo=githubactions&logoColor=white&style=flat-square" alt="Deploy"></a>
  <img src="https://img.shields.io/badge/%F0%9F%94%92%20page-AES--256-2E7D32?style=flat-square" alt="AES-256 encrypted">
  <a href="https://github.com/ShepherdLoveYou/purrfessor"><img src="https://img.shields.io/badge/powered%20by-Purrfessor%20%C2%B7%20%E5%96%B5%E6%95%99%E6%8E%88-2f6fd6?style=flat-square" alt="Powered by Purrfessor"></a>
</p>

Wendy's college plan and UCR daily dashboard: a password-protected, encrypted static site, built with
[Purrfessor · 喵教授](https://github.com/ShepherdLoveYou/purrfessor).

Wendy 的大学规划和 UCR 每日看板：口令保护的加密静态网站，基于 [Purrfessor · 喵教授](https://github.com/ShepherdLoveYou/purrfessor)。

| Page / 页面 | URL / 地址 |
|---|---|
| **Home / 首页**: college plan and progress / 大学规划与进度表 | <https://shepherdloveyou.github.io/wendy/> |
| **Today / 今日**: classes, to-dos, announcements, grades, AI brief / 课、作业、公告、成绩、AI 简报 | <https://shepherdloveyou.github.io/wendy/today/> |

Both pages are password-protected; tick *remember me* to skip the password for 30 days. The daily page is rebuilt 4 times a day
(US Pacific 00:05 / 06:00 / 12:00 / 18:00); see [Purrfessor](https://github.com/ShepherdLoveYou/purrfessor) for
what is on it.

两个页面都有口令保护，可以勾选"30 天内记住我"。今日页面每天自动更新 4 次（太平洋时间 00:05 / 06:00 / 12:00 / 18:00），
页面上有什么见[喵教授的说明](https://github.com/ShepherdLoveYou/purrfessor)。

## Everyday tasks / 常用操作

- **Change the daily page's settings** (course names, class notes, weekly tasks, holidays): edit the local,
  git-ignored `purrfessor.toml`, then run `purrfessor config-upload` to upload it and redeploy.<br>
  **改今日页面的设置**（课程简称、课的备注、每周固定作业、假期）：编辑本地的 `purrfessor.toml`（私密，不进仓库），
  然后运行 `purrfessor config-upload`，会上传并重新部署。
- **Update the home page**: re-encrypt the plaintext HTML from the repo root (the salt in `.staticrypt.json` is
  reused), replace `site/index.html` with the output, then commit and push. Never commit the plaintext.<br>
  **更新首页**：在仓库根目录重新加密明文 HTML（会沿用 `.staticrypt.json` 里的盐），用输出覆盖 `site/index.html`，
  提交并推送。明文 HTML 不要放进仓库。
  ```bash
  npx staticrypt@3 "<plaintext / 明文>.html" -d site_tmp --remember 30   # asks for the password / 会提示输入口令
  ```
- **Update the framework**: `git pull --no-rebase upstream main` (upstream = ShepherdLoveYou/purrfessor). This
  README is kept by `merge=ours` in `.gitattributes`; run `git config merge.ours.driver true` once per clone.<br>
  **更新喵教授框架**：`git pull --no-rebase upstream main`（upstream 是 ShepherdLoveYou/purrfessor）。
  这份 README 不会被覆盖：`.gitattributes` 里设置了 `merge=ours`，新的电脑上先运行一次 `git config merge.ours.driver true`。
- **Run the tests locally**: `mise run setup && mise run test`.<br>
  **本地测试**：`mise run setup && mise run test`。

## GitHub settings / GitHub 上的设置

| Name / 名称 | Kind / 类型 | What for / 用途 |
|---|---|---|
| `CANVAS_TOKEN` | secret | Reads Canvas / 读取 Canvas |
| `SITE_PASSWORD` | secret | Unlocks the daily page / 今日页面的口令 |
| `GEMINI_API_KEY` | secret | Translation, announcements, today's brief / 翻译、理解公告、今日简报 |
| `PURRFESSOR_CONFIG` | secret | `purrfessor.toml` |
| `PURRFESSOR_ENABLED` | variable / 变量 | `true` = deploy / 部署开关 |
