# 微信知识助手桌面壳

Tauri 2 薄壳：负责拉起本机 Python 服务、提供桌面窗口与系统托盘。
前端仍由 `backend/frontend` 提供（`http://127.0.0.1:8721`），这里不复制、不打包前端。

- 关窗口 = 隐藏到托盘，服务继续运行
- 托盘菜单：显示主界面 / 在浏览器打开 / 退出（退出时结束自己拉起的 Python 服务）
- 已有服务在运行时直接附着，不再起第二个进程
- 数据与模型仍保存在仓库的 `data/`、`models/`，不做安装目录迁移

## 构建（Windows）

```bat
cd desktop
npm install
npm run tauri -- build --no-bundle
```

需要 Rust + MSVC（BuildTools）+ WebView2。仓库根目录的 `构建桌面版.bat` 会自动
调用 `vcvars64.bat` 并把产物复制成 `微信知识助手.exe`。

当前机器没有 NSIS，GitHub 也不可达，所以只产出便携 exe；`--no-bundle` 可跳过安装包步骤。
