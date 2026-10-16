#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频工具箱 —— 飞牛 fnOS 应用后端。

只依赖 Python 标准库；视频处理全部交给同目录 bin/ 下的静态 ffmpeg。
"""

import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jobs import JobManager  # noqa: E402
from media import (  # noqa: E402
    FFToolError, FFmpeg, HW_MODES, MERGED_DIR_NAME, MERGE_GROUPS, MERGE_POST_ACTIONS,
    DEFAULT_ORIENTATION_RATIO, ORIENTATION_RATIO_MAX, ORIENTATION_RATIO_MIN,
    VIDEO_EXTS, check_merge_compatible, cluster_by_hash, compute_cover_hash,
    extract_thumb, fmt_duration, group_files, normalize_orientation_ratio, to_seconds,
)

APP_NAME = "vtoolbox"
APP_VERSION = "1.0.7"

# 飞牛「统一网关」前缀，必须与 app/ui/config 的 gatewayPrefix 一致。
# 网关会把请求按该前缀转发进来；同一条路由同时兼容「带前缀」与「不带前缀」，
# 这样网关访问（公网/桌面内嵌）和直连端口访问（局域网 http://ip:5230）都能工作。
GATEWAY_PREFIX = "/app/vtoolbox"

DEFAULT_CONFIG = {
    "trim_mode": "accurate",
    "concurrency": 1,
    "on_exists": "rename",
    "crf": 20,
    "preset": "veryfast",
    "hwaccel": "off",
    "output_mode": "default",
    "allow_roots": [],
}

MAX_BROWSE_ENTRIES = 3000
MAX_PROBE_FILES = 200
PROBE_WORKERS = 8
MAX_SCAN_FILES = 20000
MAX_COVER_FILES = 600
COVER_WORKERS = 4
THUMB_CACHE_MAX = 1200

# 默认输出位置：default = 应用默认输出目录；current = 跟随当前浏览目录
OUTPUT_MODES = ("default", "current")
TRIM_TARGETS = ("head", "tail", "both")


def log(message):
    sys.stdout.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    sys.stdout.flush()


def strip_gateway_prefix(path):
    """剥离统一网关前缀，使网关访问与直连端口访问共用同一套路由。

    /app/vtoolbox        -> /
    /app/vtoolbox/api/x  -> /api/x
    /api/x               -> /api/x（直连时原样返回）
    """
    if not GATEWAY_PREFIX:
        return path
    if path == GATEWAY_PREFIX:
        return "/"
    if path.startswith(GATEWAY_PREFIX + "/"):
        return path[len(GATEWAY_PREFIX):]
    return path


def page_base(raw_path):
    """由「原始请求路径」推导页面所在目录，用于注入 <base href>。

    前端全部使用相对路径，因此这一步决定了 style.css / api/xxx 的最终去向：
      /                        -> /
      /index.html              -> /
      /app/vtoolbox            -> /app/vtoolbox/
      /app/vtoolbox/index.html -> /app/vtoolbox/
    """
    path = raw_path or "/"
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    elif not path.endswith("/"):
        tail = path.rsplit("/", 1)[-1]
        if "." in tail:                     # 指向具体文件而非目录
            path = path.rsplit("/", 1)[0] + "/"
        else:
            path += "/"
    return path or "/"


class UnixHTTPServer(ThreadingHTTPServer):
    """监听 Unix Socket 的 HTTP 服务，供飞牛统一网关转发。

    官方契约：gatewaySocket 只写文件名，服务在 ${TRIM_APPDEST} 下创建它；
    网关校验登录态后把请求转发到该 socket。
    """

    address_family = socket.AF_UNIX
    daemon_threads = True

    def server_bind(self):
        # 与 HTTPServer.server_bind 的区别：跳过 socket.getfqdn()——
        # Unix Socket 的“地址”是文件路径，getfqdn 会触发无意义的 DNS 查询。
        try:
            os.unlink(self.server_address)
        except OSError:
            pass
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = APP_NAME
        self.server_port = 0
        try:
            os.chmod(self.server_address, 0o777)
        except OSError:
            pass

    def get_request(self):
        request, _ = self.socket.accept()
        return request, ("unix", 0)

    def server_close(self):
        try:
            super(UnixHTTPServer, self).server_close()
        finally:
            try:
                os.unlink(self.server_address)
            except OSError:
                pass


class App(object):
    def __init__(self, args):
        self.args = args
        self.www_dir = os.path.abspath(args.www)
        self.bindir = os.path.abspath(args.bin)
        self.var_dir = os.path.abspath(args.var)
        self.etc_dir = os.path.abspath(args.etc)
        self.tmp_dir = os.path.abspath(args.tmp)
        self.port = args.port
        self.config_file = os.path.join(self.etc_dir, "config.json")
        self._config_lock = threading.RLock()
        self.config = dict(DEFAULT_CONFIG)

        for path in (self.var_dir, self.etc_dir, self.tmp_dir):
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                pass

        self._load_config()
        self.ff = FFmpeg(self.bindir, lambda: self.config)
        self.manager = JobManager(self.ff, self.var_dir, lambda: self.config, self.tmp_dir)
        self._roots_cache = None
        self._roots_stamp = 0.0
        self._cover_cache_file = os.path.join(self.var_dir, "cover-cache.json")
        self._cover_cache = {}
        self._cover_lock = threading.RLock()
        self._thumb_dir = os.path.join(self.tmp_dir, "thumbs")
        self._thumb_lock = threading.RLock()
        self._load_cover_cache()

    # ---------- 封面缓存 ----------

    def _load_cover_cache(self):
        try:
            with open(self._cover_cache_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._cover_cache = data
        except (OSError, ValueError):
            self._cover_cache = {}

    def _save_cover_cache(self):
        try:
            tmp = self._cover_cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._cover_cache, handle)
            os.replace(tmp, self._cover_cache_file)
        except OSError:
            pass

    @staticmethod
    def _cover_key(path):
        try:
            st = os.stat(path)
        except OSError:
            return ""
        return "%s|%d|%d" % (path, st.st_size, int(st.st_mtime))

    def cover_hash(self, path):
        """带磁盘缓存的封面指纹（文件大小/修改时间变了才重算）。"""
        key = self._cover_key(path)
        if not key:
            raise FFToolError("无法访问文件：%s" % os.path.basename(path))
        with self._cover_lock:
            cached = self._cover_cache.get(key)
        if cached is not None:
            return cached
        info = None
        try:
            info = self.ff.probe(path)
        except FFToolError:
            info = None
        value = compute_cover_hash(self.ff, path, info)
        with self._cover_lock:
            self._cover_cache[key] = value
            if len(self._cover_cache) > 20000:
                for stale in list(self._cover_cache)[:5000]:
                    self._cover_cache.pop(stale, None)
        return value

    def _flush_cover_cache(self):
        with self._cover_lock:
            self._save_cover_cache()

    # ---------- 缩略图缓存 ----------

    def thumb_path(self, path):
        key = hashlib.sha1(self._cover_key(path).encode("utf-8")).hexdigest()
        return os.path.join(self._thumb_dir, key + ".jpg")

    def thumb_bytes(self, path):
        target = self.thumb_path(path)
        try:
            with open(target, "rb") as handle:
                data = handle.read()
            if data:
                return data
        except OSError:
            pass
        info = None
        try:
            info = self.ff.probe(path)
        except FFToolError:
            info = None
        data = extract_thumb(self.ff, path, info)
        if data:
            try:
                with self._thumb_lock:
                    os.makedirs(self._thumb_dir, exist_ok=True)
                    tmp = "%s.%d.tmp" % (target, os.getpid())
                    with open(tmp, "wb") as handle:
                        handle.write(data)
                    os.replace(tmp, target)
            except OSError:
                pass
        return data

    def prune_thumbs(self):
        """缩略图目录超过上限时清理最旧的一批。"""
        try:
            entries = []
            for name in os.listdir(self._thumb_dir):
                full = os.path.join(self._thumb_dir, name)
                try:
                    entries.append((os.path.getmtime(full), full))
                except OSError:
                    continue
            if len(entries) <= THUMB_CACHE_MAX:
                return
            entries.sort()
            for _, full in entries[:len(entries) - THUMB_CACHE_MAX]:
                try:
                    os.remove(full)
                except OSError:
                    pass
        except OSError:
            pass

    # ---------- 配置 ----------

    def _load_config(self):
        try:
            with open(self.config_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                merged = dict(DEFAULT_CONFIG)
                merged.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
                self.config = merged
        except (OSError, ValueError):
            pass

    def save_config(self, patch):
        with self._config_lock:
            merged = dict(self.config)
            for key, value in (patch or {}).items():
                if key not in DEFAULT_CONFIG:
                    continue
                merged[key] = value
            try:
                merged["concurrency"] = max(1, min(3, int(merged.get("concurrency") or 1)))
            except (TypeError, ValueError):
                merged["concurrency"] = 1
            try:
                merged["crf"] = max(10, min(35, int(merged.get("crf") or 20)))
            except (TypeError, ValueError):
                merged["crf"] = 20
            if merged.get("trim_mode") not in ("accurate", "fast"):
                merged["trim_mode"] = "accurate"
            if merged.get("on_exists") not in ("rename", "skip", "overwrite"):
                merged["on_exists"] = "rename"
            if merged.get("hwaccel") not in HW_MODES:
                merged["hwaccel"] = "off"
            if merged.get("output_mode") not in OUTPUT_MODES:
                merged["output_mode"] = "default"
            if not isinstance(merged.get("allow_roots"), list):
                merged["allow_roots"] = []
            self.config = merged
            try:
                tmp = self.config_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(merged, handle, ensure_ascii=False, indent=4)
                os.replace(tmp, self.config_file)
            except OSError as exc:
                log("写入配置失败：%s" % exc)
            return merged

    # ---------- 可访问目录 ----------

    def roots(self):
        with self._config_lock:
            return self._compute_roots()

    def _compute_roots(self):
        found = []

        def add_many(value):
            if not value:
                return
            if isinstance(value, (list, tuple)):
                parts = []
                for item in value:
                    parts.extend(re.split(r"[:\n,]", str(item)))
            else:
                parts = re.split(r"[:\n,]", str(value))
            for part in parts:
                part = part.strip()
                if part and os.path.isdir(part):
                    found.append(os.path.realpath(part))

        add_many(os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS"))
        add_many(os.environ.get("TRIM_DATA_SHARE_PATHS"))
        add_many(os.environ.get("VTOOLBOX_ALLOW_ROOTS"))
        add_many(self.config.get("allow_roots"))
        add_many(self.args.allow_root)

        if not found:
            fallback = self.args.dev_root or os.path.expanduser("~")
            if fallback and os.path.isdir(fallback):
                found.append(os.path.realpath(fallback))

        seen = set()
        ordered = []
        for path in found:
            if path not in seen:
                seen.add(path)
                ordered.append(path)
        return ordered

    def default_output_dir(self):
        for root in self.roots():
            if root.replace("\\", "/").endswith("vtoolbox/output") and os.access(root, os.W_OK):
                return root
        for root in self.roots():
            if os.access(root, os.W_OK):
                return root
        return ""

    def is_allowed(self, path):
        if not path:
            return False
        target = os.path.realpath(path)
        for root in self.roots():
            if target == root or target.startswith(root + os.sep):
                return True
        return False

    # ---------- 状态 ----------

    def state(self):
        return {
            "app": {"name": APP_NAME, "version": APP_VERSION,
                    "merged_dir_name": MERGED_DIR_NAME,
                    "merge_groups": list(MERGE_GROUPS),
                    "orientation_ratio_default": DEFAULT_ORIENTATION_RATIO,
                    "orientation_ratio_min": ORIENTATION_RATIO_MIN,
                    "orientation_ratio_max": ORIENTATION_RATIO_MAX},
            "ffmpeg": {
                "available": self.ff.available(),
                "version": self.ff.version() if self.ff.available() else "",
                "path": self.ff.ffmpeg,
                "builtin": self.ff.is_builtin(),
                "source": "内置" if self.ff.is_builtin() else "系统回退",
                "ffprobe": self.ff.ffprobe,
            },
            "roots": self.roots(),
            "default_output_dir": self.default_output_dir(),
            "config": dict(self.config),
            "hwaccel": self.ff.hw_info(),
            "stats": self.manager.stats(),
            "extensions": sorted(VIDEO_EXTS),
            "tmp_dir": self.tmp_dir,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "vtoolbox/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    # ---------- 基础设施 ----------

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        pass

    def log_request(self, code="-", size="-"):
        # 记录每一次请求，用于排查「桌面/网关到底有没有把请求送进来」这类问题。
        # 网关来的请求 client_address 是 ("unix", 0)，直连是 (ip, port)，
        # 因此日志能直接区分「走网关」还是「走端口」。
        try:
            via = "网关" if self.client_address[0] == "unix" else "直连"
            log("访问[%s] %s %s -> %s" % (via, self.command or "-", self.path or "-", code))
        except Exception:
            pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, status=400):
        self._send_json({"error": message}, status)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求内容不是合法的 JSON")
        if not isinstance(data, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return data

    def _query(self):
        parsed = urllib.parse.urlparse(self.path)
        return strip_gateway_prefix(parsed.path), urllib.parse.parse_qs(parsed.query)

    # ---------- 路由 ----------

    def do_GET(self):
        raw_path = urllib.parse.urlparse(self.path).path
        path, query = self._query()
        try:
            if path.startswith("/api/"):
                self._handle_api("GET", path, query, {})
            else:
                self._serve_static(path, raw_path)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._send_error_json("服务端错误：%s" % exc, 500)

    def do_POST(self):
        path, query = self._query()
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send_error_json(str(exc), 400)
            return
        try:
            if path.startswith("/api/"):
                self._handle_api("POST", path, query, body)
            else:
                self._send_error_json("接口不存在", 404)
        except BrokenPipeError:
            pass
        except ValueError as exc:
            self._send_error_json(str(exc), 400)
        except FFToolError as exc:
            self._send_error_json(str(exc), 400)
        except Exception as exc:
            self._send_error_json("服务端错误：%s" % exc, 500)

    def _handle_api(self, method, path, query, body):
        app = self.app
        route = path[len("/api/"):].strip("/")

        if route == "state" and method == "GET":
            self._send_json(app.state())
            return

        if route == "health":
            self._send_json({"ok": True, "version": APP_VERSION})
            return

        if route == "browse" and method == "GET":
            target = (query.get("path") or [""])[0]
            self._send_json(self._browse(app, target))
            return

        if route == "probe" and method == "POST":
            self._send_json(self._probe_many(app, body))
            return

        if route == "scan" and method == "POST":
            self._send_json(self._scan(app, body))
            return

        if route == "analyze" and method == "POST":
            self._send_json(self._analyze(app, body))
            return

        if route == "thumb" and method == "GET":
            self._serve_thumb(app, (query.get("path") or [""])[0])
            return

        if route == "jobs" and method == "GET":
            job_id = (query.get("id") or [""])[0]
            if job_id:
                job = app.manager.get(job_id)
                if not job:
                    self._send_error_json("任务不存在", 404)
                    return
                self._send_json({"job": job.to_dict(with_logs=True)})
                return
            with_logs = (query.get("logs") or ["0"])[0] == "1"
            self._send_json({
                "jobs": [j.to_dict(with_logs=with_logs) for j in app.manager.list()],
                "stats": app.manager.stats(),
            })
            return

        if route == "jobs" and method == "POST":
            kind = (body.get("type") or "").strip()
            files = body.get("files") or []
            options = body.get("options") or {}
            if not isinstance(files, list) or not files:
                raise ValueError("请至少选择一个视频文件")
            for item in files:
                if not app.is_allowed(item):
                    raise ValueError("存在未授权的文件路径：%s" % item)
            if kind == "trim":
                target = (options.get("trim_target") or "head").lower()
                if target not in ("head", "tail", "both"):
                    target = "head"
                options["trim_target"] = target
                if target in ("head", "both"):
                    head = to_seconds(options.get("head_seconds"))
                    if head <= 0:
                        raise ValueError("片头时长必须大于 0")
                    options["head_seconds"] = head
                if target in ("tail", "both"):
                    tail = to_seconds(options.get("tail_seconds"))
                    if tail <= 0:
                        raise ValueError("片尾时长必须大于 0")
                    options["tail_seconds"] = tail
            if kind == "merge":
                # 合并后对源视频的处理：保留 / 移动到「已合并视频」/ 删除
                action = (options.get("post_action") or "keep").lower()
                if action not in MERGE_POST_ACTIONS:
                    action = "keep"
                options["post_action"] = action
                # 分组方式：参数 / 分辨率 / 横竖屏
                group_by = (options.get("merge_group") or "params").lower()
                if group_by not in MERGE_GROUPS:
                    group_by = "params"
                options["merge_group"] = group_by
                options["orientation_ratio"] = normalize_orientation_ratio(
                    options.get("orientation_ratio"))
            options["output_dir"] = (options.get("output_dir") or app.default_output_dir()).strip()
            job = app.manager.submit(kind, files, options)
            self._send_json({"job": job.to_dict()})
            return

        if route == "jobs/cancel" and method == "POST":
            job_id = body.get("id") or ""
            if not app.manager.cancel(job_id):
                self._send_error_json("任务不存在", 404)
                return
            self._send_json({"ok": True})
            return

        if route == "jobs/remove" and method == "POST":
            job_id = body.get("id") or ""
            if not app.manager.remove(job_id):
                self._send_error_json("任务正在执行，无法删除", 400)
                return
            self._send_json({"ok": True})
            return

        if route == "jobs/clear" and method == "POST":
            app.manager.clear_finished()
            self._send_json({"ok": True})
            return

        if route == "config" and method == "GET":
            self._send_json({"config": dict(app.config)})
            return

        if route == "config" and method == "POST":
            updated = app.save_config(body)
            app._roots_cache = None
            self._send_json({"config": updated})
            return

        self._send_error_json("接口不存在：%s" % path, 404)

    # ---------- 目录浏览 ----------

    def _probe_many(self, app, body):
        """并发探测多个文件。

        批量勾选时一次可能几十上百个文件，串行 ffprobe 会拖到很久，
        而飞牛网关（nginx）默认 60 秒读超时，超时即返回 502 —— 这正是
        「选取视频较多时请求失败(http 502)」的成因。这里改用线程池并发处理。
        """
        paths = body.get("paths") or []
        if not isinstance(paths, list):
            raise ValueError("paths 必须是数组")
        if len(paths) > MAX_PROBE_FILES:
            raise ValueError("一次最多探测 %d 个文件" % MAX_PROBE_FILES)
        if not paths:
            return {"items": [], "errors": []}

        def work(path):
            if not isinstance(path, str) or not path:
                return None, {"path": path, "error": "路径无效"}
            if not app.is_allowed(path):
                return None, {"path": path, "error": "路径不在授权目录内"}
            try:
                return app.ff.probe(path, use_cache=False), None
            except FFToolError as exc:
                return None, {"path": path, "error": str(exc)}
            except Exception as exc:            # noqa: BLE001 - 单个文件失败不影响整批
                return None, {"path": path, "error": str(exc)}

        items, errors = [], []
        workers = max(1, min(PROBE_WORKERS, len(paths)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for info, err in pool.map(work, paths):
                if info is not None:
                    items.append(info)
                if err is not None:
                    errors.append(err)
        return {"items": items, "errors": errors}

    def _browse(self, app, target):
        roots = app.roots()
        if not target:
            return {"path": "", "parent": None, "roots": roots, "dirs": [], "videos": [], "count": 0}

        target = os.path.realpath(target)
        if not app.is_allowed(target):
            raise FFToolError("该目录不在授权目录内，请先在「应用设置 → 授权目录」中授予权限")
        if not os.path.isdir(target):
            raise FFToolError("目录不存在或不可访问")

        parent = None
        for root in roots:
            if target != root and (target.startswith(root + os.sep)):
                parent = os.path.dirname(target)
                break

        dirs, videos = [], []
        count = 0
        try:
            entries = sorted(os.scandir(target), key=lambda e: e.name.lower())
        except OSError as exc:
            raise FFToolError("无法读取目录：%s" % exc)

        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append({"name": entry.name, "path": entry.path})
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            ext = os.path.splitext(entry.name)[1].lstrip(".").lower()
            if ext not in VIDEO_EXTS:
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            videos.append({
                "name": entry.name,
                "path": entry.path,
                "ext": ext,
                "size": size,
                "size_text": human_size(size),
            })
            count += 1
            if count >= MAX_BROWSE_ENTRIES:
                break

        dirs.sort(key=lambda d: d["name"].lower())
        videos.sort(key=lambda v: v["name"].lower())
        return {
            "path": target,
            "parent": parent,
            "roots": roots,
            "dirs": dirs,
            "videos": videos,
            "count": count,
        }

    def _scan(self, app, body):
        root = os.path.realpath(body.get("root") or "")
        if not app.is_allowed(root):
            raise FFToolError("该目录不在授权目录内")
        if not os.path.isdir(root):
            raise FFToolError("目录不存在")
        recursive = bool(body.get("recursive", True))
        wanted = body.get("extensions") or []
        if wanted:
            wanted = {str(e).lower().lstrip(".") for e in wanted if e}
        else:
            wanted = set(VIDEO_EXTS)

        found = []
        if recursive:
            for base, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if name.startswith("."):
                        continue
                    ext = os.path.splitext(name)[1].lstrip(".").lower()
                    if ext not in wanted:
                        continue
                    path = os.path.join(base, name)
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        continue
                    found.append({"name": name, "path": path, "ext": ext,
                                  "size": size, "size_text": human_size(size),
                                  "dir": os.path.relpath(base, root)})
                    if len(found) >= MAX_SCAN_FILES:
                        break
                if len(found) >= MAX_SCAN_FILES:
                    break
        else:
            for name in os.listdir(root):
                ext = os.path.splitext(name)[1].lstrip(".").lower()
                if ext not in wanted:
                    continue
                path = os.path.join(root, name)
                if not os.path.isfile(path):
                    continue
                found.append({"name": name, "path": path, "ext": ext,
                              "size": os.path.getsize(path),
                              "size_text": human_size(os.path.getsize(path)),
                              "dir": ""})

        found.sort(key=lambda f: f["path"].lower())
        return {"root": root, "files": found, "count": len(found)}

    # ---------- 分析：参数分组 / 封面分组 ----------

    def _analyze(self, app, body):
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths:
            raise ValueError("请至少选择一个视频文件")
        for item in paths:
            if not app.is_allowed(item):
                raise ValueError("存在未授权的文件路径：%s" % item)
        by = (body.get("by") or "params").lower()
        if by == "cover":
            return self._analyze_cover(app, paths, body)
        return self._analyze_params(app, paths, by, body)

    def _analyze_params(self, app, paths, by="params", body=None):
        if by not in MERGE_GROUPS:
            by = "params"
        ratio = normalize_orientation_ratio((body or {}).get("orientation_ratio")
                                            if by == "orientation" else None)
        infos = []
        errors = []
        for path in paths:
            try:
                info = app.ff.probe(path, use_cache=False)
                if not info.get("has_video"):
                    raise FFToolError("没有视频流")
                infos.append(info)
            except FFToolError as exc:
                errors.append({"path": path, "error": str(exc)})
        if not infos:
            raise FFToolError("所选文件都无法读取：" + (errors[0]["error"] if errors else "未知原因"))

        if by == "orientation":
            groups = group_files("orientation", [i["path"] for i in infos], infos, ratio)
        elif by == "resolution":
            groups = group_files("resolution", [i["path"] for i in infos], infos)
        else:
            groups = group_files("params", [i["path"] for i in infos], infos)
        compatible, reason = check_merge_compatible(infos)
        payload = []
        for index, group in enumerate(groups, start=1):
            payload.append({
                "index": index,
                "label": group["label"],
                "count": len(group["paths"]),
                "duration": group["duration"],
                "duration_text": fmt_duration(group["duration"]),
                "mergeable": len(group["paths"]) >= 2,
                "items": [{"path": info["path"], "name": info["name"],
                           "duration_text": info["duration_text"],
                           "size_text": human_size(info.get("size") or 0)}
                          for info in group["infos"]],
            })
        return {
            "by": by,
            "orientation_ratio": ratio,
            "compatible": compatible,
            "reason": reason,
            "total": len(infos),
            "group_count": len(groups),
            "mergeable_count": len([g for g in payload if g["mergeable"]]),
            "groups": payload,
            "errors": errors,
            "signatures": {},
            "labels": {},
        }

    def _analyze_cover(self, app, paths, body):
        if len(paths) > MAX_COVER_FILES:
            raise ValueError("一次最多分析 %d 个文件，请先缩小选择范围" % MAX_COVER_FILES)
        try:
            threshold = int(body.get("threshold", 6))
        except (TypeError, ValueError):
            threshold = 6
        threshold = max(0, min(16, threshold))

        started = time.time()
        items = []
        errors = []
        cache_hits = 0

        def work(path):
            key = app._cover_key(path)
            with app._cover_lock:
                cached = app._cover_cache.get(key)
            if cached is not None:
                return path, cached, True, None
            try:
                return path, app.cover_hash(path), False, None
            except FFToolError as exc:
                return path, "", False, str(exc)
            except Exception as exc:  # 单个文件失败不该中断整批
                return path, "", False, str(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=COVER_WORKERS) as pool:
            for path, value, hit, error in pool.map(work, paths):
                if hit:
                    cache_hits += 1
                if error:
                    errors.append({"path": path, "error": error})
                    continue
                items.append({"path": path, "name": os.path.basename(path),
                              "hash": value or ""})

        groups = cluster_by_hash(items, threshold)
        payload = []
        for index, group in enumerate(groups, start=1):
            members = group["items"]
            payload.append({
                "index": index,
                "label": "封面相同" if not group.get("weak") else "无法提取封面",
                "count": len(members),
                "hash": group.get("rep") or "",
                "thumb": members[0]["path"] if members else "",
                "items": [{"path": m["path"], "name": m["name"], "hash": m["hash"],
                           "thumb": m["path"]} for m in members],
            })

        if len(items) > 1:
            app._flush_cover_cache()

        duplicates = [g for g in payload if g["count"] > 1]
        return {
            "by": "cover",
            "threshold": threshold,
            "total": len(items),
            "group_count": len(payload),
            "duplicate_groups": len(duplicates),
            "duplicate_files": sum(g["count"] for g in duplicates),
            "elapsed": round(time.time() - started, 2),
            "cached": cache_hits,
            "groups": payload,
            "errors": errors,
        }

    def _serve_thumb(self, app, path):
        if not path or not app.is_allowed(path):
            self._send_error_json("路径不在授权目录内", 403)
            return
        if not os.path.isfile(path):
            self._send_error_json("文件不存在", 404)
            return
        try:
            data = app.thumb_bytes(path)
        except Exception as exc:
            self._send_error_json("生成缩略图失败：%s" % exc, 500)
            return
        if not data:
            self._send_error_json("无法从该文件提取画面", 422)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)
        app.prune_thumbs()

    # ---------- 静态资源 ----------

    def _serve_static(self, path, raw_path=None):
        www = self.app.www_dir
        rel = urllib.parse.unquote(path.split("?", 1)[0])
        if rel in ("", "/"):
            rel = "/index.html"
        candidate = os.path.realpath(os.path.join(www, rel.lstrip("/")))
        if not (candidate == www or candidate.startswith(www + os.sep)):
            self._send_error_json("非法路径", 403)
            return
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "index.html")
        if not os.path.isfile(candidate):
            self._send_error_json("文件不存在", 404)
            return
        ctype, _ = mimetypes.guess_type(candidate)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        try:
            with open(candidate, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            self._send_error_json("读取失败：%s" % exc, 500)
            return

        # 首页注入 <base>：前端统一使用相对路径，注入后无论从
        # http://<内网IP>:5230/ 还是 http(s)://<域名>/app/vtoolbox/ 打开，
        # 资源与接口都能落到同一个前缀下。
        if os.path.basename(candidate) == "index.html" and "html" in ctype:
            base = page_base(raw_path if raw_path is not None else path)
            html = body.decode("utf-8", "replace")
            if "<base" not in html.lower():
                html = html.replace("<head>", '<head>\n<base href="%s">' % base, 1)
            body = html.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def human_size(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return "%d B" % int(size)
            return "%.1f %s" % (size, unit)
        size /= 1024.0


def main():
    parser = argparse.ArgumentParser(description="视频工具箱服务端")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5230)
    parser.add_argument("--www", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "www"))
    parser.add_argument("--bin", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin"))
    parser.add_argument("--var", default="/tmp/vtoolbox/var")
    parser.add_argument("--etc", default="/tmp/vtoolbox/etc")
    parser.add_argument("--tmp", default="/tmp/vtoolbox/tmp")
    parser.add_argument("--allow-root", action="append", default=[],
                        help="额外的可访问目录，可重复")
    parser.add_argument("--dev-root", default="", help="无授权目录时的兜底可访问目录")
    parser.add_argument("--sock", default="", help="统一网关 Unix Socket 路径（飞牛网关入口）")
    args = parser.parse_args()

    app = App(args)

    servers = []
    tcp_server = None
    try:
        tcp_server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        log("错误：无法监听 %s:%d（%s）" % (args.host, args.port, exc))
        return 1
    tcp_server.daemon_threads = True
    tcp_server.app = app
    servers.append(tcp_server)

    # 统一网关入口：飞牛校验登录态后把 /app/vtoolbox/... 转发到该 socket。
    # 它让应用复用飞牛当前的域名与协议，因此公网访问（含 HTTPS）无需额外映射端口。
    unix_server = None
    if args.sock:
        try:
            unix_server = UnixHTTPServer(args.sock, Handler)
            unix_server.daemon_threads = True
            unix_server.app = app
            servers.append(unix_server)
            threading.Thread(target=unix_server.serve_forever, daemon=True).start()
        except OSError as exc:
            unix_server = None
            log("警告：无法创建网关 socket %s（%s）" % (args.sock, exc))
            log("      公网/桌面网关访问将不可用；如需排查请确认 %s 所在目录对运行用户可写。"
                % os.path.dirname(args.sock))

    log("视频工具箱 %s 启动，ffmpeg=%s (%s)" % (APP_VERSION, app.ff.ffmpeg, app.ff.version() or "不可用"))
    if not app.ff.available():
        log("警告：未找到可用的 ffmpeg，任务将无法执行。请确认包内 bin/ffmpeg 存在，或在应用设置中指定 ffmpeg 路径。")
    elif not app.ff.is_builtin():
        log("提示：未使用包内内置 ffmpeg，已回退到系统 ffmpeg（%s）。" % app.ff.ffmpeg)
    log("监听：tcp %s:%d%s" % (args.host, args.port,
                               "，网关前缀 %s" % GATEWAY_PREFIX if unix_server else ""))
    if unix_server:
        log("网关 socket：%s（%s）" % (args.sock, GATEWAY_PREFIX))
    log("可访问目录：%s" % (", ".join(app.roots()) or "（无，请在应用设置中授权目录）"))
    log("运行目录：%s" % app.manager.runtime_dir)
    if os.path.realpath(app.manager.runtime_dir) != os.path.realpath(app.var_dir):
        log("警告：%s 不可写，任务记录将无法跨重启保留（已降级到 %s）"
            % (app.var_dir, app.manager.runtime_dir))
    try:
        tcp_server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for item in servers:
            try:
                item.server_close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
