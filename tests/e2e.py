#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频工具箱端到端测试。

生成测试视频 → 启动后端 → 通过 HTTP API 提交任务 → 校验输出时长与切点精度。

用法：
    python3 tests/e2e.py [--ffmpeg PATH] [--keep]
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "app", "server", "main.py")
PORT = 5299
BASE = "http://127.0.0.1:%d" % PORT

# 某些环境里 /tmp 是容量极小的 tmpfs（实测仅 10MB），而测试样本动辄几十 MB，
# 会直接报 "No space left on device"。统一把临时目录放到工作区内。
_TEST_TMP = os.environ.get("VTOOLBOX_TEST_TMP") or os.path.join(
    os.path.dirname(ROOT), ".workbuddy", "tmp", "e2e")
try:
    os.makedirs(_TEST_TMP, exist_ok=True)
    tempfile.tempdir = _TEST_TMP
except OSError:
    pass

PASS, FAIL = [], []


def log(message):
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    log("  %s %s%s" % ("[通过]" if ok else "[失败]", name, ("  → " + detail) if detail else ""))


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


# ---------------------------------------------------------------- HTTP

def request(path, method="GET", body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_server(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            request("/api/health")
            return True
        except Exception:
            time.sleep(0.3)
    return False


def wait_job(job_id, timeout=900):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        job = request("/api/jobs?id=" + job_id)["job"]
        if job["status"] != last:
            last = job["status"]
            log("    任务 %s：%s %s" % (job_id, job["status"], job.get("step") or ""))
        if job["status"] in ("done", "failed", "canceled"):
            return job
        time.sleep(0.5)
    raise RuntimeError("任务超时")


# ---------------------------------------------------------------- 媒体工具

def duration_of(ffprobe, path):
    proc = run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", path])
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def streams_of(ffprobe, path):
    proc = run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
                "-of", "json", path])
    return json.loads(proc.stdout or "{}").get("streams", [])


