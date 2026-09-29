## 改了什么

## 为什么

关联 issue：

## 怎么验证的

- [ ] `uv run ruff check .`
- [ ] `uv run pytest -q`
- [ ] `python3 scripts/check_public_tree.py`

补充说明（新增或修改的用例、手动验证步骤）：

## 检查

- [ ] 行为变化已同步到 `docs/` 下对应文档
- [ ] 没有引入凭据、Cookie、真实代理端点、个人绝对路径或真实平台响应
- [ ] 新增依赖已更新 `uv.lock`；新增 vendor 前端资源已更新许可证与 `THIRD-PARTY-NOTICES.md`
