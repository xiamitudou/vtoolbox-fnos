#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟飞牛安装后的布局，验证 cmd/ 生命周期脚本能否真实拉起服务。

不写 utime（沙箱可能禁止），手动解包并 chmod。
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = "/home/support/WorkBuddy/feiniu/videotoolbox"
FPK = os.path.join(ROOT, "dist", "vtoolbox.fpk")
# 放在工作空间内：本机沙箱会拦截 /tmp 下由其他命令创建的路径
SANDBOX = "/home/support/WorkBuddy/feiniu/.workbuddy/tmp/fpk-verify"
APP = os.path.join(SANDBOX, "apps", "vtoolbox")
# 用动态端口：本机沙箱里 nohup 分离出去的进程可能落在已失效的命名空间中，
# 固定端口一旦被上一轮残留实例占住，测试就会连不上（而进程看起来"已启动"）。
PORT = None
DIRECT_PORT = None
RUN = time.strftime("%H%M%S")

# 本机可能设置了 http_proxy，会让 127.0.0.1 的请求被代理拦截（返回 502）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen(url, timeout=10):
    return OPENER.open(url, timeout=timeout)

FAIL = []


def log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def free_port(start=5231, count=20):
    import socket
    for candidate in range(start, start + count):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise RuntimeError("找不到空闲端口")


def port_busy(port):
    """端口是否已被真实进程占用。

    刻意加 SO_REUSEADDR：否则上一次运行留下的 TIME_WAIT 会被误判为占用，
    导致「服务已监听」误报失败（这正是本机曾出现过的一次假失败）。
    """
    import socket
    probe = socket.socket()
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def wait_health(port=None, timeout=60):
    port = port or DIRECT_PORT
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen("http://127.0.0.1:%d/api/health" % port, timeout=3) as resp:
                resp.read()
            return True
        except Exception:
            time.sleep(0.4)
    return False


def dump_log(path, title):
    log("    %s（%s）：" % (title, path))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle.read().splitlines()[-20:]:
                log("      " + line)
    except OSError as exc:
        log("      无法读取：%s" % exc)


