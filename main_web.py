# -*- coding: utf-8 -*-
"""Lumen（本地媒体库 Web UI 版）入口。

- 内嵌 HTTP 服务提供页面与媒体流，pywebview 无边框窗口承载 UI；
- 关闭窗口时自动轻量备份（gzip 元数据快照）；
- 环境变量 MM_SMOKE=1 时窗口展示数秒后自动关闭（冒烟测试用）。

启动策略（重要）：
  本机装的是 WebView2，而 WebView2 偶尔会整机失效——任何新实例都卡在
  CreateCoreWebView2Environment 上不回调，表现为「双击后一直未响应」。
  所以这里做成两层：
    1) 原生窗口「隐藏创建」，等页面 loaded 再 show，卡住时用户看不到白窗；
    2) 启动看门狗：MM_BOOT_TIMEOUT 秒内没就绪，就杀掉卡死的进程，
       改用 Edge/Chrome 的 app 窗口打开同一套界面（后端走本机 /rpc，功能一致）。
  命令行：Lumen.exe --browser 可直接进兼容模式。
"""
import os
import sys

# ---- 运行环境兜底（本机管控策略：仅允许写应用目录内）----
_BASE = os.path.dirname(os.path.abspath(__file__))
_TMP = os.path.join(_BASE, "_tmp")
os.makedirs(_TMP, exist_ok=True)


def _clean_webview_zombies():
    """启动前清掉父进程已死的 msedgewebview2。

    实测：一次强杀/崩溃留下的残留 WebView2 会让**下一次**启动卡死在
    CreateCoreWebView2Environment（白屏 + 系统判定未响应）；清掉即恢复。
    这是「反复启动不了」的直接原因，所以放在最前面做。
    """
    try:
        sys.path.insert(0, _BASE)
        from app.winenv import clean_orphans
        n = clean_orphans()
        if n:
            _log("已清理残留 WebView2 进程 %d 个" % n)
        return n
    except Exception:
        return 0


def _usable_temp() -> str:
    """挑一个真正能用的临时目录：应用目录 _tmp 优先，其次用户 TEMP。

    2026-11-07 实测（本机故障现场）：原来用 tempfile.mkdtemp 做探测，在
    `%LOCALAPPDATA%\\Temp` 上会卡住十几秒甚至永久阻塞——目录创建被安全软件
    的过滤驱动拖死，进程停在 main_web 模块导入阶段（连 startup.log 都写不出来，
    表现就是双击后毫无反应/白屏）。改成「写一个探针文件」后瞬间返回。

    WebView2 的 profile 不再依赖 TEMP（见 _webview_profile + storage_path），
    所以这里把 TEMP 指到应用目录不会再把 EBWebView 的坏状态带进来。
    """
    cands = [_TMP,
             os.path.join(os.environ.get("LOCALAPPDATA") or "", "Temp"),
             os.environ.get("TEMP"), os.environ.get("TMP")]
    for d in cands:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".tmp_probe_%d" % os.getpid())
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("1")
            os.remove(probe)
            return d
        except OSError:
            continue
    return _TMP


if os.environ.get("MM_KEEP_TEMP") != "1":
    _T = _usable_temp()
    for _k in ("TEMP", "TMP", "TMPDIR"):
        os.environ[_k] = _T

# pythonnet 默认走 coreclr（本机 .NET Framework 加载路径不可用）
os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")
if "PYTHONNET_CORECLR_RUNTIME_CONFIG" not in os.environ:
    _candidates = [
        os.path.join(getattr(sys, "_MEIPASS", _BASE), "pythonnet.runtimeconfig.json"),
        os.path.join(_BASE, "runtime", "pythonnet.runtimeconfig.json"),
        os.path.join(_BASE, "pythonnet.runtimeconfig.json"),
    ]
    for _c in _candidates:
        if os.path.isfile(_c):
            os.environ["PYTHONNET_CORECLR_RUNTIME_CONFIG"] = _c
            break

sys.path.insert(0, _BASE)


