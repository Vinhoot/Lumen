# Lumen · 本地媒体库

一个轻量的 Windows 桌面应用，把散落在电脑各处的图片与视频整理成可浏览、可筛选、
可打标签的本地媒体库。macOS 风格的「液态玻璃」界面，纯本地运行，不上传任何数据。

> Lumen 是光的单位。散在各处的照片和视频，先在这里被点亮，再被找回来。

> 装好压包、只想快点用起来？直接看 [USER-GUIDE.md](USER-GUIDE.md)。

## 文档

- [USER-GUIDE.md](USER-GUIDE.md)：怎么装、怎么用、数据在哪、打不开怎么办
- [docs/FEATURES.zh-CN.md](docs/FEATURES.zh-CN.md)：完整功能清单、快捷键、技术规格

## 特性

- **两种添加方式**：多选添加文件，或扫描文件夹（递归、可记住扫描源、支持一键重扫）
- **按文件夹 / 日期 / 作者 / 类型分组**，日期粒度可自定义为年 / 月 / 日
- **网格 + 列表双视图**，缩略图大小随意调节（140–360px），缩略图懒加载 + 失败重试
- **作者识别**：读取 EXIF Artist / XMP creator / 视频元数据，配合 sidecar 与文件夹名
  推断，拿不准的自动进「未识别作者」视图，右键即可批量归属
- **标签系统**：自定义名称与颜色，侧栏 Ctrl+点击多选（AND 筛选），右键菜单 / 批量
  操作条 / 三态勾选快速增删
- **应用内预览**：双击或空格进入全屏预览，视频流式播放（HTTP Range，可拖进度条），
  ←/→ 切换，一键在系统中打开或移到回收站
- **右键菜单无处不在**：媒体、空白处、侧栏的文件夹 / 作者 / 标签均有对应菜单，
  支持子菜单与键盘操作（↑↓ 移动、→ 展开子菜单、Enter 执行、Esc 关闭）
- **批量操作**：框选、Ctrl/Shift 多选、Ctrl+A 全选，批量打标签 / 设作者 / 打开 / 删除
- **删除更安心**：单文件或整个文件夹移到 Windows 回收站（可还原），也可仅移出媒体库
- **工作区**：把不同来源的媒体隔离开（如「工作」「生活」），互不干扰
- **自动备份**：退出时自动做 gzip 元数据快照（保留最近 5 份），右键备份按钮可查看
  历史并一键恢复
- **多尺寸自适应**：880px 到全屏均可用，窄屏下侧栏自动转为抽屉

## 运行环境

- Windows 10/11（依赖 WebView2 Runtime，Win11 自带；Win10 如缺失会自动提示安装）
- Python 3.10+

## 源码运行

```bat
:: 1. 创建虚拟环境（可选但推荐）
py -3.12 -m venv runtime
runtime\Scripts\python -m pip install -r requirements.txt

:: 2. 启动
runtime\Scripts\python main_web.py
```

## 打包

```bat
:: 绿色版（onedir，免安装）：产物 dist\Lumen\Lumen.exe
runtime\Scripts\python -m PyInstaller mediavault_web.spec --noconfirm

:: 单文件安装程序（Inno Setup 6，需先装 ISCC.exe）：产物 dist_installer\LumenSetup.exe
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" tools\installer\lumen.iss
```

## 数据与隐私

- 媒体库索引存放在程序目录的 `data\`（便携模式）；目录不可写时回退到
  `%APPDATA%\Lumen`。**不复制、不移动你的媒体文件**，只记录索引与缩略图。
- 内嵌 HTTP 服务只监听 `127.0.0.1` 随机端口，仅供应用自身加载页面与媒体流，
  外部无法访问。
- 不联网、不采集任何数据；卸载时删除 `data\` 目录即可完全清除。

## 目录结构

```
├── main_web.py              入口：内嵌 HTTP 服务 + pywebview 无边框窗口
├── mediavault_web.spec      PyInstaller 打包配置
├── requirements.txt         依赖清单
├── app/
│   ├── pyapi.py             JS 桥 API（工作区 / 查询 / 扫描 / 删除 / 备份 / 窗口）
│   ├── server.py            静态资源 + 缩略图 + 媒体 Range 流
│   ├── db.py                SQLite 索引与查询
│   ├── scanner.py           目录扫描（多线程、过滤非照片杂图）
│   ├── authoring.py         作者识别规则（EXIF / XMP / sidecar / 文件夹名 / 文件名）
│   ├── metadata.py          元数据抽取（EXIF / XMP / ffprobe）
│   ├── thumbs.py            缩略图生成（Pillow / ffmpeg 抽帧）
│   ├── backup.py            gzip 快照备份与恢复
│   ├── workspace.py         多工作区（每个工作区独立数据库）
│   ├── paths.py             数据目录定位（便携优先）
│   ├── winenv.py            Windows 相关：孤儿进程清理 / 窗口激活
│   ├── assets/              应用图标
│   └── web/                 前端（原生 HTML/CSS/JS，无构建步骤）
└── tools/installer/
    ├── lumen.iss            Inno Setup 安装器脚本
    └── ui.html              安装界面
```

## 启动流程为什么绕

WebView2 有个坑：窗口不可见时会挂起渲染进程，`loaded` 事件永不触发 —— 表现就是
「白屏 + 未响应」。所以这里：

- 窗口**不隐藏创建**，页面加载完直接显示；
- WebView2 的 profile 固定到 `data\webview2`，配 `webview2.dirty` 脏标记，
  上次没干净退出就自动重建；
- 启动前清理父进程已死的 `msedgewebview2.exe` 残留；
- 看门狗是**独立进程**（主线程卡在 COM 调用上会占着 GIL，进程内线程会被饿死），
  超时未就绪就杀掉重试一次，仍失败才切到 Edge/Chrome 的 app 窗口（功能一致）。

## License

MIT
