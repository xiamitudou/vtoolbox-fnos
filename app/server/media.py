# -*- coding: utf-8 -*-
"""ffmpeg / ffprobe 封装：媒体探测、关键帧定位、批量删片头与批量合并。

后端只依赖 Python 标准库。所有重活交给内置的静态 ffmpeg。
"""

import json
import os
import re
import subprocess
import threading

VIDEO_EXTS = {
    "mp4", "mkv", "mov", "m4v", "avi", "ts", "m2ts", "mts", "webm", "flv",
    "wmv", "mpg", "mpeg", "vob", "rmvb", "rm", "3gp", "asf", "f4v", "ogv", "divx",
}

SUPPORTED_OUT_EXTS = {"mp4", "mkv", "m4v", "ts", "webm", "avi", "flv", "mov"}

VIDEO_ENCODER_BY_CODEC = {
    "h264": "libx264",
    "hevc": "libx265",
    "vp9": "libvpx-vp9",
    "vp8": "libvpx",
    "av1": "libsvtav1",
}

# 只有这两种编码参与「两段式精准切割」（编码器参数可与源片对齐）
TWO_STAGE_CODECS = {"h264", "hevc"}

MP4_EXTS = {"mp4", "m4v", "mov"}
STRICT_AUDIO = {"dts", "truehd", "mlp", "eac3"}

CONCAT_SAFE_SEP = ":"

# 合并完成后对「参与合并的源视频」的处理方式。
#   keep   保留不动（默认，最安全）
#   move   移动到源视频各自所在目录下的 MERGED_DIR_NAME 子文件夹里归档
#   delete 永久删除
MERGE_POST_ACTIONS = ("keep", "move", "delete")
MERGED_DIR_NAME = "已合并视频"


class FFToolError(Exception):
    """ffmpeg / ffprobe 调用失败。"""


class Canceled(Exception):
    """任务被用户取消。"""