def _log(msg: str):
    """启动过程留痕（排查「启动不了」时第一手资料）。"""
    import time
    try:
        from app.paths import dirs
        log = os.path.join(dirs()["data"], "startup.log")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _webview_profile() -> str:
    """给 WebView2 一个受控的用户数据目录，并处理「上次异常退出」的自愈。

    为什么不用 pywebview 的默认私有模式：那会给每次启动一个随机临时目录，
    上一轮崩溃留下的坏状态无从清理，而本机实测「残留 = 下次必卡」。
    这里改成 data/webview2 固定目录 + dirty 标记：
      - 正常关闭：删掉 dirty；
      - 下次启动发现 dirty 还在 → 说明上次没干净退出，直接重建 profile。
    顺带好处：localStorage 能持久（视图/缩略图大小/侧栏折叠的记忆不再丢）。
    """
    import shutil
    from app.paths import dirs

    d = dirs()["data"]
    prof = os.path.join(d, "webview2")
    dirty = os.path.join(d, "webview2.dirty")
    if os.path.exists(dirty):
        shutil.rmtree(prof, ignore_errors=True)
        _log("检测到上次异常退出，已重建 WebView2 配置目录")
        try:
            os.remove(dirty)
        except OSError:
            pass
    try:
        os.makedirs(prof, exist_ok=True)
        with open(dirty, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass
    return prof


def _webview_profile_clean():
    try:
        from app.paths import dirs
        os.remove(os.path.join(dirs()["data"], "webview2.dirty"))
    except OSError:
        pass


def _diag_start():
    """MM_DIAG=1 时定期把所有 Python 线程栈写进 diag.log（定位「卡在哪」）。"""
    if os.environ.get("MM_DIAG") != "1":
        return
    import threading
    import time
    import traceback

    def loop():
        n = 0
        while n < 40:
            time.sleep(2.0)
            n += 1
            try:
                from app.paths import dirs
                path = os.path.join(dirs()["data"], "diag.log")
                frames = sys._current_frames()
                names = {t.ident: t.name for t in threading.enumerate()}
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("\n===== dump #%d @%s =====\n"
                             % (n, time.strftime("%H:%M:%S")))
                    for ident, frame in frames.items():
                        fh.write("--- thread %s (%s) ---\n"
                                 % (names.get(ident, ident), ident))
                        fh.write("".join(traceback.format_stack(frame)))
            except Exception:
                pass

    threading.Thread(target=loop, daemon=True, name="diag").start()


def _install_excepthook():
    import traceback

    def hook(tp, val, tb):
        try:
            from app.paths import dirs
            log = os.path.join(dirs()["data"], "error.log")
            with open(log, "a", encoding="utf-8") as fh:
                fh.write("\n" + "".join(traceback.format_exception(tp, val, tb)))
        except Exception:
            pass
        sys.__excepthook__(tp, val, tb)

    sys.excepthook = hook


_install_excepthook()


# ---------- 启动降级记忆 ----------
def _flag_path() -> str:
    from app.paths import dirs
    return os.path.join(dirs()["data"], "webview2-fallback.json")


def _recent_fallback() -> bool:
    """24 小时内降级过 → 说明这台机器现在起不来 WebView2，原生尝试只给短超时。"""
    import json
    import time
    try:
        with open(_flag_path(), "r", encoding="utf-8") as fh:
            ts = float(json.load(fh).get("ts") or 0)
        return (time.time() - ts) < 86400
    except Exception:
        return False


def _mark_fallback(reason: str):
    import json
    import time
    try:
        with open(_flag_path(), "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "reason": reason[:400]}, fh, ensure_ascii=False)
    except Exception:
        pass


def _clear_fallback():
    try:
        os.remove(_flag_path())
    except Exception:
        pass


# ---------- 浏览器探测 ----------
def _browser_candidates():
    """兼容模式的界面宿主候选（Edge 优先，Chrome 次之）。"""
    import shutil
    env = os.environ
    pf = env.get("ProgramFiles", r"C:\Program Files")
    pf86 = env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = env.get("LOCALAPPDATA") or ""
    out = [
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for name in ("msedge", "chrome"):
        p = shutil.which(name)
        if p:
            out.append(p)
    return [p for p in out if p and os.path.isfile(p)]


def _window_exes():
    """可见窗口 → (hwnd, 标题, 进程exe路径)。"""
    import ctypes

    u = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(300)
        u.GetWindowTextW(hwnd, buf, 300)
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if h:
            n = ctypes.c_ulong(520)
            b = ctypes.create_unicode_buffer(520)
            if k32.QueryFullProcessImageNameW(h, 0, b, ctypes.byref(n)):
                exe = b.value
            k32.CloseHandle(h)
        out.append((hwnd, buf.value, exe))
        return True

    u.EnumWindows(cb, 0)
    return out


def _wait_app_window(exe_name: str, timeout: float) -> bool:
    """等浏览器窗口里出现本应用页面（标题带「Lumen」= HTML 已加载）。"""
    import time
    key = os.path.basename(exe_name).lower()
    t0 = time.time()
    while time.time() - t0 < timeout:
        for _, title, exe in _window_exes():
            if "Lumen" in title and exe.lower().endswith(key):
                return True
        time.sleep(1.0)
    return False


def _kill_tree(pid: int):
    import subprocess
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def run_browser(reason: str = ""):
    """兼容模式：界面跑在 Edge/Chrome 的 app 窗口里，后端通过本机 /rpc 通信。

    依次尝试 Edge → Chrome → 系统默认浏览器；每个候选都用窗口标题确认
    页面真的加载成功（这台机器出现过「窗口开了但页面加载不出」的情况）。
    """
    import subprocess
    import time
    import webbrowser

    from app.paths import dirs, asset_path
    from app.pyapi import PyApi
    from app.server import start

    dirs()
    api = PyApi()          # 不 set_window：事件走 pollEvents 队列，由前端轮询派发
    web_dir = asset_path(os.path.join("app", "web"))
    url, server = start(api, web_dir, with_token=True)
    _log("兼容模式启动：%s" % (reason or "手动"))

    profile = os.path.join(dirs()["data"], "browser-profile")
    os.makedirs(profile, exist_ok=True)
    base_args = ["--window-size=1440,940", "--no-first-run",
                 "--no-default-browser-check", "--disable-features=Translate"]

    opened = False
    for i, exe in enumerate(_browser_candidates()):
        args = [exe, "--app=" + url] + base_args
        if i == 0:
            args += ["--user-data-dir=" + profile]   # 独立配置文件，不碰用户日常会话
        _log("兼容模式尝试：%s" % exe)
        try:
            proc = subprocess.Popen(args)
        except Exception as e:
            _log("启动失败 %s: %r" % (exe, e))
            continue
        if _wait_app_window(exe, 15):
            _log("兼容模式 OK：%s" % exe)
            opened = True
            try:
                proc.wait()
            except KeyboardInterrupt:
                pass
            break
        _log("页面未加载成功：%s" % exe)
        _kill_tree(proc.pid)

    if not opened:
        _log("Edge/Chrome 均失败，退到系统默认浏览器")
        webbrowser.open(url)
        # 默认浏览器一般直接进现有会话，窗口不会带 --app 标志；
        # 只要出现标题带「Lumen」的窗口就算成功
        ok = _wait_app_window("", 40)
        _log("默认浏览器 %s" % ("OK" if ok else "未见窗口"))
        if not ok:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "Lumen暂时打不开图形界面：这台电脑的 WebView2 与浏览器组件"
                    "当前都工作异常（多为安全管控软件或系统资源问题）。\n\n"
                    "建议：重启电脑后再打开Lumen，通常即可恢复。\n"
                    "启动过程的记录在 data\\startup.log。",
                    "Lumen", 0x00000010)
            except Exception:
                pass
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    try:
        server.shutdown()
    except Exception:
        pass
    api.shutdown_backup()


def _single_instance() -> bool:
    """单实例保护：拿到锁返回 True；已有实例则激活它的窗口并返回 False。

    背景：这台机器上出现过用户反复双击、每个实例都卡住并互相抢 WebView2
    的情况——多实例只会让「启动不了」雪上加霜。
    """
    import ctypes

    from app.paths import dirs
    from app.winenv import activate_app, pid_alive

    lock = os.path.join(dirs()["data"], "app.lock")
    try:
        with open(lock, "r", encoding="utf-8") as fh:
            old = int(fh.read().strip() or 0)
        if old and old != os.getpid() and pid_alive(old):
            _log("已有实例运行（pid %d），激活其窗口并退出" % old)
            activate_app()
            return False
    except (ValueError, OSError):
        pass
    try:
        with open(lock, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass
    return True


def _release_single_instance():
    try:
        from app.paths import dirs
        lock = os.path.join(dirs()["data"], "app.lock")
        with open(lock, "r", encoding="utf-8") as fh:
            if fh.read().strip() == str(os.getpid()):
                os.remove(lock)
    except OSError:
        pass


def run_watchdog(parent_pid: int, flag: str, timeout: float):
    """独立看门狗进程（--watchdog 模式）：父进程超时未就绪就杀掉并重试。

    为什么用独立进程：主线程卡在 WebView2 的 COM 调用上时不释放 GIL，
    进程内的看门狗线程会被饿死——只有独立进程能保证触发。
    """
    import subprocess
    import time

    sys.path.insert(0, _BASE)
    from app.winenv import pid_alive

    t0 = time.time()
    while time.time() - t0 < timeout:
        if not os.path.exists(flag):          # 父进程就绪后会删掉 flag
            return
        if not pid_alive(parent_pid):         # 父进程自己退了
            try:
                os.remove(flag)
            except OSError:
                pass
            return
        time.sleep(0.5)

    # 超时：杀掉卡死的父进程，重试一次；重试也失败才切兼容模式
    _log("看门狗：%.0fs 未就绪，杀掉 pid %d 重试" % (timeout, parent_pid))
    _kill_tree(parent_pid)
    time.sleep(1)
    try_n = int(os.environ.get("MM_BOOT_TRY", "0") or 0)
    env = dict(os.environ, MM_BOOT_TRY=str(try_n + 1))
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, os.path.join(_BASE, "main_web.py")]
    if try_n >= 1:
        _mark_fallback("watchdog retry failed")
        cmd += ["--browser", "watchdog"]
    else:
        _log("看门狗：自动重启（第 %d 次重试）" % (try_n + 1))
    subprocess.Popen(cmd, cwd=_BASE, env=env)


def run():
    import threading
    import time

    import webview
    from app.paths import dirs, asset_path
    from app.pyapi import PyApi
    from app.server import start

    dirs()  # 确保数据目录就绪
    if not _single_instance():
        return
    _clean_webview_zombies()
    # WebView2 用固定的用户数据目录（data\webview2），不再让 pywebview 每次都
    # tempfile.mkdtemp 一个新临时 profile：
    #   1) mkdtemp 在 TEMP 被安全软件拖慢时会卡死（本机实测卡十秒以上到永久阻塞）；
    #   2) 固定目录配 dirty 标记，异常退出后能自愈重建（_webview_profile）。
    prof = _webview_profile()
    api = PyApi()
    web_dir = asset_path(os.path.join("app", "web"))
    url, server = start(api, web_dir)
    if os.environ.get("MM_FILE_UI") == "1":
        # 本机 Chromium 网络栈被拦（实测连 127.0.0.1 都超时），改走本地文件直载：
        # 页面与静态资源用 file://，JS↔Python 仍走 pywebview 桥（不走 socket）。
        url = "file:///" + os.path.join(web_dir, "index.html").replace("\\", "/")
        _log("界面走本地文件：%s" % url)

    _log("原生模式启动（TEMP=%s，WebView2 配置=%s）"
         % (os.environ.get("TEMP"), prof))
    _diag_start()
    window = webview.create_window(
        "Lumen",
        url,
        width=1440,
        height=900,
        min_size=(880, 560),
        frameless=True,
        easy_drag=False,       # 仅标题栏可拖窗（否则内容区拖拽框选会被当成拖窗口）
        resizable=True,
        # 注意：绝不能在这里用 hidden=True 再等 loaded 去 show。
        # pywebview 的 hidden 实现是 Show() → Hide()，而 WebView2 在窗口不可见时
        # 会挂起渲染进程，loaded 事件随之永远不来 → 表现就是「白屏 + 未响应」。
        # 实测：hidden=True 时 1/5 成功，hidden=False 时 3/3 成功。
        js_api=api,
    )
    api.set_window(window)

    ready = threading.Event()

    def _bring_front():
        try:
            hwnd = api._hwnd()
            if hwnd:
                import ctypes
                ctypes.windll.user32.ShowWindow(hwnd, 9)          # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _diag_frontend():
        """MM_DIAG=1：就绪后 6 秒抓一次前端自报状态，确认页面真的渲染出来了。"""
        if os.environ.get("MM_DIAG") != "1":
            return
        import time

        def probe():
            time.sleep(6)
            js = ("JSON.stringify({title:document.title,"
                  "cards:document.querySelectorAll('.card').length,"
                  "rows:document.querySelectorAll('.row').length,"
                  "gridKids:(document.getElementById('gridWrap')||{children:[]}).children.length,"
                  "sections:document.querySelectorAll('.side-section').length,"
                  "tagBtn:!!document.getElementById('tagBtn'),"
                  "theme:document.documentElement.dataset.theme||'',"
                  "bootFail:document.documentElement.dataset.bootFail||'',"
                  "errs:(window.__mmErr||[]).slice(0,6)})")
            try:
                out = window.evaluate_js(js)
            except Exception as e:
                out = "evaluate_js 失败：%r" % (e,)
            _log("前端自报：%s" % out)

        threading.Thread(target=probe, daemon=True, name="diag-fe").start()

    def on_loaded():
        _log("loaded 回调进入")
        if boot_flag:
            try:
                os.remove(boot_flag)
            except OSError:
                pass
        try:
            window.show()
            _log("show() 返回")
        except Exception as e:
            _log("show() 异常：%r" % (e,))
        _bring_front()
        _log("bring_front 返回")
        ready.set()
        _log("原生界面已就绪")
        _diag_frontend()
        _clear_fallback()

    try:
        window.events.loaded += on_loaded
    except Exception:
        _log("无法挂 loaded 事件，退化为直接显示")
        try:
            window.show()
        except Exception:
            pass
        ready.set()

    # 图标：交给 pywebview 官方参数处理。
    # 千万不要在后台线程里 getattr(window, "native") 去设 form.Icon ——
    # 那是跨线程访问 .NET WinForms 控件，会与 WebView2 初始化抢消息泵，
    # 实测使启动成功率掉到 ~50%（白屏/未响应的另一半元凶）。
    _ico = asset_path(os.path.join("app", "assets", "icon.ico"))
    if not os.path.isfile(_ico):
        _ico = None

    def on_closed():
        api.shutdown_backup()
        try:
            server.shutdown()
        except Exception:
            pass
        _release_single_instance()
        _webview_profile_clean()
        if boot_flag:
            try:
                os.remove(boot_flag)
            except OSError:
                pass
        # 退出时把自己名下的 WebView2 子进程一起收掉：否则残留会毒化下一次启动
        try:
            from app.winenv import kill_my_children
            kill_my_children()
        except Exception:
            pass

    try:
        window.events.closed += on_closed
    except Exception:
        pass

    # ---- 启动看门狗：独立进程，主进程卡死（GIL 被 COM 占住）也能救 ----
    boot_flag = os.path.join(dirs()["data"], "boot.pending")
    try:
        with open(boot_flag, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        boot_flag = None
    if os.environ.get("MM_SMOKE") != "1" and boot_flag:
        import subprocess
        base = float(os.environ.get("MM_BOOT_TIMEOUT", "12"))
        limit = 4.0 if _recent_fallback() else base
        wd = [sys.executable]
        if not getattr(sys, "frozen", False):
            wd.append(os.path.abspath(__file__))
        wd += ["--watchdog", str(os.getpid()), str(limit), boot_flag]
        subprocess.Popen(wd, cwd=_BASE)

    if os.environ.get("MM_SMOKE") == "1":

        def auto_close():
            time.sleep(float(os.environ.get("MM_SMOKE_SEC", "5")))
            try:
                window.evaluate_js(
                    "document.title='SMOKE_OK total='+document.querySelectorAll('.card').length")
            except Exception:
                pass
            time.sleep(1)
            try:
                window.destroy()
            except Exception:
                os._exit(0)

        threading.Thread(target=auto_close, daemon=True).start()

    webview.start(icon=_ico, private_mode=False, storage_path=prof)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--watchdog" in argv:
        i = argv.index("--watchdog")
        try:
            _ppid = int(argv[i + 1])
            _timeout = float(argv[i + 2])
            _flag = argv[i + 3]
        except (IndexError, ValueError):
            sys.exit(2)
        run_watchdog(_ppid, _flag, _timeout)
    elif "--browser" in argv:
        run_browser(" ".join(argv[argv.index("--browser") + 1:]) or "命令行 --browser")
    else:
        try:
            run()
        except Exception:
            import traceback
            _log("原生模式异常：\n" + traceback.format_exc())
            run_browser("原生模式异常")
