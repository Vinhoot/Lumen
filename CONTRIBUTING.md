# 贡献指南

感谢有兴趣改进 Lumen。项目很小，规则也很简单。

## 环境

```bat
py -3.12 -m venv runtime
runtime\Scripts\python -m pip install -r requirements.txt
runtime\Scripts\python main_web.py
```

需要 Windows 10/11 64 位 + WebView2 运行时（Win11 自带）。

## 代码结构

```
main_web.py         入口：HTTP 服务 + pywebview 窗口 + 启动看门狗
app/pyapi.py        JS ↔ Python 桥（约 40 个方法，前端所有数据都从这里走）
app/server.py       静态资源 / 缩略图 / 视频 Range 流
app/db.py           SQLite schema 与查询（含工作区、标签、作者、分组）
app/scanner.py      目录扫描（多线程、过滤非素材图）
app/thumbs.py       缩略图生成（Pillow；视频用 ffmpeg 抽帧）
app/authoring.py    作者识别规则（EXIF / XMP / sidecar / 文件夹名 / 文件名）
app/backup.py       gzip 元数据快照与恢复
app/workspace.py    多工作区管理
app/paths.py        数据目录定位（便携优先）
app/winenv.py       Windows 相关：孤儿进程清理、窗口激活
app/web/            前端（原生 HTML/CSS/JS，无构建步骤）
tools/installer/    Inno Setup 安装器脚本
```

前端没有构建步骤：直接改 `app/web/` 里的文件，刷新窗口即可看到效果。

## 提交前请自查

- **不要提交运行数据**：`data/`、`_tmp/`、`webview2/`、`*.db`、`*.log`、`thumbs/`；
- **不要提交打包产物**：`dist/`、`dist_installer/`、`build/`、`runtime/`；
- **不要提交个人素材或截图原图**：演示素材已程序化生成在 `tools/_promo_media`；
- 提交前跑一次 `runtime\Scripts\python main_web.py`，确认窗口能正常起来；
- 如果改了 `db.py` 的 schema，请保证**旧库能平滑升级**（用 `CREATE TABLE IF NOT EXISTS` /
  `ALTER TABLE` 增量迁移，不要重建表）。

## 提交信息

```
<范围>: <一句话做了什么>

可选：为什么这么改、影响哪些功能。
```

范围可用：`ui` `db` `scan` `thumbs` `backup` `installer` `docs`。

## Issue

- 报 Bug 请带上：Windows 版本、`data\startup.log` 的相关片段（**抹掉用户名与真实路径**）、
  现象描述（白屏 / 崩溃 / 显示异常）；
- 提需求请说清使用场景，而不是只说想要什么按钮。

## License

提交的代码默认以 [MIT](LICENSE) 授权。