def to_seconds(value):
    """把 12 / '12.5' / '00:00:12.5' / '1:02:03' 转成秒。"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    if ":" in text:
        parts = text.split(":")
        total = 0.0
        for part in parts:
            total = total * 60 + float(part or 0)
        return total
    return float(text)


def fmt_duration(seconds):
    seconds = max(0.0, float(seconds or 0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if hours:
        return "%d:%02d:%05.2f" % (hours, minutes, secs)
    return "%d:%05.2f" % (minutes, secs)


def split_container(ext):
    ext = (ext or "").lower().lstrip(".")
    if ext in MP4_EXTS:
        return "mp4"
    return ext


# 内置缺失时的系统回退候选（飞牛自带：mediasrv 版含 QSV，/usr/bin 版含 VAAPI）
SYSTEM_FFMPEG_CANDIDATES = (
    "/usr/trim/lib/mediasrv/ffmpeg",
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
)
SYSTEM_FFPROBE_CANDIDATES = (
    "/usr/trim/lib/mediasrv/ffprobe",
    "/usr/bin/ffprobe",
    "/usr/local/bin/ffprobe",
)


def _usable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _resolve_tool(explicit, bindir, name, system_candidates):
    """按 显式指定 → 内置 → PATH → 系统自带 的顺序找一个可用的可执行文件。

    内置 ffmpeg 万一缺失或权限异常，应用仍然可用（回退系统 ffmpeg），
    避免「一个文件不对就整个应用不可用」。
    """
    if _usable(explicit):
        return explicit
    builtin = os.path.join(bindir or "", name)
    if _usable(builtin):
        return builtin
    for candidate in system_candidates:
        if _usable(candidate):
            return candidate
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if _usable(candidate):
            return candidate
    # 都没有时保留原路径，便于报错信息里显示期望位置
    return explicit or builtin


def _find_system_tool(explicit, candidates, name):
    """只从「系统自带」里找（用于硬件加速：内置静态 ffmpeg 不含 VAAPI/QSV）。"""
    if _usable(explicit):
        return explicit
    for candidate in candidates:
        if _usable(candidate):
            return candidate
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if _usable(candidate):
            return candidate
    return ""


# ---------------------------------------------------------------- 硬件加速

# 软件编码器 → 硬件编码器
HW_ENCODER = {
    "vaapi": {"libx264": "h264_vaapi", "libx265": "hevc_vaapi", "libvpx-vp9": "vp9_vaapi"},
    "qsv": {"libx264": "h264_qsv", "libx265": "hevc_qsv", "libsvtav1": "av1_qsv"},
}

HW_KIND_LABEL = {"vaapi": "VAAPI（核显通用）", "qsv": "Intel QSV（核显专用）"}

HW_MODES = ("off", "auto")


def find_drm_device():
    """找一个可用的 /dev/dri 设备节点。"""
    import glob
    for pattern in ("/dev/dri/renderD128", "/dev/dri/renderD*", "/dev/dri/card0", "/dev/dri/card*"):
        for candidate in sorted(glob.glob(pattern)):
            if os.path.exists(candidate):
                return candidate
    return ""


class HwAccel(object):
    """核显加速能力探测。

    要点：包内自带的静态 ffmpeg **不含** VAAPI/QSV（只有 vdpau），
    因此硬件加速必须改用系统 ffmpeg（飞牛自带 /usr/trim/lib/mediasrv/ffmpeg 含 QSV，
    /usr/bin/ffmpeg 含 VAAPI）。

    探测分三步且结果缓存：设备节点是否存在 → 系统 ffmpeg 是否有对应编码器 → 真正试编一帧。
    只有真跑通才算可用，否则一律回退软件编码，绝不让用户任务失败。
    """

    def __init__(self, system_ffmpeg, kind="auto"):
        self.system_ffmpeg = system_ffmpeg or ""
        self.kind = kind
        self._lock = threading.RLock()
        self._status = None

    def status(self, refresh=False):
        with self._lock:
            if self._status is None or refresh:
                self._status = self._probe_kind("vaapi") or self._probe_kind("qsv") \
                    or self._unavailable()
            return dict(self._status)

    def available(self):
        return bool(self.status().get("available"))

    def encoder(self, software_encoder):
        status = self.status()
        if not status.get("available"):
            return ""
        return HW_ENCODER.get(status.get("kind"), {}).get(software_encoder, "")

    def device_args(self):
        status = self.status()
        if not status.get("available"):
            return []
        if status["kind"] == "vaapi":
            return ["-init_hw_device", "vaapi=vtbox:%s" % status["device"],
                    "-filter_hw_device", "vtbox"]
        return ["-init_hw_device", "qsv=vtbox", "-filter_hw_device", "vtbox"]

    def upload_filter(self, high_bit_depth=False):
        """把软件帧上传到显存的滤镜片段（拼在滤镜链末尾）。"""
        status = self.status()
        if not status.get("available"):
            return ""
        if status["kind"] == "qsv":
            return "format=nv12,hwupload=extra_hw_frames=64" if not high_bit_depth else "format=p010,hwupload=extra_hw_frames=64"
        return "format=p010,hwupload" if high_bit_depth else "format=nv12,hwupload"

    # ---------- 内部 ----------

    def _unavailable(self):
        reason = "未检测到可用的核显设备或驱动"
        if not self.system_ffmpeg:
            reason = "系统未提供支持硬件加速的 ffmpeg（包内静态版不含 VAAPI/QSV）"
        elif not find_drm_device():
            reason = "设备上没有 /dev/dri 节点（无核显或未加载驱动）"
        return {"available": False, "kind": "", "device": "", "reason": reason}

    def _probe_kind(self, kind):
        if not _usable(self.system_ffmpeg):
            return None
        device = find_drm_device()
        if kind == "vaapi" and not device:
            return None

        encoder = {"vaapi": "h264_vaapi", "qsv": "h264_qsv"}[kind]
        if not self._has_encoder(encoder):
            return None

        if kind == "vaapi":
            cmd = [self.system_ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
                   "-init_hw_device", "vaapi=vtbox:%s" % device,
                   "-filter_hw_device", "vtbox",
                   "-f", "lavfi", "-i", "nullsrc=s=128x64:d=0.2",
                   "-vf", "format=nv12,hwupload",
                   "-c:v", encoder, "-qp", "26", "-frames:v", "3", "-f", "null", "-"]
        else:
            cmd = [self.system_ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
                   "-init_hw_device", "qsv=vtbox", "-filter_hw_device", "vtbox",
                   "-f", "lavfi", "-i", "nullsrc=s=128x64:d=0.2",
                   "-vf", "format=nv12,hwupload=extra_hw_frames=64",
                   "-c:v", encoder, "-global_quality", "26", "-frames:v", "3",
                   "-f", "null", "-"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return {"available": True, "kind": kind, "device": device,
                "ffmpeg": self.system_ffmpeg, "reason": ""}

    def _has_encoder(self, name):
        try:
            out = subprocess.run([self.system_ffmpeg, "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=30).stdout
        except Exception:
            return False
        return name in (out or "")


class FFmpeg(object):
    def __init__(self, bindir, config_provider=None):
        bindir = bindir or ""
        self.bindir = bindir
        self.config_provider = config_provider or (lambda: {})
        self.ffmpeg = _resolve_tool(os.environ.get("VTOOLBOX_FFMPEG"), bindir,
                                    "ffmpeg", SYSTEM_FFMPEG_CANDIDATES)
        self.ffprobe = _resolve_tool(os.environ.get("VTOOLBOX_FFPROBE"), bindir,
                                     "ffprobe", SYSTEM_FFPROBE_CANDIDATES)
        self.system_ffmpeg = _find_system_tool(os.environ.get("VTOOLBOX_HW_FFMPEG"),
                                               SYSTEM_FFMPEG_CANDIDATES, "ffmpeg")
        self.hw = HwAccel(self.system_ffmpeg, "auto")
        self._version = None
        self._encoders = None
        self._probe_cache = {}
        self._lock = threading.RLock()

    # ---------- 硬件加速 ----------

    def hw_requested(self):
        """用户是否开启了硬件加速（off / auto）。"""
        try:
            value = (self.config_provider() or {}).get("hwaccel") or "off"
        except Exception:
            value = "off"
        return value if value in HW_MODES else "off"

    def hw_info(self):
        """给界面用的硬件加速状态。"""
        requested = self.hw_requested()
        status = self.hw.status()
        status["requested"] = requested
        status["active"] = bool(requested != "off" and status.get("available"))
        status["label"] = HW_KIND_LABEL.get(status.get("kind") or "", "不可用")
        return status

    def use_hw(self, software_encoder):
        """当前这次编码是否走硬件，返回硬件编码器名或空串。"""
        if self.hw_requested() == "off":
            return ""
        return self.hw.encoder(software_encoder)

    def is_builtin(self, tool=None):
        """是否为包内自带的二进制（用于界面提示）。"""
        path = tool or self.ffmpeg
        return os.path.dirname(os.path.abspath(path)) == os.path.abspath(self.bindir or "")

    def describe(self):
        """返回一行人类可读的 ffmpeg 来源说明。"""
        kind = "内置" if self.is_builtin() else "系统回退"
        return "%s（%s）" % (self.ffmpeg, kind)

    # ---------- 基础能力 ----------

    def available(self):
        return os.path.isfile(self.ffmpeg) and os.access(self.ffmpeg, os.X_OK)

    def version(self):
        with self._lock:
            if self._version is None:
                self._version = ""
                try:
                    out = subprocess.run(
                        [self.ffmpeg, "-hide_banner", "-version"],
                        capture_output=True, text=True, timeout=30,
                    ).stdout
                    first = out.splitlines()[0] if out else ""
                    m = re.search(r"ffmpeg version (\S+)", first)
                    self._version = m.group(1) if m else first.strip()
                    if self._is_static(out):
                        self._version += " (static)"
                except Exception:
                    self._version = ""
            return self._version

    @staticmethod
    def _is_static(banner):
        return "gpl" in (banner or "").lower()

    def encoders(self):
        with self._lock:
            if self._encoders is None:
                names = set()
                try:
                    out = subprocess.run(
                        [self.ffmpeg, "-hide_banner", "-encoders"],
                        capture_output=True, text=True, timeout=30,
                    ).stdout
                    for line in out.splitlines():
                        m = re.match(r"\s*[A-Z.]{6}\s+(\S+)", line)
                        if m:
                            names.add(m.group(1))
                except Exception:
                    pass
                self._encoders = names
            return self._encoders

    def has_encoder(self, name):
        return bool(name) and name in self.encoders()

    # ---------- 探测 ----------

    def probe(self, path, use_cache=True):
        with self._lock:
            if use_cache and path in self._probe_cache:
                return self._probe_cache[path]
        try:
            st = os.stat(path)
        except OSError as exc:
            raise FFToolError("无法访问文件：%s" % exc)

        try:
            proc = subprocess.run(
                [self.ffprobe, "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", path],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            raise FFToolError("ffprobe 超时，文件可能损坏或位于慢速外接存储")
        if proc.returncode != 0:
            raise FFToolError((proc.stderr or "").strip().splitlines()[-1] if proc.stderr else "ffprobe 执行失败")

        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError:
            raise FFToolError("ffprobe 输出无法解析")

        streams = data.get("streams") or []
        fmt = data.get("format") or {}

        duration = 0.0
        try:
            duration = float(fmt.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0

        video = None
        audio = []
        info_streams = []
        for s in streams:
            ctype = s.get("codec_type")
            attached = int((s.get("disposition") or {}).get("attached_pic") or 0) == 1
            entry = {
                "type": ctype,
                "codec": s.get("codec_name"),
                "attached_pic": attached,
                "time_base": s.get("time_base"),
            }
            if ctype == "video":
                entry.update({
                    "width": s.get("width"),
                    "height": s.get("height"),
                    "pix_fmt": s.get("pix_fmt"),
                    "fps": self._fps(s),
                    "profile": s.get("profile"),
                })
                if video is None and not attached:
                    video = entry
            elif ctype == "audio":
                entry.update({
                    "channels": s.get("channels"),
                    "sample_rate": int(s.get("sample_rate") or 0),
                    "profile": s.get("profile"),
                })
                audio.append(entry)
            info_streams.append(entry)

        if video is None and duration <= 0:
            raise FFToolError("未检测到视频流（可能不是视频文件）")

        if duration <= 0 and video:
            duration = self._duration_from_stream(video, streams)

        ext = os.path.splitext(path)[1].lstrip(".").lower()
        result = {
            "path": path,
            "name": os.path.basename(path),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "ext": ext,
            "container": (fmt.get("format_name") or "").split(",")[0],
            "duration": duration,
            "duration_text": fmt_duration(duration),
            "vcodec": (video or {}).get("codec"),
            "width": (video or {}).get("width"),
            "height": (video or {}).get("height"),
            "pix_fmt": (video or {}).get("pix_fmt"),
            "profile": (video or {}).get("profile"),
            "fps": (video or {}).get("fps") or 0,
            "vtime_base": (video or {}).get("time_base"),
            "has_video": video is not None,
            "has_audio": len(audio) > 0,
            "audio": audio,
            "streams": info_streams,
        }
        with self._lock:
            if use_cache:
                self._probe_cache[path] = result
        return result

    @staticmethod
    def _fps(stream):
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = stream.get(key) or ""
            if "/" in raw:
                num, den = raw.split("/", 1)
                try:
                    num = float(num)
                    den = float(den)
                except ValueError:
                    continue
                if den > 0 and num > 0:
                    return round(num / den, 4)
        return 0

    @staticmethod
    def _duration_from_stream(video, streams):
        best = 0.0
        for s in streams:
            try:
                best = max(best, float(s.get("duration") or 0))
            except (TypeError, ValueError):
                continue
        return best

    def invalidate(self, path):
        with self._lock:
            self._probe_cache.pop(path, None)

    def keyframe_at_or_after(self, path, seconds, window=180.0):
        """返回不早于 seconds 的第一个关键帧时间点；找不到返回 None。"""
        if seconds <= 0:
            return 0.0
        try:
            proc = subprocess.run(
                [self.ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-read_intervals", "%.3f%%+%.3f" % (max(0.0, seconds - 1.0), window),
                 "-show_entries", "packet=pts_time,flags", "-print_format", "json",
                 path],
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError:
            return None
        best = None
        for pkt in data.get("packets") or []:
            flags = pkt.get("flags") or ""
            if "K" not in flags:
                continue
            try:
                pts = float(pkt.get("pts_time"))
            except (TypeError, ValueError):
                continue
            if pts + 1e-3 < seconds:
                continue
            if best is None or pts < best:
                best = pts
        return best

    def keyframe_at_or_before(self, path, seconds, window=180.0):
        """返回不晚于 seconds 的最后一个关键帧时间点；找不到返回 None。"""
        if seconds <= 0:
            return None
        span = max(1.0, float(window))
        try:
            proc = subprocess.run(
                [self.ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-read_intervals", "%.3f%%+%.3f" % (max(0.0, seconds - span), span + 1.0),
                 "-show_entries", "packet=pts_time,flags", "-print_format", "json",
                 path],
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError:
            return None
        best = None
        for pkt in data.get("packets") or []:
            flags = pkt.get("flags") or ""
            if "K" not in flags:
                continue
            try:
                pts = float(pkt.get("pts_time"))
            except (TypeError, ValueError):
                continue
            if pts > seconds + 1e-3:
                continue
            if best is None or pts > best:
                best = pts
        return best

    # ---------- 执行 ----------

    def run(self, args, ctx=None, total=None, hw=False):
        """执行一条 ffmpeg 命令；ctx 提供进度回写与取消。

        hw=True 时使用系统 ffmpeg + 核显设备（内置静态版不含 VAAPI/QSV）；
        若探测显示硬件不可用，会自动退回软件编码，不影响任务完成。
        """
        use_hw = bool(hw) and self.hw.available()
        if use_hw and not _usable(self.system_ffmpeg):
            use_hw = False
        binary = self.system_ffmpeg if use_hw else self.ffmpeg
        cmd = [binary, "-hide_banner", "-nostdin", "-y"]
        if use_hw:
            cmd += self.hw.device_args()
        if ctx is not None:
            cmd += ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]
        else:
            cmd += ["-loglevel", "error"]
        cmd += [str(a) for a in args]

        if ctx is not None:
            ctx.log("$ " + " ".join(_short(a) for a in cmd))

        tail = []
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, bufsize=1, start_new_session=True,
            )
        except OSError as exc:
            raise FFToolError("无法启动 ffmpeg：%s" % exc)

        if ctx is not None:
            ctx.register_proc(proc)

        drain = threading.Thread(target=_drain_stderr, args=(proc, tail), daemon=True)
        drain.start()

        try:
            for line in proc.stdout:
                if ctx is not None:
                    ctx.tick_cancel()
                self._handle_progress(line, ctx, total)
            rc = proc.wait()
            drain.join(timeout=5)
        finally:
            if ctx is not None:
                ctx.unregister_proc(proc)
            if proc.poll() is None:
                _kill_tree(proc)

        if rc != 0:
            msg = "；".join(l for l in tail[-4:] if l.strip())
            raise FFToolError(msg or "ffmpeg 退出码 %s" % rc)

    @staticmethod
    def _handle_progress(line, ctx, total):
        if ctx is None:
            return
        line = line.strip()
        if not line or "=" not in line:
            return
        key, _, value = line.partition("=")
        value = value.strip()
        if key in ("out_time_us", "out_time_ms") and value not in ("N/A", ""):
            try:
                micros = int(value)
            except ValueError:
                return
            if micros < 0:
                return
            seconds = micros / 1000000.0
            if total and total > 0:
                ctx.progress(seconds / total)
        elif key == "speed" and value not in ("N/A", ""):
            ctx.speed(value)
        elif key == "progress" and value == "end":
            ctx.progress(1.0)


def _short(arg):
    if len(arg) > 120:
        return arg[:117] + "..."
    return arg


def _drain_stderr(proc, sink):
    try:
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                sink.append(line)
                if len(sink) > 40:
                    del sink[0]
    except Exception:
        pass


def _kill_tree(proc):
    try:
        import signal
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------- 输出路径

def pick_output_ext(src_ext, requested=None):
    if requested:
        requested = requested.lower().lstrip(".")
        if requested in SUPPORTED_OUT_EXTS:
            src_ext = requested
    ext = (src_ext or "").lower().lstrip(".")
    if ext == "mov":
        return "mp4"
    if ext in SUPPORTED_OUT_EXTS:
        return ext
    return "mkv"


def movflags_args(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in MP4_EXTS:
        return ["-movflags", "+faststart"]
    return []


def resolve_output_path(dst, on_exists):
    if not os.path.exists(dst):
        return dst
    if on_exists == "overwrite":
        return dst
    if on_exists == "skip":
        return None
    root, ext = os.path.splitext(dst)
    index = 1
    while index < 1000:
        candidate = "%s (%d)%s" % (root, index, ext)
        if not os.path.exists(candidate):
            return candidate
        index += 1
    raise FFToolError("输出文件重名过多，请更换输出目录或文件名")


# ---------------------------------------------------------------- 删片头

def is_high_bit_depth(info):
    pix = (info.get("pix_fmt") or "").lower()
    return "10le" in pix or "12le" in pix or "p010" in pix or "p016" in pix


def audio_plan(info):
    """返回音轨处理策略。

    用输入定位（-ss）复制音轨时，ffmpeg 会从切点之前的关键帧开始复制，
    导致片头段音轨比视频多出一截（实测约 0.5 秒，输出总时长随之变长）。
    因此片头段的音轨一律重新编码；源片音轨本身就是 AAC 时，尾段仍可直接复制。
    """
    audios = info.get("audio") or []
    if not audios:
        return "none"
    return "seg-aac"


def _audio_rate_args(info):
    first = (info.get("audio") or [{}])[0]
    args = ["-b:a", "192k"]
    rate = int(first.get("sample_rate") or 0)
    if rate:
        args += ["-ar", str(rate)]
    channels = int(first.get("channels") or 0)
    if channels:
        args += ["-ac", str(min(channels, 6))]
    return args


def seg_audio_args(info):
    """片头段：音轨重新编码，保证与视频同起点。"""
    if not (info.get("audio") or []):
        return ["-an"]
    return ["-c:a", "aac"] + _audio_rate_args(info)


def tail_audio_args(info):
    """尾段：源片音轨是 AAC 就直接复制，否则统一转 AAC 以保证拼接兼容。"""
    audios = info.get("audio") or []
    if not audios:
        return ["-an"]
    if all((a.get("codec") == "aac") for a in audios):
        return ["-c:a", "copy"]
    return ["-c:a", "aac"] + _audio_rate_args(info)


def final_audio_codec(info):
    if not (info.get("audio") or []):
        return None
    return "aac"


def encode_args(vcodec, info, opts):
    """让片头段的重编码参数尽量与源片对齐，保证可以直接拼接。"""
    args = []
    pix = (info.get("pix_fmt") or "").lower()
    if pix.startswith("yuv") or pix.startswith("gray"):
        args += ["-pix_fmt", info["pix_fmt"]]

    profile = (info.get("profile") or "").lower()
    crf = int(opts.get("crf") or 20)
    preset = opts.get("preset") or "veryfast"

    if vcodec == "h264":
        if profile.startswith("high"):
            args += ["-profile:v", "high"]
        elif profile.startswith("main"):
            args += ["-profile:v", "main"]
        elif profile.startswith("baseline") or profile.startswith("constrained"):
            args += ["-profile:v", "baseline"]
        if profile.startswith("high 10") or profile.startswith("high 4:2:2"):
            args = [a for a in args if a not in ("-profile:v", "high")]
            args += ["-profile:v", "high"]
        args += ["-crf", str(crf), "-preset", preset]
    elif vcodec == "hevc":
        if "10" in profile:
            args += ["-profile:v", "main10"]
        elif profile.startswith("main"):
            args += ["-profile:v", "main"]
        args += ["-crf", str(crf), "-preset", preset, "-x265-params", "log-level=error"]
    else:
        args += ["-crf", str(crf), "-preset", preset]
    return args


def _qsv_preset(preset):
    preset = (preset or "veryfast").lower()
    if preset in ("ultrafast", "superfast", "veryfast", "faster", "fast",
                  "medium", "slow", "slower", "veryslow"):
        return preset
    return "veryfast"


def _hw_quality_args(status, opts):
    """硬件编码的画质参数。

    校验依据：`ffmpeg -h encoder=h264_vaapi` 提供 `-rc_mode`/`-qp`（无 `-preset`）；
    `h264_qsv` 提供 `-preset`；`-global_quality` 是编码器通用选项。
    """
    quality = int(opts.get("crf") or 20)
    if status.get("kind") == "qsv":
        return ["-global_quality", str(quality), "-preset", _qsv_preset(opts.get("preset"))]
    # VAAPI 没有 preset，用 CQP 恒定质量模式，qp 与 CRF 数值区间相近
    return ["-rc_mode", "CQP", "-qp", str(quality)]


def run_with_fallback(ff, build, ctx, total, label="编码"):
    """执行一次编码；硬件编码失败时自动改用软件编码重试一次。

    build(hw_allowed) 需要返回 (args, used_hw)。这样核显驱动/权限异常时
    最多浪费一次启动，不会让整个任务失败。
    """
    args, used_hw = build(True)
    if used_hw:
        status = ff.hw.status()
        ctx.log("使用核显硬件加速（%s，设备 %s）"
                % (status.get("kind", "").upper(), status.get("device") or "默认"))
    try:
        ff.run(args, ctx=ctx, total=total, hw=used_hw)
    except FFToolError as exc:
        if not used_hw:
            raise
        ctx.log("%s硬件编码失败，自动回退软件编码：%s" % (label, exc))
        args, _ = build(False)
        ff.run(args, ctx=ctx, total=total, hw=False)


def _full_reencode_args(ff, src, dst, start, end, info, opts, hw_allowed=True):
    """整片重编码：保留 [start, end)；end 为 None 表示一直到结尾。"""
    build_hw = bool(hw_allowed) and bool(ff.use_hw("libx264"))
    args = ["-ss", "%.3f" % start]
    if end is not None:
        args += ["-t", "%.3f" % max(0.0, end - start)]
    args += ["-i", src,
             "-map", "0:v:0", "-map", "0:a?", "-sn", "-dn"]
    if build_hw:
        args += ["-vf", ff.hw.upload_filter(), "-c:v", ff.use_hw("libx264")]
        args += _hw_quality_args(ff.hw.status(), opts)
    else:
        args += ["-c:v", "libx264", "-crf", str(int(opts.get("crf") or 20)),
                 "-preset", opts.get("preset") or "veryfast", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    args += movflags_args(dst)
    args.append(dst)
    return args, build_hw


TRIM_TARGETS = ("head", "tail", "both")
TRIM_TARGET_LABEL = {"head": "片头", "tail": "片尾", "both": "片头与片尾"}


def trim_bounds(opts, duration):
    """解析删除范围，返回 (target, head, tail, start, end)。

    保留区间是 [start, end)：删片头则 start = 片头时长，删片尾则 end = 总时长 - 片尾时长。
    两种删除方向共用同一套区间模型，只是关键帧落在哪一侧不同。
    """
    target = (opts.get("trim_target") or "head").lower()
    if target not in TRIM_TARGETS:
        target = "head"
    head = to_seconds(opts.get("head_seconds")) if target in ("head", "both") else 0.0
    tail = to_seconds(opts.get("tail_seconds")) if target in ("tail", "both") else 0.0

    if target in ("head", "both") and head <= 0:
        raise FFToolError("片头时长必须大于 0")
    if target in ("tail", "both") and tail <= 0:
        raise FFToolError("片尾时长必须大于 0")
    if duration <= 0:
        raise FFToolError("无法读取视频时长")

    start = max(0.0, head)
    end = duration - max(0.0, tail)
    if end - start <= 0.5:
        raise FFToolError("要删除的时长已达到或超过视频总时长 %s" % fmt_duration(duration))
    return target, head, tail, start, end


def target_label(target):
    return TRIM_TARGET_LABEL.get(target, "片头")


def plan_trim(ff, src, info, opts):
    """决定切割方案，返回 (mode, extra)。

    mode: fast / copy / reencode / two-stage / three-stage
    extra["segments"] 是 [(kind, start, end), ...]，kind 为 reencode 或 copy。
    只删片头时结果与旧版两段式完全一致（重编码片头段 + 复制到尾）。
    """
    duration = info.get("duration") or 0
    if duration <= 0:
        raise FFToolError("无法读取视频时长：%s" % info.get("name"))

    target, head, tail, start, end = trim_bounds(opts, duration)
    cut_tail = end < duration - 1e-3
    base = {"target": target, "head": head, "tail": tail,
            "start": start, "end": end, "cut_tail": cut_tail}

    mode = (opts.get("mode") or "accurate").lower()
    if mode == "fast":
        return "fast", base

    vcodec = info.get("vcodec")
    encoder = VIDEO_ENCODER_BY_CODEC.get(vcodec or "")
    if vcodec not in TWO_STAGE_CODECS:
        base["reason"] = "源编码 %s 不支持两段式切割" % (vcodec or "未知")
        return "reencode", base
    if not ff.has_encoder(encoder):
        base["reason"] = "内置 ffmpeg 缺少 %s 编码器" % encoder
        return "reencode", base
    if vcodec == "h264" and is_high_bit_depth(info):
        base["reason"] = "10bit H.264 需要用 8bit 重新编码"
        return "reencode", base

    window = float(opts.get("keyframe_window") or 180)

    # 片头侧：从 start 之后的第一个关键帧起可以直接复制，之前的一小截重编码
    k_start, head_seg = None, 0.0
    if start > 1e-3:
        k_start = ff.keyframe_at_or_after(src, start, window=window)
        if k_start is None:
            base["reason"] = "未能在切点之后定位到关键帧"
            return "reencode", base
        head_seg = k_start - start
        if head_seg <= 0.08:
            head_seg = 0.0

    # 片尾侧：在 end 之前的最后一个关键帧处截断，之后的一小截重编码
    k_end, tail_seg = None, 0.0
    if cut_tail:
        k_end = ff.keyframe_at_or_before(src, end, window=window)
        if k_end is None or (k_start is not None and k_end <= k_start):
            base["reason"] = "未能在切点之前定位到关键帧"
            return "reencode", base
        tail_seg = end - k_end
        if tail_seg <= 0.08:
            tail_seg = 0.0

    if head_seg <= 0 and tail_seg <= 0:
        return "copy", base

    segments = []
    if head_seg > 0:
        segments.append(("reencode", start, k_start))
    mid_start = k_start if head_seg > 0 else start
    mid_end = k_end if tail_seg > 0 else end
    if mid_end - mid_start > 0.05:
        segments.append(("copy", mid_start, mid_end))
    if tail_seg > 0:
        segments.append(("reencode", k_end, end))

    base["segments"] = segments
    base["head_seg"] = head_seg
    base["tail_seg"] = tail_seg
    return ("three-stage" if (head_seg > 0 and tail_seg > 0) else "two-stage"), base


def _trim_segment_args(ff, src, kind, seg_start, seg_end, duration, info, opts, out_path):
    """构造单段切割命令；seg_end 到达片尾时不加 -t，与旧版「复制到尾」语义一致。"""
    seg_len = seg_end - seg_start
    args = ["-ss", "%.3f" % seg_start, "-i", src]
    if seg_end < duration - 1e-3:
        args += ["-t", "%.3f" % seg_len]
    args += ["-map", "0:v:0", "-map", "0:a?", "-sn", "-dn"]
    if kind == "reencode":
        encoder = VIDEO_ENCODER_BY_CODEC[info["vcodec"]]
        args += ["-c:v", encoder] + encode_args(encoder, info, opts) + seg_audio_args(info)
    else:
        args += ["-c:v", "copy"] + tail_audio_args(info)
    args += ["-avoid_negative_ts", "make_zero", out_path]
    return args


def do_trim(ff, src, dst, info, opts, ctx):
    """执行单个文件的片头 / 片尾删除。"""
    duration = info.get("duration") or 0
    mode, extra = plan_trim(ff, src, info, opts)
    start = extra["start"]
    end = extra["end"]
    cut_tail = extra["cut_tail"]
    keep = end - start
    label = target_label(extra["target"])

    if mode in ("fast", "copy"):
        unknown = mode == "fast"
        title = "快速切割（直接复制）" if unknown else "切点在关键帧上，直接复制"
        ctx.step(title, weight=1.0, total=None if unknown else keep)
        args = ["-ss", "%.3f" % start]
        if cut_tail:
            args += ["-t", "%.3f" % keep]
        args += ["-i", src,
                 "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
                 "-c", "copy", "-avoid_negative_ts", "make_zero"]
        ff.run(args + movflags_args(dst) + [dst],
               ctx=ctx, total=None if unknown else keep)
        return {"mode": mode, "target": extra["target"]}

    if mode == "reencode":
        ctx.log("回退整片重编码：%s" % extra.get("reason"))
        ctx.step("整片重编码（较慢）", weight=1.0, total=keep)
        run_with_fallback(
            ff,
            lambda allow: _full_reencode_args(ff, src, dst, start, end, info, opts, allow),
            ctx, keep, label="整片重编码")
        return {"mode": "reencode", "reason": extra.get("reason"),
                "target": extra["target"]}

    # 两段式 / 三段式：重编码切点附近的一小截，中间大段直接复制
    segments = extra["segments"]
    plan = audio_plan(info)
    if plan == "seg-aac" and not all((a.get("codec") == "aac")
                                     for a in (info.get("audio") or [])):
        ctx.log("源片音轨非 AAC（%s），切割时将统一转为 AAC"
                % ",".join(sorted({(a.get("codec") or "?") for a in (info.get("audio") or [])})))

    if extra.get("head_seg"):
        ctx.log("片头切点 %.3fs，下一个关键帧 %.3fs，需重编码 %.2fs"
                % (start, start + extra["head_seg"], extra["head_seg"]))
    if extra.get("tail_seg"):
        ctx.log("片尾切点 %.3fs，上一个关键帧 %.3fs，需重编码 %.2fs"
                % (end, end - extra["tail_seg"], extra["tail_seg"]))

    reencode_total = float(sum(s[2] - s[1] for s in segments if s[0] == "reencode")) or 1.0
    copy_count = max(1, len([s for s in segments if s[0] == "copy"]))
    parts = []
    for idx, (kind, seg_start, seg_end) in enumerate(segments):
        seg_len = seg_end - seg_start
        temp = os.path.join(ctx.temp_dir, "part%d.mkv" % idx)
        parts.append(temp)
        args = _trim_segment_args(ff, src, kind, seg_start, seg_end,
                                  duration, info, opts, temp)
        if kind == "reencode":
            ctx.step("重编码片段 %.2fs" % seg_len,
                     weight=0.55 * (seg_len / reencode_total), total=seg_len)
            ff.run(args, ctx=ctx, total=seg_len)
        else:
            ctx.step("复制剩余视频", weight=0.30 / copy_count, total=max(0.1, seg_len))
            ff.run(args, ctx=ctx, total=max(0.1, seg_len))

    list_file = os.path.join(ctx.temp_dir, "concat.txt")
    with open(list_file, "w", encoding="utf-8") as handle:
        for part in parts:
            handle.write("file '%s'\n" % part.replace("'", "'\\''"))

    ctx.step("拼接输出", weight=0.15, total=keep)
    out_args = ["-f", "concat", "-safe", "0", "-i", list_file,
                "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-c", "copy"]
    out_args += _strict_args(dst, final_audio_codec(info))
    out_args += movflags_args(dst)
    out_args.append(dst)
    ff.run(out_args, ctx=ctx, total=keep)

    for temp in parts + [list_file]:
        try:
            os.remove(temp)
        except OSError:
            pass

    return {"mode": mode, "target": extra["target"],
            "removed": label, "start": start, "end": end,
            "head_seg": extra.get("head_seg", 0.0),
            "tail_seg": extra.get("tail_seg", 0.0),
            "segments": len(segments), "audio": plan}


def _strict_args(dst, audio_codec):
    ext = os.path.splitext(dst)[1].lower().lstrip(".")
    if ext not in MP4_EXTS:
        return []
    if audio_codec in STRICT_AUDIO:
        return ["-strict", "-2"]
    return []


# ---------------------------------------------------------------- 合并

def merge_signature(info):
    """用于判断能否直接复制拼接的流特征签名。"""
    sig = []
    for s in info.get("streams") or []:
        if s.get("type") == "video":
            if s.get("attached_pic"):
                continue
            sig.append(("v", s.get("codec"), s.get("width"), s.get("height"),
                        s.get("pix_fmt"), round(float(s.get("fps") or 0), 3),
                        s.get("time_base")))
        elif s.get("type") == "audio":
            sig.append(("a", s.get("codec"), s.get("channels"),
                        s.get("sample_rate"), s.get("time_base")))
    return sig


def check_merge_compatible(infos):
    if len(infos) < 2:
        return False, "至少需要两个文件才能合并"
    base = merge_signature(infos[0])
    if not base:
        return False, "第一个文件没有可用的视频流"
    for info in infos[1:]:
        sig = merge_signature(info)
        if sig != base:
            return False, "各文件的编码/分辨率/帧率/音轨参数不一致"
    return True, ""


def signature_key(info):
    """把流特征签名压成一个可比较的字符串键。"""
    return json.dumps(merge_signature(info), sort_keys=True, ensure_ascii=False)


def describe_signature(info):
    """给界面用的人类可读参数说明。"""
    vcodec = (info.get("vcodec") or "?").upper()
    size = "%sx%s" % (info.get("width") or "?", info.get("height") or "?")
    fps = info.get("fps") or 0
    fps_text = ("%.3f" % fps).rstrip("0").rstrip(".") if fps else "?"
    audio = info.get("audio") or []
    if audio:
        acodec = ",".join(sorted({(a.get("codec") or "?").upper() for a in audio}))
        audio_text = "%d 条 %s" % (len(audio), acodec)
    else:
        audio_text = "无音轨"
    pix = info.get("pix_fmt") or ""
    extra = (" " + pix) if pix else ""
    return "%s %s %sfps%s · %s" % (vcodec, size, fps_text, extra, audio_text)


def _bucket_group(files, infos, key_of, label_of):
    """通用分桶：按 key_of(info) 分组，保持文件的原始先后顺序。

    label_of(info) 只在每个桶首次出现时调用一次，避免重复计算。
    """
    buckets = {}
    order = []
    for path, info in zip(files, infos):
        key = key_of(info)
        if key not in buckets:
            buckets[key] = {"key": key, "label": label_of(info),
                            "paths": [], "infos": [], "duration": 0.0}
            order.append(key)
        bucket = buckets[key]
        bucket["paths"].append(path)
        bucket["infos"].append(info)
        bucket["duration"] += info.get("duration") or 0
    return [buckets[key] for key in order]


def group_by_signature(files, infos):
    """按流特征签名分组，保持文件的原始先后顺序。

    返回 [{"key", "label", "paths", "infos", "duration"}]，组内可直接复制拼接。
    """
    return _bucket_group(files, infos, signature_key, describe_signature)


# ---------------------------------------------------------------- 分辨率 / 横竖屏分组

MERGE_GROUPS = ("params", "resolution", "orientation")

# 横竖屏判定：宽高比的「不敏感区」。|w/h - 1| 小于阈值时视为方形/接近方形，
# 单独成组；否则按 w > h 判为横屏、w < h 判为竖屏。默认 0.05（即比例在
# 0.95~1.05 之间算方形），界面可调。
DEFAULT_ORIENTATION_RATIO = 0.05
ORIENTATION_RATIO_MIN = 0.0
ORIENTATION_RATIO_MAX = 1.0


def normalize_orientation_ratio(value):
    """把界面传入的比例范围收敛到合法区间，非法值回落默认值。"""
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return DEFAULT_ORIENTATION_RATIO
    if ratio != ratio:  # NaN
        return DEFAULT_ORIENTATION_RATIO
    return max(ORIENTATION_RATIO_MIN, min(ORIENTATION_RATIO_MAX, ratio))


def resolution_key(info):
    """分辨率分组键：只看画面像素尺寸，忽略编码/帧率（重新编码时无关紧要）。"""
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    return "%dx%d" % (width, height)


def describe_resolution(info):
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    size = "%dx%d" % (width, height)
    # 顺手标一下这套像素尺寸对应的大致画质档位，方便用户一眼认出 1080p/720p
    tier = ""
    short = height if height and height <= width else width
    if short:
        for name, edge in (("8K", 4320), ("4K", 2160), ("2K", 1440),
                           ("1080p", 1080), ("720p", 720), ("480p", 480), ("360p", 360)):
            if short >= edge - 2:
                tier = " · %s" % name
                break
    return "分辨率 %s%s" % (size, tier)


def orientation_of(info, ratio):
    """返回 'landscape' / 'portrait' / 'square'。"""
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    if width <= 0 or height <= 0:
        return "unknown"
    value = float(width) / float(height)
    if abs(value - 1.0) <= ratio:
        return "square"
    return "landscape" if width > height else "portrait"


ORIENTATION_TEXT = {
    "landscape": "横屏",
    "portrait": "竖屏",
    "square": "方形（比例接近 1:1）",
    "unknown": "无法判定画面比例",
}


def orientation_key(info, ratio):
    return orientation_of(info, ratio)


def describe_orientation(info, ratio):
    kind = orientation_of(info, ratio)
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    text = ORIENTATION_TEXT.get(kind, "未知")
    if width and height:
        return "%s · %dx%d（%.2f:1）" % (text, width, height, float(width) / height)
    return text


def group_by_resolution(files, infos):
    """按分辨率（像素尺寸）分组，忽略编码/帧率差异。"""
    return _bucket_group(files, infos, resolution_key, describe_resolution)


def group_by_orientation(files, infos, ratio=None):
    """按横竖屏分组：横屏归横屏、竖屏归竖屏；比例接近方形时单独成组。

    ratio 是「不敏感区」半宽（默认 0.05），界面可调。
    """
    ratio = normalize_orientation_ratio(
        DEFAULT_ORIENTATION_RATIO if ratio is None else ratio)
    return _bucket_group(
        files, infos,
        lambda info: orientation_key(info, ratio),
        lambda info: describe_orientation(info, ratio),
    )


def group_files(strategy, files, infos, ratio=None):
    """按策略分组，供「直接拼接/重新编码」两种合并路径共用。"""
    strategy = (strategy or "params").lower()
    if strategy == "resolution":
        return group_by_resolution(files, infos)
    if strategy == "orientation":
        return group_by_orientation(files, infos, ratio)
    return group_by_signature(files, infos)


def _concat_copy(ff, paths, dst, infos, ctx, total):
    list_file = os.path.join(ctx.temp_dir, "merge.txt")
    with open(list_file, "w", encoding="utf-8") as handle:
        for path in paths:
            handle.write("file '%s'\n" % path.replace("'", "'\\''"))
    args = ["-f", "concat", "-safe", "0", "-i", list_file,
            "-map", "0", "-map_metadata", "0", "-c", "copy"]
    args += _strict_args(dst, ((infos[0].get("audio") or [{}])[0]).get("codec"))
    args += movflags_args(dst)
    args.append(dst)
    ff.run(args, ctx=ctx, total=total)


def do_merge(ff, files, infos, dst, opts, ctx):
    mode = (opts.get("mode") or "auto").lower()
    total = sum((i.get("duration") or 0) for i in infos)
    n = len(files)

    compatible, reason = check_merge_compatible(infos)

    if mode == "copy" and not compatible:
        # 参数不一致时按参数分组，各组分别直接拼接（见 do_merge_grouped）
        raise FFToolError("各文件参数不一致，无法直接拼接（%s）" % reason)

    if mode == "auto":
        mode = "copy" if compatible else "reencode"

    if mode == "copy":
        if not compatible:
            ctx.log("参数不完全一致（%s），自动切换为标准合并" % reason)
            mode = "reencode"
        else:
            ctx.step("直接拼接（不重新编码）", weight=1.0, total=total)
            _concat_copy(ff, files, dst, infos, ctx, total)
            return {"mode": "copy", "duration": total, "groups": 1, "files": n}

    ctx.step("标准合并（统一参数后重新编码）", weight=1.0, total=total)
    silent = {"width": 0, "height": 0, "fps": 0}
    used = {}

    def build(allow):
        args, info_out, hw = _merge_reencode_args(ff, files, infos, dst, opts, allow)
        silent.update(info_out)
        used["hw"] = hw
        return args, hw

    ctx.log("统一输出：%dx%d @ %.3ffps"
            % (_merge_target(infos)[0], _merge_target(infos)[1], _merge_target(infos)[2]))
    run_with_fallback(ff, build, ctx, total, label="标准合并")
    return {"mode": "reencode", "duration": total, "groups": 1, "files": n,
            "target": dict(silent)}


def do_merge_grouped(ff, groups, output_dir, opts, ctx, job=None):
    """按参数分组，每组各自直接拼接为一个文件。

    返回 [{index, label, paths, dst, duration, status, error}]。
    """
    on_exists = opts.get("on_exists") or "rename"
    ext = (opts.get("output_ext") or "").lower().lstrip(".")
    base_name = (opts.get("output_name") or "").strip()
    multi = len(groups) > 1
    results = []

    group_mode = (opts.get("group_mode") or "copy").lower()
    step_verb = "拼接" if group_mode == "copy" else "合并"

    for index, group in enumerate(groups, start=1):
        if job is not None:
            job.check_cancel()
        paths = group["paths"]
        infos = group["infos"]
        out_ext = pick_output_ext(infos[0].get("ext"), ext)
        default = os.path.splitext(infos[0].get("name") or "合并输出")[0] + "_合并"
        name = base_name or default
        if multi:
            name = "%s_%d" % (name, index)
        name = _safe_name(name)
        if not name.lower().endswith("." + out_ext):
            name = "%s.%s" % (name, out_ext)
        dst = resolve_output_path(os.path.join(output_dir, name), on_exists)

        entry = {"index": index, "label": group["label"], "paths": paths,
                 "dst": dst, "duration": group["duration"], "status": "pending",
                 "error": None, "files": len(paths)}
        results.append(entry)

        if len(paths) < 2:
            entry["status"] = "skipped"
            entry["error"] = "该组只有 1 个文件，无法合并"
            ctx.log("第 %d 组（%s）只有 1 个文件，已跳过" % (index, group["label"]))
            continue
        if dst is None:
            entry["status"] = "skipped"
            entry["error"] = "同名文件已存在且设置为跳过"
            ctx.log("第 %d 组输出已存在，已跳过" % index)
            continue

        ctx.log("第 %d/%d 组：%s，%d 个文件 → %s"
                % (index, len(groups), group["label"], len(paths), os.path.basename(dst)))
        ctx.step("%s第 %d 组（%s）" % (step_verb, index, group["label"]),
                 weight=1.0 / max(1, len(groups)), total=group["duration"])
        try:
            if group_mode == "reencode":
                # 组内统一分辨率/帧率后重新编码：可用于「按分辨率分组」之后
                # 把同分辨率但编码/帧率各异的文件真正合成一个画面。
                def _build(allow, _paths=paths, _infos=infos, _dst=dst):
                    args, _info, hw = _merge_reencode_args(
                        ff, _paths, _infos, _dst, opts, allow)
                    return args, hw

                run_with_fallback(ff, _build, ctx, group["duration"],
                                  label="第 %d 组合并" % index)
            else:
                _concat_copy(ff, paths, dst, infos, ctx, group["duration"])
            entry["status"] = "done"
        except FFToolError as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            ctx.log("第 %d 组失败：%s" % (index, exc))

    return results


def _merge_target(infos):
    max_w = max(int(i.get("width") or 0) for i in infos) or 1280
    max_h = max(int(i.get("height") or 0) for i in infos) or 720
    width = max_w - (max_w % 2)
    height = max_h - (max_h % 2)
    fps = max((i.get("fps") or 0) for i in infos) or 30
    fps = min(max(fps, 1), 60)
    return width, height, round(fps, 3)


def _merge_reencode_args(ff, files, infos, dst, opts, hw_allowed=True):
    width, height, fps = _merge_target(infos)

    inputs = []
    for path in files:
        inputs += ["-i", path]

    silent_index = {}
    next_index = len(files)
    for idx, info in enumerate(infos):
        if not info.get("has_audio"):
            inputs += ["-f", "lavfi", "-t", "%.3f" % max(0.2, info.get("duration") or 0),
                       "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            silent_index[idx] = next_index
            next_index += 1

    parts = []
    for idx, info in enumerate(infos):
        parts.append(
            "[%d:v:0]scale=%d:%d:force_original_aspect_ratio=decrease,"
            "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=%s,format=yuv420p[v%d]"
            % (idx, width, height, width, height, fps, idx)
        )
        if info.get("has_audio"):
            parts.append("[%d:a:0]aresample=48000,aformat=sample_fmts=fltp:"
                         "channel_layouts=stereo[a%d]" % (idx, idx))
        else:
            parts.append("[%d:a:0]aformat=sample_fmts=fltp:channel_layouts=stereo[a%d]"
                         % (silent_index[idx], idx))

    chain = "".join("[v%d][a%d]" % (i, i) for i in range(len(files)))
    tail = "%sconcat=n=%d:v=1:a=1[vcat][aout]" % (chain, len(files))

    build_hw = bool(hw_allowed) and bool(ff.use_hw("libx264"))
    if build_hw:
        # 硬编码输入需要把软件帧上传到显存，上传放在 concat 之后只做一次
        tail += ";[vcat]%s[vout]" % ff.hw.upload_filter()
        video_args = ["-c:v", ff.use_hw("libx264")] + _hw_quality_args(ff.hw.status(), opts)
    else:
        tail += ";[vcat]null[vout]"
        video_args = ["-c:v", "libx264", "-crf", str(int(opts.get("crf") or 20)),
                      "-preset", opts.get("preset") or "veryfast", "-pix_fmt", "yuv420p"]

    filter_complex = ";".join(parts) + ";" + tail

    args = inputs + [
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
    ] + video_args + [
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
    ]
    args += movflags_args(dst)
    args.append(dst)
    return args, {"width": width, "height": height, "fps": fps}, build_hw


def _safe_name(name):
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", str(name or "")).strip()
    name = name.strip(". ")
    return name or "输出"


# ---------------------------------------------------------------- 封面指纹

COVER_THUMB_SIZE = "192:-2"


def cover_frame_time(info):
    """取首帧略靠后的位置：避开部分片源的纯黑起幅，同时仍是「封面」那一帧。"""
    duration = (info or {}).get("duration") or 0
    if duration <= 4:
        return 0.0
    return min(1.5, max(0.0, duration * 0.01))


def compute_cover_hash(ff, path, info=None, timeout=90):
    """提取封面帧并算 64 位 dHash（差异哈希），返回 16 位十六进制串。

    dHash 对缩放、轻微压缩差异不敏感，适合判断「封面是否相同」。
    """
    ts = cover_frame_time(info)
    cmd = [ff.ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
           "-ss", "%.3f" % ts, "-i", path, "-frames:v", "1",
           "-vf", "scale=9:8:flags=bilinear,format=gray",
           "-f", "rawvideo", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FFToolError("提取封面超时")
    raw = proc.stdout or b""
    if len(raw) < 72:
        return ""
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if raw[base + col] > raw[base + col + 1] else 0)
    return "%016x" % bits


def hamming_hex(a, b):
    """两个十六进制哈希的汉明距离；无法比较时返回 99。"""
    if not a or not b or len(a) != len(b):
        return 99
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 99


def cluster_by_hash(items, threshold=6):
    """按汉明距离做单链聚类，返回按组大小降序的分组列表。

    items: [{"path","name","hash","duration_text",...}]
    """
    groups = []
    for item in items:
        if not item.get("hash"):
            groups.append({"rep": "", "items": [item], "weak": True})
            continue
        placed = False
        for group in groups:
            if group.get("weak"):
                continue
            if hamming_hex(item["hash"], group["rep"]) <= threshold:
                group["items"].append(item)
                placed = True
                break
        if not placed:
            groups.append({"rep": item["hash"], "items": [item], "weak": False})
    groups.sort(key=lambda g: (-len(g["items"]), g["items"][0]["path"].lower()))
    return groups


def extract_thumb(ff, path, info=None, timeout=90):
    """抽一张 JPEG 缩略图（给界面显示封面用）。"""
    ts = cover_frame_time(info)
    cmd = [ff.ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
           "-ss", "%.3f" % ts, "-i", path, "-frames:v", "1",
           "-vf", "scale=%s" % COVER_THUMB_SIZE, "-f", "image2", "-c:v", "mjpeg",
           "-q:v", "5", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return b""
    if proc.returncode != 0:
        return b""
    return proc.stdout or b""


# ---------------------------------------------------------------- 分组拼接