def gray_frame(ffmpeg, path, at=None):
    cmd = [ffmpeg, "-v", "error"]
    if at is not None:
        cmd += ["-ss", "%.3f" % at]
    cmd += ["-i", path, "-frames:v", "1", "-vf", "scale=160:90", "-pix_fmt", "gray",
            "-f", "rawvideo", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    return proc.stdout


def mad(a, b):
    if not a or not b or len(a) != len(b):
        return 255.0
    total = 0
    for i in range(len(a)):
        total += abs(a[i] - b[i])
    return total / float(len(a))


# ---------------------------------------------------------------- 生成样本

def make_sample(ffmpeg, path, seconds, size, rate, vcodec, acodec, extra=None):
    cmd = [ffmpeg, "-y", "-v", "error",
           "-f", "lavfi", "-i", "testsrc2=size=%s:rate=%s:duration=%d" % (size, rate, seconds),
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=%d" % seconds,
           "-c:v", vcodec, "-c:a", acodec, "-shortest"]
    if vcodec == "libx264":
        cmd += ["-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p", "-g", "60"]
    elif vcodec == "mpeg4":
        cmd += ["-q:v", "6"]
    if acodec == "libmp3lame":
        cmd += ["-b:a", "128k"]
    cmd += (extra or []) + [path]
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError("生成样本失败 %s：%s" % (path, proc.stderr[-500:]))


def make_pattern(ffmpeg, path, pattern, seconds, size, rate, vcodec, acodec="aac"):
    """用一个静态图案生成视频：同一图案的不同编码，首帧画面应当一致。"""
    cmd = [ffmpeg, "-y", "-v", "error",
           "-f", "lavfi", "-i", "%s=size=%s:rate=%s" % (pattern, size, rate),
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
           "-t", str(seconds), "-c:v", vcodec, "-c:a", acodec, "-shortest"]
    if vcodec == "libx264":
        cmd += ["-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p", "-g", "50"]
    elif vcodec == "mpeg4":
        cmd += ["-q:v", "5"]
    cmd += [path]
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError("生成图案样本失败 %s：%s" % (path, proc.stderr[-500:]))


def raw_get(path):
    """取二进制响应（/api/thumb 用）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(BASE + path, timeout=60) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", default=os.environ.get("VTOOLBOX_FFMPEG", "ffmpeg"))
    parser.add_argument("--ffprobe", default=os.environ.get("VTOOLBOX_FFPROBE", "ffprobe"))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    ffmpeg, ffprobe = args.ffmpeg, args.ffprobe
    work = tempfile.mkdtemp(prefix="vtoolbox-e2e-")
    samples = os.path.join(work, "samples")
    out_dir = os.path.join(work, "out")
    os.makedirs(samples)
    os.makedirs(out_dir)
    log("工作目录：%s" % work)

    log("\n== 生成测试视频 ==")
    a1 = os.path.join(samples, "a1_长片段.mp4")
    a2 = os.path.join(samples, "a2_短片段.mp4")
    b1 = os.path.join(samples, "b1_异构.avi")
    make_sample(ffmpeg, a1, 62, "1280x720", 30, "libx264", "aac")
    make_sample(ffmpeg, a2, 24, "1280x720", 30, "libx264", "aac")
    make_sample(ffmpeg, b1, 18, "640x480", 25, "mpeg4", "libmp3lame")
    for path in (a1, a2, b1):
        log("  %s  %.2fs" % (os.path.basename(path), duration_of(ffprobe, path)))

    log("\n== 启动后端 ==")
    env = os.environ.copy()
    env["VTOOLBOX_FFMPEG"] = ffmpeg
    env["VTOOLBOX_FFPROBE"] = ffprobe
    env["VTOOLBOX_ALLOW_ROOTS"] = samples + ":" + out_dir
    server = subprocess.Popen(
        [sys.executable, SERVER, "--port", str(PORT),
         "--www", os.path.join(ROOT, "app", "www"),
         "--bin", os.path.join(ROOT, "app", "bin"),
         "--var", os.path.join(work, "var"),
         "--etc", os.path.join(work, "etc"),
         "--tmp", os.path.join(work, "tmp"),
         "--dev-root", work],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        if not wait_server():
            raise RuntimeError("后端启动失败")
        state = request("/api/state")
        log("  ffmpeg=%s 可访问目录=%s" % (state["ffmpeg"]["version"], state["roots"]))
        check("后端可访问授权目录", len(state["roots"]) == 2, str(state["roots"]))

        log("\n== 用例 1：删除片头（精准两段式）==")
        head = 12.5
        job = wait_job(request("/api/jobs", "POST", {
            "type": "trim",
            "files": [a1],
            "options": {"head_seconds": head, "mode": "accurate",
                        "output_dir": out_dir, "name_suffix": "_精准", "output_ext": "mp4"},
        })["job"]["id"])
        check("精准删片头任务成功", job["status"] == "done", job.get("error") or "")
        out1 = os.path.join(out_dir, "a1_长片段_精准.mp4")
        if os.path.isfile(out1):
            expects = duration_of(ffprobe, a1) - head
            actual = duration_of(ffprobe, out1)
            check("输出时长正确（%.2fs，期望 %.2fs）" % (actual, expects), abs(actual - expects) < 0.6)
            src_frame = gray_frame(ffmpeg, a1, head)
            out_frame = gray_frame(ffmpeg, out1, 0)
            diff = mad(src_frame, out_frame)
            check("首帧与源片 %.1fs 处一致（MAD=%.2f）" % (head, diff), diff < 6.0)
            streams = streams_of(ffprobe, out1)
            check("保留视频与音频流", len(streams) >= 2 and any(s["codec_type"] == "audio" for s in streams),
                  str([s.get("codec_name") for s in streams]))
            mode = (job.get("result") or {}).get("mode")
            check("使用了精准切割路径（%s）" % mode, mode in ("two-stage", "copy"))
        else:
            check("生成输出文件", False, out1)

        log("\n== 用例 2：删除片头（快速复制）==")
        job = wait_job(request("/api/jobs", "POST", {
            "type": "trim",
            "files": [a2],
            "options": {"head_seconds": 10, "mode": "fast",
                        "output_dir": out_dir, "name_suffix": "_快速", "output_ext": "mp4"},
        })["job"]["id"])
        check("快速删片头任务成功", job["status"] == "done", job.get("error") or "")
        out2 = os.path.join(out_dir, "a2_短片段_快速.mp4")
        if os.path.isfile(out2):
            actual = duration_of(ffprobe, out2)
            check("输出时长不小于预期（%.2fs，期望 ≥ %.2fs）" % (actual, duration_of(ffprobe, a2) - 10),
                  actual >= duration_of(ffprobe, a2) - 10 - 0.2)

        log("\n== 用例 3：合并视频（参数一致，直接拼接）==")
        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge", "files": [a1, a2],
            "options": {"mode": "auto", "output_dir": out_dir, "output_name": "同源合并"},
        })["job"]["id"])
        check("同源合并任务成功", job["status"] == "done", job.get("error") or "")
        merged = os.path.join(out_dir, "同源合并.mp4")
        if os.path.isfile(merged):
            expects = duration_of(ffprobe, a1) + duration_of(ffprobe, a2)
            actual = duration_of(ffprobe, merged)
            check("合并时长正确（%.2fs，期望 %.2fs）" % (actual, expects), abs(actual - expects) < 0.8)
            mode = (job.get("result") or {}).get("mode")
            check("走直接拼接路径（%s）" % mode, mode == "copy")
        else:
            check("生成合并文件", False, merged)

        log("\n== 用例 4：合并视频（异构参数，自动重编码）==")
        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge", "files": [a1, b1],
            "options": {"mode": "auto", "output_dir": out_dir, "output_name": "异构合并"},
        })["job"]["id"])
        check("异构合并任务成功", job["status"] == "done", job.get("error") or "")
        merged2 = os.path.join(out_dir, "异构合并.mp4")
        if os.path.isfile(merged2):
            streams = streams_of(ffprobe, merged2)
            video = [s for s in streams if s["codec_type"] == "video"]
            expects = duration_of(ffprobe, a1) + duration_of(ffprobe, b1)
            actual = duration_of(ffprobe, merged2)
            check("合并时长正确（%.2fs，期望 %.2fs）" % (actual, expects), abs(actual - expects) < 1.5)
            check("统一到最大分辨率 1280x720",
                  bool(video) and video[0].get("width") == 1280 and video[0].get("height") == 720,
                  str(video[0]) if video else "无视频流")
            mode = (job.get("result") or {}).get("mode")
            check("走重新编码路径（%s）" % mode, mode == "reencode")
        else:
            check("生成异构合并文件", False, merged2)

        log("\n== 用例 5：异常与安全 ==")
        try:
            request("/api/jobs", "POST", {
                "type": "trim", "files": ["/etc/passwd"],
                "options": {"head_seconds": 5, "output_dir": out_dir},
            })
            check("拒绝未授权路径", False, "本应报错")
        except urllib.error.HTTPError as exc:
            check("拒绝未授权路径", exc.code == 400)

        bad = request("/api/jobs", "POST", {
            "type": "trim", "files": [a2],
            "options": {"head_seconds": 9999, "output_dir": out_dir},
        })["job"]["id"]
        bad_job = wait_job(bad, timeout=120)
        check("片头超过视频长度时任务失败", bad_job["status"] == "failed",
              bad_job.get("error") or "")

        try:
            request("/api/jobs", "POST", {
                "type": "merge", "files": [a1],
                "options": {"output_dir": out_dir},
            })
            check("拒绝单文件合并", False, "本应报错")
        except urllib.error.HTTPError as exc:
            check("拒绝单文件合并", exc.code == 400)

        job = request("/api/jobs", "GET")
        check("任务列表可读取", "jobs" in job and isinstance(job["jobs"], list))

        # ---------------- 新增功能 1：按封面（首帧）分组 ----------------

        log("\n== 用例 6：按封面分组（首帧指纹，跨编码/分辨率）==")
        c1 = os.path.join(samples, "cover_大.mp4")      # smptebars 1280x720 h264
        c2 = os.path.join(samples, "cover_小.avi")      # smptebars 640x360  mpeg4
        c3 = os.path.join(samples, "other.mp4")        # rgbtestsrc 图案不同
        make_pattern(ffmpeg, c1, "smptebars", 12, "1280x720", 25, "libx264")
        make_pattern(ffmpeg, c2, "smptebars", 12, "640x360", 25, "mpeg4", "libmp3lame")
        make_pattern(ffmpeg, c3, "rgbtestsrc", 12, "640x360", 25, "libx264")

        cover = request("/api/analyze", "POST",
                        {"by": "cover", "paths": [c1, c2, c3], "threshold": 6})
        same = [g for g in cover["groups"] if g["count"] > 1]
        check("封面分析返回分组", cover.get("total") == 3, "共 %s 个" % cover.get("total"))
        check("能识别出封面相同的一组（%d 组）" % len(same), len(same) == 1)
        if same:
            names = sorted(os.path.basename(i["name"]) for i in same[0]["items"])
            check("同组包含跨编码/跨分辨率的两条（%s）" % "、".join(names),
                  sorted([os.path.basename(c1), os.path.basename(c2)]) == names, str(names))
        check("封面不同的视频不混入同组",
              all(os.path.basename(c3) not in [i["name"] for i in g["items"]] for g in same))

        log("\n== 用例 7：封面缩略图接口 ==")
        try:
            status, ctype, data = raw_get("/api/thumb?path=" + urllib.parse.quote(c1))
            check("缩略图返回 JPEG", status == 200 and ctype.startswith("image/jpeg") and
                  data[:2] == b"\xff\xd8", "type=%s size=%d" % (ctype, len(data)))
            status2, _, data2 = raw_get("/api/thumb?path=" + urllib.parse.quote(c1))
            check("缩略图命中缓存（两次字节一致）", status2 == 200 and data2 == data)
        except Exception as exc:
            check("缩略图返回 JPEG", False, str(exc))

        try:
            raw_get("/api/thumb?path=" + urllib.parse.quote("/etc/hostname"))
            check("缩略图拒绝未授权路径", False, "本应报错")
        except urllib.error.HTTPError as exc:
            check("缩略图拒绝未授权路径", exc.code == 403, "HTTP %d" % exc.code)

        # ---------------- 新增功能 2：参数分组预览 ----------------

        log("\n== 用例 8：参数分组预览（/api/analyze by=params）==")
        analysis = request("/api/analyze", "POST",
                           {"by": "params", "paths": [a1, a2, b1]})
        check("参数不一致被识别", analysis.get("compatible") is False, analysis.get("reason") or "")
        check("给出 2 个参数分组（实际 %s）" % analysis.get("group_count"),
              analysis.get("group_count") == 2)
        check("只有同参数的那组可合并（%s 组）" % analysis.get("mergeable_count"),
              analysis.get("mergeable_count") == 1)
        for group in analysis.get("groups") or []:
            log("    第 %d 组 · %s · %d 个 · %s 秒"
                % (group["index"], group["label"], group["count"], group["duration_text"]))

        # ---------------- 新增功能 3：直接拼接按参数分组各自合并 ----------------

        log("\n== 用例 9：直接拼接 + 参数不一致 → 分组各自合并 ==")
        d1 = os.path.join(samples, "grp_a1.mp4")
        d2 = os.path.join(samples, "grp_a2.mp4")
        d3 = os.path.join(samples, "grp_b1.avi")
        d4 = os.path.join(samples, "grp_b2.avi")
        make_sample(ffmpeg, d1, 12, "1280x720", 30, "libx264", "aac")
        make_sample(ffmpeg, d2, 8, "1280x720", 30, "libx264", "aac")
        make_sample(ffmpeg, d3, 10, "640x480", 25, "mpeg4", "libmp3lame")
        make_sample(ffmpeg, d4, 6, "640x480", 25, "mpeg4", "libmp3lame")
        group_dir = os.path.join(work, "grouped")
        os.makedirs(group_dir, exist_ok=True)
        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge",
            "files": [d1, d2, d3, d4],
            "options": {"mode": "copy", "output_dir": group_dir,
                        "output_name": "分组结果", "output_ext": "mp4"},
        })["job"]["id"])
        check("分组合并任务成功", job["status"] == "done", job.get("error") or "")
        result = job.get("result") or {}
        check("走按参数分组拼接（%s）" % result.get("mode"), result.get("mode") == "copy-grouped")
        check("识别出 2 个参数分组（%s）" % result.get("group_count"),
              result.get("group_count") == 2)
        outs = sorted((result.get("groups") or []), key=lambda g: g["index"])
        check("两组都完成（%s）" % result.get("done_count"), result.get("done_count") == 2)
        if len(outs) == 2:
            check("输出分别为 分组结果_1 / 分组结果_2",
                  os.path.basename(outs[0]["dst"] or "") == "分组结果_1.mp4" and
                  os.path.basename(outs[1]["dst"] or "") == "分组结果_2.mp4",
                  "%s / %s" % (outs[0]["dst"], outs[1]["dst"]))
            for entry, expected in zip(outs, [20.0, 16.0]):
                if entry["dst"] and os.path.isfile(entry["dst"]):
                    dur = duration_of(ffprobe, entry["dst"])
                    check("第 %d 组时长正确（%.2fs，期望约 %.1fs）" % (entry["index"], dur, expected),
                          abs(dur - expected) < 0.6)
                else:
                    check("第 %d 组输出存在" % entry["index"], False, str(entry["dst"]))
            check("两组参数各自保持原样（分辨率为各组自己的）",
                  streams_of(ffprobe, outs[0]["dst"])[0].get("width") == 1280 and
                  streams_of(ffprobe, outs[1]["dst"])[0].get("width") == 640,
                  "%s / %s" % (streams_of(ffprobe, outs[0]["dst"])[0].get("width"),
                               streams_of(ffprobe, outs[1]["dst"])[0].get("width")))

        # ---------------- 新增功能 4：核显硬件加速探测与回退 ----------------

        log("\n== 用例 10：核显硬件加速探测 ==")
        hw = state.get("hwaccel") or {}
        check("状态里带硬件加速信息", "available" in hw and "requested" in hw,
              "available=%s requested=%s reason=%s"
              % (hw.get("available"), hw.get("requested"), hw.get("reason")))
        check("默认不开启（requested=%s）" % hw.get("requested"), hw.get("requested") == "off")
        cfg = request("/api/config", "POST", {"hwaccel": "auto"})
        check("可开启硬件加速（hwaccel=%s）" % cfg["config"].get("hwaccel"),
              cfg["config"].get("hwaccel") == "auto")
        hw2 = (request("/api/state").get("hwaccel") or {})
        check("开启后状态一致", hw2.get("requested") == "auto",
              "available=%s active=%s" % (hw2.get("available"), hw2.get("active")))
        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge",
            "files": [a1, b1],
            "options": {"mode": "reencode", "output_dir": out_dir,
                        "output_name": "hw_合并", "output_ext": "mp4"},
        })["job"]["id"])
        check("开启硬件加速后重编码任务仍成功（不可用则自动回退软件）",
              job["status"] == "done", job.get("error") or "")
        hw_out = os.path.join(out_dir, "hw_合并.mp4")
        check("回退/硬件路径都产出可播放文件",
              os.path.isfile(hw_out) and duration_of(ffprobe, hw_out) > 60)
        job = wait_job(request("/api/jobs", "POST", {
            "type": "trim",
            "files": [a2],
            "options": {"head_seconds": 5, "mode": "accurate",
                        "output_dir": out_dir, "name_suffix": "_hw精准", "output_ext": "mp4"},
        })["job"]["id"])
        check("硬件加速开启时精准切割仍走软件编码并成功",
              job["status"] == "done" and (job.get("result") or {}).get("mode") in ("two-stage", "copy"),
              job.get("error") or str(job.get("result")))
        request("/api/config", "POST", {"hwaccel": "off"})

        # ---------------- 新增：删除片尾 / 片头片尾 / 探测与输出位置配置 ----------------

        log("\n== 用例 11：只删片尾 ==")
        src_dur = duration_of(ffprobe, a1)
        # 7 秒：切点 55s 不在关键帧上（样本每 2 秒一个关键帧），
        # 这样能真正走到「尾部重编码」路径，而不是恰好纯复制。
        tail_seconds = 7
        job = wait_job(request("/api/jobs", "POST", {
            "type": "trim",
            "files": [a1],
            "options": {"trim_target": "tail", "tail_seconds": tail_seconds, "mode": "accurate",
                        "output_dir": out_dir, "name_suffix": "_删片尾", "output_ext": "mp4"},
        })["job"]["id"])
        check("删片尾任务成功", job["status"] == "done", job.get("error") or "")
        tail_out = os.path.join(out_dir, "a1_长片段_删片尾.mp4")
        check("删片尾输出存在", os.path.isfile(tail_out))
        result = job.get("result") or {}
        check("任务结果标注删除范围为片尾", result.get("target") == "tail", str(result))
        check("片尾走的是重编码切点附近一小段（tail_seg=%.2fs，模式 %s）"
              % (result.get("tail_seg") or 0, result.get("mode")),
              (result.get("tail_seg") or 0) > 0 and result.get("mode") == "two-stage",
              str(result))
        if os.path.isfile(tail_out):
            dur = duration_of(ffprobe, tail_out)
            check("删片尾后时长 ≈ 源时长 - %.0fs（源 %.2fs / 实际 %.2fs）"
                  % (tail_seconds, src_dur, dur),
                  abs(dur - (src_dur - tail_seconds)) < 1.2)
            streams = streams_of(ffprobe, tail_out)
            check("删片尾输出视频参数与源片一致（%sx%s）"
                  % (streams[0].get("width") if streams else "-",
                     streams[0].get("height") if streams else "-"),
                  bool(streams) and streams[0].get("width") == 1280
                  and streams[0].get("codec_name") == "h264")
            head_same = mad(gray_frame(ffmpeg, a1), gray_frame(ffmpeg, tail_out))
            check("删片尾不动片头（首帧一致，MAD=%.2f）" % head_same, head_same < 12.0)

        log("\n== 用例 12：片头 + 片尾一起删 ==")
        head_seconds = 5
        job = wait_job(request("/api/jobs", "POST", {
            "type": "trim",
            "files": [a1],
            "options": {"trim_target": "both", "head_seconds": head_seconds,
                        "tail_seconds": tail_seconds, "mode": "accurate",
                        "output_dir": out_dir, "name_suffix": "_删头尾", "output_ext": "mp4"},
        })["job"]["id"])
        check("片头+片尾任务成功", job["status"] == "done", job.get("error") or "")
        both_out = os.path.join(out_dir, "a1_长片段_删头尾.mp4")
        check("片头+片尾输出存在", os.path.isfile(both_out))
        result = job.get("result") or {}
        check("任务结果同时给出头尾切点信息",
              result.get("target") == "both" and "head_seg" in result and "tail_seg" in result,
              str(result))
        check("片头片尾都要重编码切点附近一小段（%s）" % result.get("mode"),
              result.get("mode") == "three-stage"
              and (result.get("head_seg") or 0) > 0 and (result.get("tail_seg") or 0) > 0,
              str(result))
        if os.path.isfile(both_out):
            dur = duration_of(ffprobe, both_out)
            expect = src_dur - head_seconds - tail_seconds
            check("两头都删后时长 ≈ %.0fs（实际 %.2fs）" % (expect, dur), abs(dur - expect) < 1.5)
            offset = mad(gray_frame(ffmpeg, a1, at=head_seconds), gray_frame(ffmpeg, both_out))
            same_start = mad(gray_frame(ffmpeg, a1), gray_frame(ffmpeg, both_out))
            check("输出首帧 ≈ 源片 %.0fs 处、且不等于源片首帧（切头生效，MAD=%.2f / %.2f）"
                  % (head_seconds, offset, same_start), offset < 16.0 and same_start > offset)

        log("\n== 用例 13：批量探测与默认输出位置 ==")
        probe = request("/api/probe", "POST", {"paths": [a1, a2, b1]})
        items = probe.get("items") or []
        check("一次探测多个文件全部返回（%d 个）" % len(items), len(items) == 3,
              str(probe.get("errors")))
        durations = {os.path.basename(i["path"]): round(i.get("duration") or 0, 1) for i in items}
        check("每个文件都读到了时长（%s）" % durations,
              all((v or 0) >= 18 for v in durations.values()))
        check("探测结果带分辨率与编码",
              all(i.get("width") and i.get("vcodec") for i in items))
        too_many = None
        try:
            request("/api/probe", "POST", {"paths": [a1] * 201})
        except urllib.error.HTTPError as exc:
            too_many = exc.code
        check("超过单批上限返回 400（不是网关 502）", too_many == 400, str(too_many))

        cfg = request("/api/config", "POST", {"output_mode": "current"})
        check("输出位置可设为「当前目录」", cfg["config"].get("output_mode") == "current",
              str(cfg["config"].get("output_mode")))
        cfg = request("/api/config", "POST", {"output_mode": "default"})
        check("输出位置可改回「默认目录」", cfg["config"].get("output_mode") == "default")
        cfg = request("/api/config", "POST", {"output_mode": "bogus"})
        check("非法取值被拒绝并回落到默认目录", cfg["config"].get("output_mode") == "default",
              str(cfg["config"].get("output_mode")))

        # ---------------- 单元校验：本机无法跑通的硬件路径与聚类算法 ----------------

        log("\n== 用例 14：硬件加速参数拼装与封面聚类（不依赖真实 GPU）==")
        try:
            sys.path.insert(0, os.path.join(ROOT, "app", "server"))
            import media as M

            hw = M.HwAccel("/usr/bin/ffmpeg")
            hw._status = {"available": True, "kind": "vaapi", "device": "/dev/dri/renderD128",
                          "ffmpeg": "/usr/bin/ffmpeg", "reason": ""}
            check("VAAPI 设备参数正确",
                  hw.device_args() == ["-init_hw_device", "vaapi=vtbox:/dev/dri/renderD128",
                                       "-filter_hw_device", "vtbox"],
                  str(hw.device_args()))
            check("VAAPI 上传滤镜正确（8bit → nv12）",
                  hw.upload_filter() == "format=nv12,hwupload", hw.upload_filter())
            check("VAAPI 上传滤镜正确（10bit → p010）",
                  hw.upload_filter(True) == "format=p010,hwupload", hw.upload_filter(True))
            check("VAAPI 画质参数为 CQP + qp（VAAPI 无 preset）",
                  M._hw_quality_args(hw.status(), {"crf": 22}) == ["-rc_mode", "CQP", "-qp", "22"],
                  str(M._hw_quality_args(hw.status(), {"crf": 22})))
            check("VAAPI 编码器映射正确",
                  hw.encoder("libx264") == "h264_vaapi" and hw.encoder("libx265") == "hevc_vaapi")
            hw._status = {"available": True, "kind": "qsv", "device": "/dev/dri/renderD128",
                          "ffmpeg": "/usr/bin/ffmpeg", "reason": ""}
            check("QSV 设备参数正确", hw.device_args() == ["-init_hw_device", "qsv=vtbox",
                                                        "-filter_hw_device", "vtbox"],
                  str(hw.device_args()))
            check("QSV 画质参数为 global_quality + preset",
                  M._hw_quality_args(hw.status(), {"crf": 22, "preset": "medium"}) ==
                  ["-global_quality", "22", "-preset", "medium"],
                  str(M._hw_quality_args(hw.status(), {"crf": 22, "preset": "medium"})))
            check("QSV 上传滤镜带 extra_hw_frames",
                  hw.upload_filter() == "format=nv12,hwupload=extra_hw_frames=64",
                  hw.upload_filter())

            hw._status = {"available": False, "kind": "", "device": "", "reason": "测试"}
            check("不可用时不返回硬件参数",
                  hw.device_args() == [] and hw.upload_filter() == "" and hw.encoder("libx264") == "")

            # 聚类：完全相同的哈希必须归为一组，差距大的必须分开
            items = [
                {"path": "/a1", "name": "a1", "hash": "0000000000000000"},
                {"path": "/a2", "name": "a2", "hash": "0000000000000001"},   # 距离 1
                {"path": "/b1", "name": "b1", "hash": "ffffffffffffffff"},   # 距离 64
                {"path": "/c1", "name": "c1", "hash": ""},                   # 提取失败
            ]
            groups = M.cluster_by_hash(items, threshold=6)
            check("汉明距离计算正确（1 / 64）",
                  M.hamming_hex("0000000000000000", "0000000000000001") == 1 and
                  M.hamming_hex("0000000000000000", "ffffffffffffffff") == 64)
            check("聚类把相近的归为一组、相差大的分开",
                  len(groups) == 3 and groups[0]["items"][0]["path"] in ("/a1", "/a2") and
                  sum(len(g["items"]) for g in groups) == 4,
                  str([len(g["items"]) for g in groups]))
            strict = M.cluster_by_hash(items, threshold=0)
            check("阈值 0 时只有完全相同的才同组", len(strict) == 4,
                  str([len(g["items"]) for g in strict]))

            sig = M.describe_signature({"vcodec": "h264", "width": 1920, "height": 1080,
                                        "fps": 25.0, "pix_fmt": "yuv420p",
                                        "audio": [{"codec": "aac"}]})
            check("参数分组标签可读（%s）" % sig, "H264" in sig and "1920x1080" in sig and "AAC" in sig)
        except Exception as exc:
            check("硬件与聚类单元校验", False, "%s: %s" % (type(exc).__name__, exc))

        # ---------------- 合并后处理源视频 ----------------

        log("\n== 用例 15：合并后「移动到已合并视频文件夹」 ==")
        move_src = os.path.join(samples, "post_m1.mp4")
        move_src2 = os.path.join(samples, "post_m2.mp4")
        make_sample(ffmpeg, move_src, 10, "1280x720", 30, "libx264", "aac")
        make_sample(ffmpeg, move_src2, 8, "1280x720", 30, "libx264", "aac")
        merged_dir = os.path.join(samples, "已合并视频")
        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge",
            "files": [move_src, move_src2],
            "options": {"mode": "auto", "output_dir": out_dir, "output_ext": "mp4",
                        "post_action": "move", "output_name": "归档测试"},
        })["job"]["id"])
        check("合并（移动模式）任务成功", job["status"] == "done", job.get("error") or "")
        result = job.get("result") or {}
        check("任务结果标注 move 且处理 2 个源视频",
              result.get("post_action") == "move" and result.get("post_sources") == 2
              and result.get("post_done") == 2,
              str({k: result.get(k) for k in ("post_action", "post_sources", "post_done")}))
        check("源视频已离开原目录",
              not os.path.exists(move_src) and not os.path.exists(move_src2))
        check("源视频已归档进「%s」文件夹" % os.path.basename(merged_dir),
              os.path.isfile(os.path.join(merged_dir, "post_m1.mp4")) and
              os.path.isfile(os.path.join(merged_dir, "post_m2.mp4")),
              str(sorted(os.listdir(merged_dir)) if os.path.isdir(merged_dir) else "文件夹不存在"))
        check("成品视频照常生成", os.path.isfile(os.path.join(out_dir, "归档测试.mp4")))

        log("\n== 用例 16：合并后「删除源视频」 ==")
        del_src = os.path.join(samples, "post_d1.mp4")
        del_src2 = os.path.join(samples, "post_d2.mp4")
        make_sample(ffmpeg, del_src, 9, "1280x720", 30, "libx264", "aac")
        make_sample(ffmpeg, del_src2, 7, "1280x720", 30, "libx264", "aac")
        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge",
            "files": [del_src, del_src2],
            "options": {"mode": "auto", "output_dir": out_dir, "output_ext": "mp4",
                        "post_action": "delete", "output_name": "删除测试"},
        })["job"]["id"])
        check("合并（删除模式）任务成功", job["status"] == "done", job.get("error") or "")
        result = job.get("result") or {}
        check("删除模式处理了 2 个源视频",
              result.get("post_action") == "delete" and result.get("post_done") == 2, str(result))
        check("源视频已被删除",
              not os.path.exists(del_src) and not os.path.exists(del_src2))
        check("删除模式不会把文件塞进「已合并视频」",
              not os.path.exists(os.path.join(merged_dir, "post_d1.mp4")))

        log("\n== 用例 17：直接拼接中「单独成组」的视频不受影响 ==")
        solo1 = os.path.join(samples, "solo_a1.mp4")
        solo2 = os.path.join(samples, "solo_a2.mp4")
        solo3 = os.path.join(samples, "solo_b1.avi")
        make_sample(ffmpeg, solo1, 10, "1280x720", 30, "libx264", "aac")
        make_sample(ffmpeg, solo2, 8, "1280x720", 30, "libx264", "aac")
        make_sample(ffmpeg, solo3, 6, "640x480", 25, "mpeg4", "libmp3lame")
        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge",
            "files": [solo1, solo2, solo3],
            "options": {"mode": "copy", "output_dir": out_dir, "output_ext": "mp4",
                        "post_action": "delete", "output_name": "单独组测试"},
        })["job"]["id"])
        check("分组拼接（删除模式）任务成功", job["status"] == "done", job.get("error") or "")
        result = job.get("result") or {}
        check("只有「真正合并」的 2 个源视频被算作已合并（post_sources=%s）"
              % result.get("post_sources"), result.get("post_sources") == 2, str(result))
        check("同组（≥2 个）的源视频被删除",
              not os.path.exists(solo1) and not os.path.exists(solo2))
        check("单独成组（仅 1 个）的视频被完整保留、未被删除", os.path.isfile(solo3))

        log("\n== 用例 18：非法 post_action 回落为「保留不动」 ==")
        keep1 = os.path.join(samples, "keep_1.mp4")
        keep2 = os.path.join(samples, "keep_2.mp4")
        make_sample(ffmpeg, keep1, 8, "1280x720", 30, "libx264", "aac")
        make_sample(ffmpeg, keep2, 6, "1280x720", 30, "libx264", "aac")
        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge",
            "files": [keep1, keep2],
            "options": {"mode": "auto", "output_dir": out_dir, "output_ext": "mp4",
                        "post_action": "bogus", "output_name": "保留测试"},
        })["job"]["id"])
        check("非法取值被拒绝并回落为 keep", (job.get("result") or {}).get("post_action") == "keep",
              str((job.get("result") or {}).get("post_action")))
        check("keep 模式下不处理任何源文件（post_done=0）",
              (job.get("result") or {}).get("post_done") == 0,
              str((job.get("result") or {}).get("post_done")))
        check("源视频原样保留", os.path.isfile(keep1) and os.path.isfile(keep2))

        # ---------------- 新增：按分辨率分组 ----------------

        log("\n== 用例 19：合并方式「按分辨率」分组 ==")
        r1 = os.path.join(samples, "res_1080_a.mp4")
        r2 = os.path.join(samples, "res_1080_b.mp4")
        r3 = os.path.join(samples, "res_720_a.mp4")
        r4 = os.path.join(samples, "res_720_b.mp4")
        # 同分辨率但编码/帧率不同：验证「分辨率相同就归为一组」
        make_sample(ffmpeg, r1, 8, "1920x1080", 30, "libx264", "aac")
        make_sample(ffmpeg, r2, 6, "1920x1080", 25, "libx264", "aac")
        make_sample(ffmpeg, r3, 8, "1280x720", 30, "libx264", "aac")
        make_sample(ffmpeg, r4, 6, "1280x720", 24, "libx264", "aac")

        analysis = request("/api/analyze", "POST",
                           {"by": "resolution", "paths": [r1, r2, r3, r4]})
        check("分辨率分组识别出 2 组（实际 %s）" % analysis.get("group_count"),
              analysis.get("group_count") == 2, str(analysis))
        check("分辨率分组的 by 字段为 resolution", analysis.get("by") == "resolution")
        labels = " ".join(g["label"] for g in analysis.get("groups") or [])
        check("分组标签含两种分辨率（1920x1080 / 1280x720）",
              "1920x1080" in labels and "1280x720" in labels, labels)
        check("每组都有 2 个文件可合并", analysis.get("mergeable_count") == 2,
              str(analysis.get("mergeable_count")))

        res_dir = os.path.join(work, "res_grouped")
        os.makedirs(res_dir, exist_ok=True)
        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge",
            "files": [r1, r2, r3, r4],
            "options": {"mode": "reencode", "merge_group": "resolution",
                        "output_dir": res_dir, "output_name": "分辨率分组",
                        "output_ext": "mp4"},
        })["job"]["id"])
        check("按分辨率分组合并任务成功", job["status"] == "done", job.get("error") or "")
        result = job.get("result") or {}
        check("走按分辨率分组重编码（%s）" % result.get("mode"),
              result.get("mode") == "reencode-grouped", str(result.get("mode")))
        check("分辨率分组产出 2 个视频（%s）" % result.get("done_count"),
              result.get("done_count") == 2, str(result.get("group_count")))
        res_outs = sorted(glob.glob(os.path.join(res_dir, "分辨率分组_*.mp4")))
        check("输出两个分辨率分组成品（实际 %d 个）" % len(res_outs), len(res_outs) == 2)
        sizes = set()
        for path in res_outs:
            streams = streams_of(ffprobe, path)
            video = [s for s in streams if s["codec_type"] == "video"]
            if video:
                sizes.add("%sx%s" % (video[0]["width"], video[0]["height"]))
        check("两个成品分辨率各不相同（%s）" % sizes,
              sizes == {"1920x1080", "1280x720"}, str(sizes))

        # ---------------- 新增：按横竖屏分组 ----------------

        log("\n== 用例 20：合并方式「按横竖屏」分组（含比例容差）==")
        o_land1 = os.path.join(samples, "ori_land1.mp4")
        o_land2 = os.path.join(samples, "ori_land2.mp4")
        o_port1 = os.path.join(samples, "ori_port1.mp4")
        o_port2 = os.path.join(samples, "ori_port2.mp4")
        o_square = os.path.join(samples, "ori_square.mp4")
        make_sample(ffmpeg, o_land1, 8, "1280x720", 30, "libx264", "aac")
        make_sample(ffmpeg, o_land2, 6, "1920x1080", 30, "libx264", "aac")
        make_sample(ffmpeg, o_port1, 8, "720x1280", 30, "libx264", "aac")
        make_sample(ffmpeg, o_port2, 6, "1080x1920", 30, "libx264", "aac")
        make_sample(ffmpeg, o_square, 6, "1080x1080", 30, "libx264", "aac")

        analysis = request("/api/analyze", "POST", {
            "by": "orientation", "orientation_ratio": 0.05,
            "paths": [o_land1, o_land2, o_port1, o_port2, o_square],
        })
        check("横竖屏分组识别出 3 组（横/竖/方形，实际 %s）" % analysis.get("group_count"),
              analysis.get("group_count") == 3, str(analysis))
        check("回显生效的比例容差（%s）" % analysis.get("orientation_ratio"),
              abs(float(analysis.get("orientation_ratio") or 0) - 0.05) < 1e-6)
        ori_labels = [g["label"] for g in (analysis.get("groups") or [])]
        ori_text = " ".join(ori_labels)
        check("分组标签区分横屏/竖屏/方形",
              "横屏" in ori_text and "竖屏" in ori_text and "方形" in ori_text, ori_text)
        # 横屏组应同时含 1280x720 与 1920x1080（说明是按方向而非按分辨率分组）
        land_group = [g for g in (analysis.get("groups") or []) if g["label"].startswith("横屏")]
        check("横屏组把两种横屏分辨率归为一组（count=%s）"
              % (land_group[0]["count"] if land_group else "-"),
              bool(land_group) and land_group[0]["count"] == 2, str(ori_labels))

        ori_dir = os.path.join(work, "ori_grouped")
        os.makedirs(ori_dir, exist_ok=True)
        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge",
            "files": [o_land1, o_land2, o_port1, o_port2, o_square],
            "options": {"mode": "reencode", "merge_group": "orientation",
                        "orientation_ratio": 0.05, "output_dir": ori_dir,
                        "output_name": "横竖屏分组", "output_ext": "mp4"},
        })["job"]["id"])
        check("按横竖屏分组合并任务成功", job["status"] == "done", job.get("error") or "")
        result = job.get("result") or {}
        check("横竖屏分组只产出 2 个成品（方形组仅 1 个被跳过，done=%s）"
              % result.get("done_count"), result.get("done_count") == 2,
              str([(r["label"], r["status"], r["files"]) for r in result.get("groups") or []]))
        ori_outs = sorted(glob.glob(os.path.join(ori_dir, "横竖屏分组_*.mp4")))
        check("输出两个横竖屏分组成品（实际 %d 个）" % len(ori_outs), len(ori_outs) == 2)
        ori_sizes = set()
        for path in ori_outs:
            video = [s for s in streams_of(ffprobe, path) if s["codec_type"] == "video"]
            if video:
                ori_sizes.add("%sx%s" % (video[0]["width"], video[0]["height"]))
        check("横屏成品与竖屏成品各一个（%s）" % ori_sizes,
              ori_sizes == {"1920x1080", "1080x1920"}, str(ori_sizes))
        # 方形视频没有被合并，源文件应仍存在
        check("方形（接近 1:1）源视频未被合并、保持原样", os.path.isfile(o_square))

        # 容差调大到 50%：竖屏 720x1280(0.5625) 不会被算方形；但 1080x1080 仍是方形。
        # 这里验证「容差 = 0」时方形视频按真实宽高比归组（1:1 既不算横也不算竖 →
        # 仍单独成组，因为 1080 > 1080 不成立、1080 < 1080 也不成立）。
        zero = request("/api/analyze", "POST", {
            "by": "orientation", "orientation_ratio": 0,
            "paths": [o_land1, o_port1, o_square],
        })
        zero_labels = " ".join(g["label"] for g in (zero.get("groups") or []))
        check("容差 0 时 1:1 视频仍单独成组（%s）" % zero_labels,
              zero.get("group_count") == 3, str(zero_labels))

        log("\n== 用例 21：非法 merge_group / orientation_ratio 回落 ==")
        fb1 = os.path.join(samples, "fb_1.mp4")
        fb2 = os.path.join(samples, "fb_2.mp4")
        make_sample(ffmpeg, fb1, 8, "1280x720", 30, "libx264", "aac")
        make_sample(ffmpeg, fb2, 6, "1280x720", 30, "libx264", "aac")
        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge",
            "files": [fb1, fb2],
            "options": {"mode": "auto", "merge_group": "bogus",
                        "orientation_ratio": "oops", "output_dir": out_dir,
                        "output_ext": "mp4", "output_name": "回落测试"},
        })["job"]["id"])
        check("非法 merge_group 被回落（任务仍成功）", job["status"] == "done", job.get("error") or "")

        job = wait_job(request("/api/jobs", "POST", {
            "type": "merge",
            "files": [fb1, fb2],
            "options": {"mode": "auto", "output_dir": out_dir,
                        "output_ext": "mp4", "output_name": "比例回落测试"},
        })["job"]["id"])
        # 未传 merge_group 时默认按参数；两个文件参数一致 → 合成单个视频（非分组模式）
        check("未传 merge_group 时按参数默认合并（单成品）",
              job["status"] == "done"
              and (job.get("result") or {}).get("mode") != "copy-grouped"
              and not (job.get("result") or {}).get("group_by"),
              str(job.get("result")))

    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    log("\n================ 结果 ================")
    log("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for name in FAIL:
        log("  失败：%s" % name)
    if args.keep:
        log("工作目录保留：%s" % work)
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
