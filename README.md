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
