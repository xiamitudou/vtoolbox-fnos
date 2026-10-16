#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关入口集成验证：同一进程同时监听 TCP 与 Unix Socket。

覆盖：
  A. TCP 直连（局域网 http://ip:5230）
  B. TCP 带网关前缀（旧版飞牛桌面拼出的地址）
  C. Unix Socket（统一网关转发）
并校验首页注入的 <base href> 在两种入口下都正确。
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TMP = tempfile.mkdtemp(prefix="vtgw-")
SOCK = os.path.join(TMP, "vtoolbox.sock")

passed = []
failed = []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  [%s]" % detail) if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def opener():
    # 绕过开发机上的 http_proxy（会让 127.0.0.1 变成 502）
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


OP = opener()


def http_get(port, path):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
    try:
        with OP.open(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path, timeout=10):
        super(UnixHTTPConnection, self).__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.sock_path)
        self.sock = sock


def unix_get(sock_path, path):
    conn = UnixHTTPConnection(sock_path)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def main():
    port = free_port()
    var = os.path.join(TMP, "var")
    etc = os.path.join(TMP, "etc")
    work = os.path.join(TMP, "tmp")
    logf = open(os.path.join(TMP, "server.log"), "w+", encoding="utf-8")

    cmd = [
        PY, os.path.join(ROOT, "app", "server", "main.py"),
        "--www", os.path.join(ROOT, "app", "www"),
        "--bin", os.path.join(ROOT, "app", "bin"),
        "--var", var, "--etc", etc, "--tmp", work,
        "--port", str(port),
        "--sock", SOCK,
    ]
    env = os.environ.copy()
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env.pop("http_proxy", None)
    env.pop("HTTP_PROXY", None)
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, env=env)

    try:
        # ---- 等待就绪 ----
        ready = False
        for _ in range(60):
            try:
                status, _ = http_get(port, "/api/state")
                if status == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.25)
        check("服务就绪（TCP）", ready)
        if not ready:
            logf.flush()
            logf.seek(0)
            print(logf.read()[-3000:])
            return 1

        check("网关 socket 已创建", os.path.exists(SOCK), SOCK)
        mode = oct(os.stat(SOCK).st_mode & 0o777) if os.path.exists(SOCK) else "-"
        check("socket 权限允许网关连接", os.path.exists(SOCK) and (os.stat(SOCK).st_mode & 0o077) != 0, mode)

        print("\n== A. TCP 直连（局域网 / 端口入口）==")
        st, body = http_get(port, "/api/state")
        data = json.loads(body.decode("utf-8"))
        check("GET /api/state 返回 200 且是 JSON", st == 200 and "roots" in data)

        st, body = http_get(port, "/")
        html = body.decode("utf-8")
        check("GET / 返回首页", st == 200 and "<title>视频工具箱</title>" in html)
        check("首页注入的 base 为 /", '<base href="/">' in html, html[html.find("<base"):html.find("<base") + 30])
        check("静态资源使用相对路径", 'href="style.css"' in html and 'src="app.js"' in html)

        st, body = http_get(port, "/style.css")
        check("GET /style.css 返回 CSS", st == 200 and b"{" in body)

        print("\n== B. TCP 带网关前缀（旧版桌面 / 反向代理前缀）==")
        st, body = http_get(port, "/app/vtoolbox")
        html = body.decode("utf-8")
        check("GET /app/vtoolbox 返回首页", st == 200 and "<title>视频工具箱</title>" in html)
        check("base 注入为 /app/vtoolbox/", '<base href="/app/vtoolbox/">' in html,
              html[html.find("<base"):html.find("<base") + 40])

        st, body = http_get(port, "/app/vtoolbox/api/state")
        check("GET /app/vtoolbox/api/state 返回 200", st == 200 and b"roots" in body)

        st, body = http_get(port, "/app/vtoolbox/style.css")
        check("GET /app/vtoolbox/style.css 返回 CSS", st == 200 and b"{" in body)

        st, body = http_get(port, "/app/vtoolbox/app.js")
        check("GET /app/vtoolbox/app.js 返回 JS", st == 200 and b"apiUrl" in body)

        print("\n== C. Unix Socket（统一网关）==")
        st, body = unix_get(SOCK, "/app/vtoolbox/api/state")
        check("经 socket GET /app/vtoolbox/api/state 返回 200", st == 200 and b"roots" in body,
              "HTTP %s" % st)

        st, body = unix_get(SOCK, "/app/vtoolbox")
        html = body.decode("utf-8")
        check("经 socket GET /app/vtoolbox 返回首页", st == 200 and "<title>视频工具箱</title>" in html)
        check("经 socket 的 base 为 /app/vtoolbox/", '<base href="/app/vtoolbox/">' in html)

        st, body = unix_get(SOCK, "/app/vtoolbox/style.css")
        check("经 socket GET /app/vtoolbox/style.css 返回 CSS", st == 200 and b"{" in body)

        st, body = unix_get(SOCK, "/api/state")
        check("socket 也兼容无前缀路径（网关剥前缀时）", st == 200 and b"roots" in body)

        print("\n== D. 边界 ==")
        try:
            st, _ = http_get(port, "/app/vtoolbox/../server/main.py")
            check("路径穿越被拦截", st in (403, 404), "HTTP %s" % st)
        except urllib.error.HTTPError as exc:
            check("路径穿越被拦截", exc.code in (403, 404), "HTTP %s" % exc.code)

        st, _ = http_get(port, "/app/vtoolboxx/api/state")
        check("相似前缀不被误剥离", st == 404, "HTTP %s" % st)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()

    print("\n通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + "，".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