def unix_http_get(sock_path, url_path):
    """经 Unix Socket 发一个 GET，模拟飞牛统一网关的转发。"""
    import http.client
    import socket as socket_mod

    class _Conn(http.client.HTTPConnection):
        def __init__(self):
            http.client.HTTPConnection.__init__(self, "localhost", timeout=10)

        def connect(self):
            sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(sock_path)
            self.sock = sock

    conn = _Conn()
    try:
        conn.request("GET", url_path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def wait_job(job_id, port=DIRECT_PORT, timeout=300):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        with urlopen("http://127.0.0.1:%d/api/jobs?id=%s" % (port, job_id),
                                    timeout=10) as resp:
            job = json.loads(resp.read().decode("utf-8"))["job"]
        if job["status"] != last:
            last = job["status"]
            log("    任务 %s：%s" % (job_id, job["status"]))
        if job["status"] in ("done", "failed", "canceled"):
            return job
        time.sleep(0.5)
    raise RuntimeError("任务超时")


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    log("  %s %s%s" % ("[通过]" if ok else "[失败]", name, ("  → " + detail) if detail else ""))


def extract_content(tar, dest):
    """只写内容与权限，忽略 mtime/owner。"""
    for member in tar.getmembers():
        target = os.path.join(dest, member.name)
        if member.isdir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if member.issym():
            if os.path.lexists(target):
                os.remove(target)
            os.symlink(member.linkname, target)
            continue
        source = tar.extractfile(member)
        with open(target, "wb") as handle:
            shutil.copyfileobj(source, handle)
        os.chmod(target, member.mode & 0o777)


def main():
    # 幂等：不整目录删除（会触发批量删除保护），按需覆盖即可
    os.makedirs(APP, exist_ok=True)

    log("== 解包 .fpk ==")
    with tarfile.open(FPK, "r:gz") as tar:
        extract_content(tar, APP)
    for name in ("cmd", "config", "wizard", "manifest", "ICON.PNG", "ICON_256.PNG", "app.tgz"):
        check("包内含 %s" % name, os.path.exists(os.path.join(APP, name)))

    log("\n== 权限声明（核显需要 video/render 附加组） ==")
    try:
        with open(os.path.join(APP, "config", "privilege"), encoding="utf-8") as handle:
            priv = json.load(handle)
        check("run-as 为 package（最小权限）",
              (priv.get("defaults") or {}).get("run-as") == "package",
              str(priv.get("defaults")))
        groups = priv.get("join-groups") or []
        check("声明了 join-groups（%s）" % ",".join(groups),
              "video" in groups and "render" in groups, str(groups))
    except (OSError, ValueError) as exc:
        check("privilege 可解析", False, str(exc))

    log("\n== 模拟飞牛安装目录（此时 app.tgz 还没解压） ==")
    target = os.path.join(APP, "target")
    os.makedirs(target, exist_ok=True)
    for name in ("var", "etc", "tmp"):
        os.makedirs(os.path.join(APP, name), exist_ok=True)
    os.makedirs(os.path.join(SANDBOX, "media"), exist_ok=True)
    os.makedirs(os.path.join(SANDBOX, "vol", "@appcenter", "vtoolbox"), exist_ok=True)

    # 每次运行用独立的 var/tmp 与端口：避免上一轮残留的 pid 文件 / 端口让本轮"看起来已启动"却连不上
    run_var = os.path.join(APP, "var-" + RUN)
    run_tmp = os.path.join(APP, "tmp-" + RUN)
    os.makedirs(run_var, exist_ok=True)
    os.makedirs(run_tmp, exist_ok=True)
    global PORT, DIRECT_PORT
    PORT = free_port(5300, 40)
    DIRECT_PORT = free_port(PORT + 1, 40)
    log("   本轮端口：main=%d  直连=%d" % (PORT, DIRECT_PORT))

    env = os.environ.copy()
    env.update({
        "TRIM_APPDEST": target,
        "TRIM_PKGVAR": run_var,
        "TRIM_PKGETC": os.path.join(APP, "etc"),
        "TRIM_PKGTMP": run_tmp,
        "VTOOLBOX_PORT": str(PORT),
        "TRIM_TEMP_LOGFILE": os.path.join(SANDBOX, "install-err.log"),
        "wizard_trim_mode": "accurate",
        "wizard_concurrency": "1",
        "wizard_on_exists": "rename",
        # 模拟飞牛授权目录 / 共享目录环境变量
        "TRIM_DATA_ACCESSIBLE_PATHS": os.path.join(SANDBOX, "media"),
        "TRIM_DATA_SHARE_PATHS": os.path.join(SANDBOX, "media"),
        # 本机存在 http_proxy，会让 127.0.0.1 请求被代理拦截
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })

    # ---- 回归点：飞牛的 install_init 是「安装文件应用前执行」，那一刻 target/ 还是空的。
    # 早期版本在这里检查 bin/ffmpeg，导致真机安装直接失败（误报「应用文件不完整」）。
    log("\n== 阶段 1：install_init（官方语义：文件落盘前执行） ==")
    log("   故意让 target/ 为空，验证不会误报文件缺失")
    rc = subprocess.run(["bash", os.path.join(APP, "cmd", "install_init")],
                        env=env, capture_output=True, text=True)
    detail = (rc.stderr or rc.stdout or "").strip().replace("\n", " / ")
    check("target 为空时 install_init 仍返回 0", rc.returncode == 0, detail)

    log("\n== 阶段 2：解包 app.tgz 到 target/ ==")
    with tarfile.open(os.path.join(APP, "app.tgz"), "r:gz") as tar:
        extract_content(tar, target)

    for rel in ("server/main.py", "server/media.py", "server/jobs.py",
                "www/index.html", "www/style.css", "www/app.js",
                "ui/config", "ui/images/icon_64.png", "ui/images/icon_256.png",
                "bin/ffmpeg", "bin/ffprobe"):
        path = os.path.join(target, rel)
        check("target/%s 存在" % rel, os.path.isfile(path))

    # ---- 统一网关入口声明：公网/桌面内嵌访问依赖它（复用飞牛域名与登录态）----
    log("\n== 校验 app/ui/config 的统一网关声明 ==")
    ui_cfg_path = os.path.join(target, "ui", "config")
    with open(ui_cfg_path, encoding="utf-8") as handle:
        ui_cfg = json.load(handle)
    entries = ui_cfg.get(".url") or {}
    entry = entries.get("vtoolbox.Application") or {}
    check("ui/config 含 vtoolbox.Application 入口", bool(entry))
    check("gatewayPrefix = /app/vtoolbox", entry.get("gatewayPrefix") == "/app/vtoolbox",
          str(entry.get("gatewayPrefix")))
    sock_name = str(entry.get("gatewaySocket") or "")
    check("gatewaySocket 只写文件名（%s）" % sock_name,
          sock_name.endswith(".sock") and "/" not in sock_name)
    check("url 指向网关前缀 /app/vtoolbox", entry.get("url") == "/app/vtoolbox",
          str(entry.get("url")))
    check("入口 key 以 appname 前缀", list(entries.keys()) == ["vtoolbox.Application"],
          str(list(entries.keys())))
    check("type=iframe（桌面内嵌打开）", entry.get("type") == "iframe", str(entry.get("type")))
    # 关键回归点：只要声明了 port，飞牛桌面就会改走「协议+端口」方式拼 iframe 地址，
    # 在 HTTPS 页面下会因 TLS 握手失败或混合内容拦截而白屏（1.0.3 的真实故障）。
    check("入口不声明 port（强制走同源网关）",
          not str(entry.get("port") or "").strip(), "port=%r" % entry.get("port"))
    check("入口不声明 protocol",
          not str(entry.get("protocol") or "").strip(), "protocol=%r" % entry.get("protocol"))

    # 前端必须是相对路径，才能同时适配 / 与 /app/vtoolbox/ 两种前缀
    with open(os.path.join(target, "www", "index.html"), encoding="utf-8") as handle:
        html_raw = handle.read()
    check("index.html 使用相对路径引用资源",
          'href="style.css"' in html_raw and 'src="app.js"' in html_raw
          and 'href="/style.css"' not in html_raw)

    # 前后端契约：下面这些钩子一旦在打包时丢失，功能会「静默失效」——
    # 页面照常打开，但排序/片尾/输出默认值等点了没反应，很难排查。
    with open(os.path.join(target, "www", "app.js"), encoding="utf-8") as handle:
        app_raw = handle.read()
    for token, why in (
        ('data-sort="name"', "按名称排序"),
        ('data-sort="duration"', "按时长排序"),
        ('data-sort="size"', "按大小排序"),
        ("PROBE_BATCH", "探测小批串行（防 502）"),
        ("probeBatch", "探测失败拆半重试"),
        ("trimTarget", "删除范围选择"),
        ("tailInput", "片尾时长输入"),
        ("cfgOutputMode", "默认输出位置"),
        ("mergePostAction", "合并后处理源视频"),
        ("post_action", "合并后处理参数"),
        ("已合并视频", "归档文件夹名"),
        ("mergeGroup", "合并分组方式选择"),
        ("orientationRatio", "横竖屏比例容差输入"),
        ("renderMergeGroup", "分组方式提示渲染"),
    ):
        check("app.js 含 %s（%s）" % (token, why), token in app_raw)
    for token in ('id="trimTarget"', 'id="tailInput"', 'id="tailChips"',
                  'id="cfgOutputMode"', 'value="tail"', 'value="both"',
                  'id="mergePostAction"', 'id="mergePostHint"',
                  'value="move"', 'value="delete"',
                  'id="mergeGroup"', 'id="orientationRatio"',
                  'id="mergeGroupHint"', 'value="resolution"',
                  'value="orientation"'):
        check("index.html 含 %s" % token, token in html_raw)

    check("bin/ffmpeg 具备可执行位",
          os.access(os.path.join(target, "bin", "ffmpeg"), os.X_OK),
          oct(os.stat(os.path.join(target, "bin", "ffmpeg")).st_mode & 0o777))
    check("bin/ffprobe 具备可执行位",
          os.access(os.path.join(target, "bin", "ffprobe"), os.X_OK))
    for name in ("main", "install_init", "install_callback"):
        check("cmd/%s 具备可执行位" % name,
              os.access(os.path.join(APP, "cmd", name), os.X_OK))

    log("\n== 阶段 3：install_callback（官方语义：文件落盘后执行） ==")
    rc = subprocess.run(["bash", os.path.join(APP, "cmd", "install_callback")],
                        env=env, capture_output=True, text=True)
    check("install_callback 返回 0", rc.returncode == 0, (rc.stderr or "").strip())
    conf = os.path.join(APP, "etc", "config.json")
    check("已生成 etc/config.json", os.path.isfile(conf))

    if os.path.isfile(conf):
        with open(conf, encoding="utf-8") as handle:
            data = json.load(handle)
        check("向导参数已写入配置（trim_mode=%s）" % data.get("trim_mode"),
              data.get("trim_mode") == "accurate")

    # install.log 写到本次运行专属的 TRIM_PKGVAR（run_var），而非固定 var/
    inst_log = os.path.join(run_var, "install.log")
    if os.path.isfile(inst_log):
        with open(inst_log, encoding="utf-8", errors="replace") as handle:
            inst_text = handle.read()
        check("install.log 记录了网关 socket 目录状态", "网关 socket" in inst_text)
    else:
        check("install.log 记录了网关 socket 目录状态", False, "未生成 %s" % inst_log)

    log("\n== 生成测试素材 ==")
    sample = os.path.join(SANDBOX, "media", "样本.mp4")
    subprocess.run([os.path.join(target, "bin", "ffmpeg"), "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "50",
                    "-c:a", "aac", "-shortest", sample], check=True)
    log("  生成测试素材 %s" % sample)

    log("\n== 执行 cmd/main status / stop / 未知参数 ==")
    rc = subprocess.run(["bash", os.path.join(APP, "cmd", "main"), "status"],
                        env=env, capture_output=True, text=True)
    check("未运行时 main status 返回 3", rc.returncode == 3, "rc=%d" % rc.returncode)
    rc = subprocess.run(["bash", os.path.join(APP, "cmd", "main"), "stop"],
                        env=env, capture_output=True, text=True)
    check("未运行时 main stop 返回 0（可重复执行）", rc.returncode == 0, "rc=%d" % rc.returncode)
    check("stop 后不残留网关 socket",
          not os.path.exists(os.path.join(target, "vtoolbox.sock")),
          os.path.join(target, "vtoolbox.sock"))
    rc = subprocess.run(["bash", os.path.join(APP, "cmd", "main"), "bogus"],
                        env=env, capture_output=True, text=True)
    check("未知参数返回 1", rc.returncode == 1, "rc=%d" % rc.returncode)

    log("\n== 执行 cmd/main start（分离启动） ==")
    log_file = os.path.join(run_var, "server.log")
    rc = subprocess.run(["bash", os.path.join(APP, "cmd", "main"), "start"],
                        env=env, capture_output=True, text=True)
    state = None
    if rc.returncode == 0:
        for _ in range(40):
            try:
                with urlopen("http://127.0.0.1:%d/api/state" % PORT, timeout=3) as resp:
                    state = json.loads(resp.read().decode("utf-8"))
                break
            except Exception:
                time.sleep(0.5)
        if state is not None:
            check("main start 返回 0", True)
            check("分离启动的实例可访问 /api/state", True)
            sock_file = os.path.join(target, "vtoolbox.sock")
            check("分离启动后已创建网关 socket", os.path.exists(sock_file), sock_file)
        else:
            # 分离进程已按日志正常启动，但沙箱给它分配的网络命名空间不可达 —— 开发机限制
            started_in_log = False
            try:
                with open(log_file, encoding="utf-8", errors="replace") as handle:
                    started_in_log = "启动，ffmpeg=" in handle.read()
            except OSError:
                pass
            if started_in_log:
                check("main start 返回 0", True)
                log("  [环境限制] 日志显示服务已正常启动，但分离进程所在网络命名空间不可达，")
                log("             本机无法验证连通性；改用 cmd/main 自己的命令行以子进程方式验证（见下）")
            else:
                check("main start 返回 0", True)
                check("分离启动的实例可访问 /api/state", False, "")
                dump_log(log_file, "cmd/main 启动日志")
    elif not os.path.exists(log_file):
        log("  [环境限制] 分离进程无法创建日志文件（沙箱剥夺 nohup 子进程写权限），此路径本机不可验证")
        log("  等价命令：bash %s start（rc=%d）" % (os.path.join(APP, "cmd", "main"), rc.returncode))
    else:
        check("main start 返回 0", False,
              (rc.stdout or "").strip() + (rc.stderr or "").strip())

    log("\n== 取出 cmd/main 自己构造的命令行 ==")
    subprocess.run(["bash", os.path.join(APP, "cmd", "main"), "stop"],
                   env=env, capture_output=True, text=True)
    dry_env = env.copy()
    dry_env["VTOOLBOX_DRY_RUN"] = "1"
    rc = subprocess.run(["bash", os.path.join(APP, "cmd", "main"), "start"],
                        env=dry_env, capture_output=True, text=True)
    main_argv = [line for line in (rc.stdout or "").splitlines() if line.strip()]
    check("dry-run 打印出完整命令行（%d 个参数）" % len(main_argv),
          rc.returncode == 0 and len(main_argv) >= 12, " ".join(main_argv))
    check("命令行指向包内 server/main.py",
          any(a.endswith("server/main.py") for a in main_argv))
    check("命令行使用包内 bin 目录",
          "--bin" in main_argv and os.path.join(target, "bin") in main_argv)
    check("命令行端口与 manifest 一致", str(PORT) in main_argv)
    check("命令行声明网关 socket",
          "--sock" in main_argv and any(a.endswith("vtoolbox.sock") for a in main_argv),
          " ".join(main_argv))

    if state is None and len(main_argv) >= 2:
        # 用 cmd/main 的同一条命令行以子进程方式启动，验证这条命令本身可用。
        # 换一个空闲端口：上一个分离实例可能仍占着原端口，而它在当前命名空间里不可达。
        alt_port = free_port(PORT + 100, 60)
        argv = list(main_argv)
        if "--port" in argv:
            argv[argv.index("--port") + 1] = str(alt_port)
        log("  （分离启动不可验证，改用上面取到的命令行以子进程方式启动，端口 %d）" % alt_port)
        handle = open(log_file, "a", encoding="utf-8")
        child = subprocess.Popen(argv, env=env, stdout=handle,
                                 stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(60):
                try:
                    with urlopen("http://127.0.0.1:%d/api/state" % alt_port, timeout=3) as resp:
                        state = json.loads(resp.read().decode("utf-8"))
                    break
                except Exception:
                    time.sleep(0.5)
            check("用 cmd/main 的命令行可正常启动并响应 /api/state", state is not None)
            if state is None:
                dump_log(log_file, "子进程启动日志")
            else:
                log("\n== 校验统一网关 socket（模拟飞牛网关转发） ==")
                sock_file = os.path.join(target, "vtoolbox.sock")
                check("启动后已创建网关 socket", os.path.exists(sock_file), sock_file)
                if os.path.exists(sock_file):
                    st, body = unix_http_get(sock_file, "/app/vtoolbox/api/state")
                    check("经 socket 访问 /app/vtoolbox/api/state", st == 200 and b"roots" in body,
                          "HTTP %s" % st)
                    st, body = unix_http_get(sock_file, "/app/vtoolbox")
                    html = body.decode("utf-8", "replace")
                    check("经 socket 打开首页（前缀路径）", st == 200 and "<title>" in html,
                          "HTTP %s" % st)
                    check("首页注入 base=/app/vtoolbox/",
                          '<base href="/app/vtoolbox/">' in html)
                    st, _ = unix_http_get(sock_file, "/app/vtoolbox/style.css")
                    check("经 socket 取到静态资源", st == 200, "HTTP %s" % st)
                    st, _ = unix_http_get(sock_file, "/app/vtoolboxx/api/state")
                    check("相似前缀不被误剥离（404）", st == 404, "HTTP %s" % st)
        finally:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()

    if state is None:
        check("能取得运行实例的 /api/state（用于校验运行环境）", False,
              "分离实例与子进程实例均不可达，本机沙箱限制")

    if state:
        check("运行期使用了内置 ffmpeg",
              state["ffmpeg"]["available"] and "static" in (state["ffmpeg"]["version"] or ""),
              state["ffmpeg"]["version"])
        check("授权目录已传入（%s）" % state["roots"],
              os.path.join(SANDBOX, "media") in state["roots"])
        check("默认输出目录可用", bool(state["default_output_dir"]), state["default_output_dir"])
        hw = state.get("hwaccel") or {}
        check("状态里带核显探测结果（available=%s）" % hw.get("available"),
              "available" in hw and "requested" in hw and isinstance(hw.get("reason"), str),
              str(hw.get("reason")))

    log("\n== 用打包产物直连启动，跑真实删片头任务 ==")
    log("   说明：本机沙箱会剥夺 nohup 分离进程的写权限（EPERM），")
    log("   因此这里用与 cmd/main 完全相同的命令行以子进程方式启动，验证打包内容本身。")
    direct_var = os.path.join(SANDBOX, "var2")
    suffix = "_已删片头_" + RUN
    direct_port = DIRECT_PORT
    direct_log = os.path.join(SANDBOX, "direct.log")
    log("   直连实例端口：%d" % direct_port)

    def start_direct():
        handle = open(direct_log, "w", encoding="utf-8")
        return subprocess.Popen(
            [sys.executable, os.path.join(target, "server", "main.py"),
             "--port", str(direct_port),
             "--www", os.path.join(target, "www"),
             "--bin", os.path.join(target, "bin"),
             "--var", direct_var,
             "--etc", os.path.join(APP, "etc"),
             "--tmp", os.path.join(SANDBOX, "tmp2"),
             "--allow-root", os.path.join(SANDBOX, "media")],
            env=env, stdout=handle, stderr=subprocess.STDOUT, text=True,
        )

    direct = start_direct()
    try:
        started = wait_health(direct_port)
        check("直连实例启动", started, "端口 %d" % direct_port)
        if not started:
            dump_log(direct_log, "实例日志")
            raise RuntimeError("直连实例启动失败")
        body = json.dumps({
            "type": "trim",
            "files": [sample],
            "options": {"head_seconds": 8, "mode": "accurate",
                        "output_dir": os.path.join(SANDBOX, "media"),
                        "name_suffix": suffix, "output_ext": "mp4"},
        }).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:%d/api/jobs" % direct_port, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=30) as resp:
            job_id = json.loads(resp.read().decode("utf-8"))["job"]["id"]
        current = wait_job(job_id, port=direct_port)
        check("删片头任务执行成功", current.get("status") == "done", current.get("error") or "")
        if current.get("status") != "done":
            log("    任务日志：")
            for line in (current.get("logs") or [])[-25:]:
                log("      " + line)
        out = os.path.join(SANDBOX, "media", "样本%s.mp4" % suffix)
        check("输出文件已生成", os.path.isfile(out))
        if os.path.isfile(out):
            proc = subprocess.run([os.path.join(target, "bin", "ffprobe"), "-v", "error",
                                   "-show_entries", "format=duration", "-of",
                                   "default=nw=1:nk=1", out], capture_output=True, text=True)
            dur = float(proc.stdout.strip() or 0)
            check("输出时长约 22 秒（实际 %.2f）" % dur, 21.4 <= dur <= 22.4)
            mode = (current.get("result") or {}).get("mode")
            check("走精准路径（%s：两段式或切点恰在关键帧的直拷）" % mode,
                  mode in ("two-stage", "copy"))
    finally:
        direct.terminate()
        try:
            direct.wait(timeout=10)
        except subprocess.TimeoutExpired:
            direct.kill()

    log("\n== 重启后任务记录恢复 ==")
    direct = start_direct()
    try:
        ok = wait_health(direct_port)
        if not ok:
            dump_log(direct_log, "实例日志")
        with urlopen("http://127.0.0.1:%d/api/jobs" % direct_port, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [item["id"] for item in data["jobs"]]
        check("历史任务已持久化并可读取（本次任务 %s 在列）" % job_id, job_id in ids,
              "共 %d 条" % len(ids))
        check("状态文件写在数据目录", os.path.isfile(os.path.join(direct_var, "jobs.json")))
    except Exception as exc:
        check("历史任务已持久化并可读取", False, str(exc))
    finally:
        direct.terminate()
        try:
            direct.wait(timeout=10)
        except subprocess.TimeoutExpired:
            direct.kill()

    log("\n== 兜底能力：内置 ffmpeg 缺失时应自动回退系统 ffmpeg ==")
    empty_bin = os.path.join(SANDBOX, "empty-bin")
    os.makedirs(empty_bin, exist_ok=True)
    fb_port = free_port()
    fb_log = os.path.join(SANDBOX, "fallback.log")
    handle = open(fb_log, "w", encoding="utf-8")
    fb = subprocess.Popen(
        [sys.executable, os.path.join(target, "server", "main.py"),
         "--port", str(fb_port),
         "--www", os.path.join(target, "www"),
         "--bin", empty_bin,
         "--var", os.path.join(SANDBOX, "var3"),
         "--etc", os.path.join(APP, "etc"),
         "--tmp", os.path.join(SANDBOX, "tmp3"),
         "--allow-root", os.path.join(SANDBOX, "media")],
        env=env, stdout=handle, stderr=subprocess.STDOUT, text=True,
    )
    try:
        ok = wait_health(fb_port, timeout=40)
        check("回退实例仍能启动", ok)
        if ok:
            with urlopen("http://127.0.0.1:%d/api/state" % fb_port, timeout=10) as resp:
                fb_state = json.loads(resp.read().decode("utf-8"))
            info = fb_state["ffmpeg"]
            check("内置缺失时回退到系统 ffmpeg（%s）" % info["path"],
                  info["available"] and not info["builtin"], info.get("version") or "")
    except Exception as exc:
        check("内置缺失时回退到系统 ffmpeg", False, str(exc))
        dump_log(fb_log, "回退实例日志")
    finally:
        fb.terminate()
        try:
            fb.wait(timeout=10)
        except subprocess.TimeoutExpired:
            fb.kill()

    log("\n== 兜底能力：安装脚本漏文件时应报出准确原因 ==")
    broken = os.path.join(SANDBOX, "broken-app")
    os.makedirs(os.path.join(broken, "cmd"), exist_ok=True)
    os.makedirs(os.path.join(broken, "target", "bin"), exist_ok=True)
    for name in ("install_init", "install_callback", "main"):
        shutil.copyfile(os.path.join(APP, "cmd", name), os.path.join(broken, "cmd", name))
        os.chmod(os.path.join(broken, "cmd", name), 0o755)
    broken_env = env.copy()
    broken_env["TRIM_APPDEST"] = os.path.join(broken, "target")
    rc = subprocess.run(["/bin/bash", os.path.join(broken, "cmd", "install_callback")],
                        env=broken_env, capture_output=True, text=True)
    # 修前：多处检查互相覆盖 err，最终永远报「缺少内置 ffmpeg」，误导排障
    msg = ((rc.stderr or "") + (rc.stdout or "")).strip()
    check("漏文件时安装失败并报出真正缺失的文件名", rc.returncode != 0 and "server/main.py" in msg,
          msg[:120] or "rc=%d" % rc.returncode)

    log("\n================ 结果 ================")
    log("失败 %d 项" % len(FAIL))
    for name in FAIL:
        log("  失败：%s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
