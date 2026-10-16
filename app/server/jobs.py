# -*- coding: utf-8 -*-
"""任务队列：串行/并发执行、进度回写、取消、持久化。"""

import json
import os
import queue
import shutil
import tempfile
import threading
import time
import traceback
import uuid

from media import (
    Canceled, FFToolError, MERGED_DIR_NAME, MERGE_GROUPS, MERGE_POST_ACTIONS,
    check_merge_compatible, do_merge, do_merge_grouped,
    do_trim, fmt_duration, group_files, normalize_orientation_ratio,
    pick_output_ext, resolve_output_path, to_seconds,
)

MAX_LOGS = 300
MAX_JOBS = 60
DEFAULT_OUT_SUFFIX = "_已删片头"
DEFAULT_OUT_SUFFIX_BY_TARGET = {
    "head": "_已删片头",
    "tail": "_已删片尾",
    "both": "_已删片头尾",
}
TRIM_TARGETS = ("head", "tail", "both")

# 合并分组方式的展示名
GROUP_TEXT = {
    "params": "参数分组",
    "resolution": "分辨率分组",
    "orientation": "横竖屏分组",
}


class Ctx(object):
    """单个文件的执行上下文：把 ffmpeg 进度映射成整个任务的进度。"""

    def __init__(self, job, index, count, temp_dir):
        self.job = job
        self.index = index
        self.count = max(1, count)
        self.temp_dir = temp_dir
        self._base = 0.0
        self._weight = 0.0
        self._frac = 0.0

    def log(self, message):
        self.job.add_log(message)

    def step(self, name, weight=None, total=None):
        self._base = min(1.0, self._base + self._weight)
        if weight is None:
            weight = max(0.05, 1.0 - self._base)
        self._weight = weight
        self._frac = 0.0
        self.job.set_step(name)
        self._emit()

    def progress(self, fraction):
        try:
            fraction = float(fraction)
        except (TypeError, ValueError):
            return
        self._frac = max(0.0, min(1.0, fraction))
        self._emit()

    def speed(self, value):
        self.job.set_speed(value)

    def _emit(self):
        inner = min(1.0, self._base + self._weight * self._frac)
        self.job.set_progress((self.index + inner) / self.count)

    def tick_cancel(self):
        self.job.check_cancel()

    def check_cancel(self):
        self.job.check_cancel()

    def register_proc(self, proc):
        self.job.register_proc(proc)

    def unregister_proc(self, proc):
        self.job.unregister_proc(proc)


class Job(object):
    def __init__(self, job_type, files, options):
        self.id = uuid.uuid4().hex[:12]
        self.type = job_type
        self.files = list(files)
        self.options = dict(options or {})
        self.status = "queued"
        self.created = time.time()
        self.started = None
        self.finished = None
        self.progress = 0.0
        self.step = "等待执行"
        self.speed = ""
        self.error = None
        self.result = None
        self.items = [{"src": path, "dst": None, "status": "pending", "error": None}
                      for path in files]
        self.logs = []
        self._procs = []
        self._cancel_flag = threading.Event()
        self._lock = threading.RLock()

    # ---------- 状态 ----------

    def add_log(self, message):
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.logs.append("[%s] %s" % (stamp, message))
            if len(self.logs) > MAX_LOGS:
                del self.logs[:len(self.logs) - MAX_LOGS]

    def set_step(self, name):
        with self._lock:
            self.step = name

    def set_progress(self, value):
        with self._lock:
            self.progress = max(0.0, min(1.0, float(value)))

    def set_speed(self, value):
        with self._lock:
            self.speed = value

    def cancel(self):
        self._cancel_flag.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _terminate(proc)
        if self.status == "queued":
            self.status = "canceled"
            self.step = "已取消"
            self.finished = time.time()

    def check_cancel(self):
        if self._cancel_flag.is_set():
            raise Canceled()

    def register_proc(self, proc):
        with self._lock:
            self._procs.append(proc)

    def unregister_proc(self, proc):
        with self._lock:
            if proc in self._procs:
                self._procs.remove(proc)

    def to_dict(self, with_logs=False):
        with self._lock:
            data = {
                "id": self.id,
                "type": self.type,
                "status": self.status,
                "progress": round(self.progress * 100, 2),
                "step": self.step,
                "speed": self.speed,
                "created": self.created,
                "started": self.started,
                "finished": self.finished,
                "error": self.error,
                "total": len(self.items),
                "done": len([i for i in self.items if i["status"] == "done"]),
                "failed": len([i for i in self.items if i["status"] == "failed"]),
                "items": [dict(i) for i in self.items],
                "options": dict(self.options),
                "result": self.result,
            }
            if with_logs:
                data["logs"] = list(self.logs)
            return data


def _terminate(proc):
    try:
        import signal
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def _pick_runtime_dir(var_dir, tmp_dir=None):
    """挑一个真正可写的运行目录，避免数据卷权限异常时整个应用不可用。

    只以「能否创建并写入」为判据：某些文件系统/沙箱会拒绝 unlink，
    清理探针文件失败不代表目录不可写。
    """
    candidates = [var_dir, tmp_dir, os.path.join(tempfile.gettempdir(), "vtoolbox")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".vtoolbox-write-test-%d" % os.getpid())
            with open(probe, "w") as handle:
                handle.write("ok")
        except OSError:
            continue
        try:
            os.remove(probe)
        except OSError:
            pass
        return candidate
    return tempfile.gettempdir()


class JobManager(object):
    def __init__(self, ffmpeg, var_dir, config_provider, tmp_dir=None):
        self.ff = ffmpeg
        self.var_dir = var_dir
        self.tmp_dir = tmp_dir
        self.config_provider = config_provider
        self.runtime_dir = _pick_runtime_dir(var_dir, tmp_dir)
        self.work_dir = os.path.join(self.runtime_dir, "work")
        self.state_file = os.path.join(self.runtime_dir, "jobs.json")
        self.jobs = {}
        self.order = []
        self.max_workers = 3
        self.active = 0
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._save_lock = threading.RLock()

        try:
            os.makedirs(self.work_dir, exist_ok=True)
        except OSError as exc:
            print("无法创建工作目录 %s：%s" % (self.work_dir, exc), flush=True)

        self._load()
        for _ in range(self.max_workers):
            worker = threading.Thread(target=self._worker, daemon=True)
            worker.start()

    # ---------- 对外接口 ----------

    def submit(self, job_type, files, options):
        if job_type not in ("trim", "merge"):
            raise ValueError("未知的任务类型")
        if not files:
            raise ValueError("请至少选择一个视频文件")
        if job_type == "merge" and len(files) < 2:
            raise ValueError("合并至少需要两个文件")
        job = Job(job_type, files, options)
        with self._lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
        self._queue.put(job.id)
        job.add_log("任务已创建，共 %d 个文件" % len(files))
        self._save()
        return job

    def get(self, job_id):
        with self._lock:
            return self.jobs.get(job_id)

    def list(self):
        with self._lock:
            return [self.jobs[jid] for jid in reversed(self.order) if jid in self.jobs]

    def cancel(self, job_id):
        job = self.get(job_id)
        if not job:
            return False
        job.cancel()
        job.add_log("用户取消任务")
        self._save()
        return True

    def remove(self, job_id):
        with self._lock:
            job = self.jobs.pop(job_id, None)
            if job and job_id in self.order:
                self.order.remove(job_id)
        if job and job.status == "running":
            with self._lock:
                self.jobs[job.id] = job
                self.order.append(job.id)
            return False
        if job:
            self._cleanup_work(job.id)
        self._save()
        return True

    def clear_finished(self):
        with self._lock:
            for job_id in list(self.order):
                job = self.jobs.get(job_id)
                if job and job.status in ("done", "failed", "canceled"):
                    self.jobs.pop(job_id, None)
                    self.order.remove(job_id)
                    self._cleanup_work(job_id)
        self._save()

    def stats(self):
        with self._lock:
            jobs = [self.jobs[jid] for jid in self.order if jid in self.jobs]
        return {
            "total": len(jobs),
            "queued": len([j for j in jobs if j.status == "queued"]),
            "running": len([j for j in jobs if j.status == "running"]),
            "done": len([j for j in jobs if j.status == "done"]),
            "failed": len([j for j in jobs if j.status == "failed"]),
            "canceled": len([j for j in jobs if j.status == "canceled"]),
            "active": self.active,
        }

    # ---------- 工作线程 ----------

    def _concurrency(self):
        try:
            value = int((self.config_provider() or {}).get("concurrency") or 1)
        except (TypeError, ValueError):
            value = 1
        return max(1, min(self.max_workers, value))

    def _worker(self):
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            job = self.get(job_id)
            if job is None:
                continue
            while True:
                with self._lock:
                    if self.active < self._concurrency():
                        self.active += 1
                        break
                    if job._cancel_flag.is_set():
                        self.active += 1
                        break
                time.sleep(0.2)
            try:
                if job.status == "queued":
                    self._run(job)
            except Exception as exc:  # 兜底，避免工作线程退出
                job.status = "failed"
                job.error = "内部错误：%s" % exc
                job.add_log(job.error)
                job.add_log(traceback.format_exc())
                job.finished = time.time()
                self._save()
            finally:
                with self._lock:
                    self.active -= 1
                self._queue.task_done()

    def _make_job_dir(self, job):
        """工作目录逐个兜底，避免数据卷权限异常时整个应用不可用。"""
        last = "未知错误"
        candidates = [
            self.work_dir,
            os.path.join(self.tmp_dir or tempfile.gettempdir(), "vtoolbox-work"),
            os.path.join(tempfile.gettempdir(), "vtoolbox-work"),
        ]
        seen = set()
        for root in candidates:
            if not root or root in seen:
                continue
            seen.add(root)
            path = os.path.join(root, job.id)
            try:
                shutil.rmtree(path, ignore_errors=True)
                os.makedirs(path, exist_ok=True)
                if root != self.work_dir:
                    job.add_log("主工作目录不可用，已改用 %s" % path)
                return path
            except OSError as exc:
                last = "%s：%s" % (root, exc)
                continue
        raise FFToolError("无法创建任务工作目录（%s）" % last)

    def _run(self, job):
        job.status = "running"
        job.started = time.time()
        job.step = "准备中"
        temp_dir = None

        try:
            job.check_cancel()
            self._save()
            temp_dir = self._make_job_dir(job)
            if job.type == "trim":
                self._run_trim(job, temp_dir)
            else:
                self._run_merge(job, temp_dir)
        except Canceled:
            job.status = "canceled"
            job.step = "已取消"
            job.add_log("任务已取消")
        except FFToolError as exc:
            job.status = "failed"
            job.error = str(exc)
            job.add_log("失败：%s" % exc)
        except Exception as exc:
            job.status = "failed"
            job.error = "意外错误：%s" % exc
            job.add_log("意外错误：%s" % exc)
            job.add_log(traceback.format_exc())
        finally:
            job.finished = time.time()
            job.speed = ""
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            if job.status == "running":
                job.status = "done"
                job.progress = 1.0
                job.step = "已完成"
            self._save()

    # ---------- 删片头 ----------

    def _run_trim(self, job, temp_dir):
        config = self.config_provider() or {}
        options = job.options
        target_kind = (options.get("trim_target") or "head").lower()
        if target_kind not in TRIM_TARGETS:
            target_kind = "head"
        head = to_seconds(options.get("head_seconds")) if target_kind in ("head", "both") else 0.0
        tail = to_seconds(options.get("tail_seconds")) if target_kind in ("tail", "both") else 0.0
        mode = options.get("mode") or config.get("trim_mode") or "accurate"
        suffix = options.get("name_suffix")
        if suffix is None:
            suffix = DEFAULT_OUT_SUFFIX_BY_TARGET.get(target_kind, DEFAULT_OUT_SUFFIX)
        output_dir = options.get("output_dir") or ""
        on_exists = options.get("on_exists") or config.get("on_exists") or "rename"
        export_ext = options.get("output_ext") or ""
        trim_options = {
            "trim_target": target_kind,
            "head_seconds": head,
            "tail_seconds": tail,
            "mode": mode,
            "crf": config.get("crf") or 20,
            "preset": config.get("preset") or "veryfast",
            "keyframe_window": 180,
        }
        remove_parts = []
        if target_kind in ("head", "both"):
            remove_parts.append("片头 %s" % fmt_duration(head))
        if target_kind in ("tail", "both"):
            remove_parts.append("片尾 %s" % fmt_duration(tail))
        remove_text = "删除" + " + ".join(remove_parts)

        total = len(job.items)
        self._ensure_output_dir(output_dir)

        for index, item in enumerate(job.items):
            job.check_cancel()
            src = item["src"]
            ctx = Ctx(job, index, total, temp_dir)
            ctx.step("读取视频信息：%s" % os.path.basename(src))
            try:
                info = self.ff.probe(src, use_cache=False)
                if not info.get("has_video"):
                    raise FFToolError("没有视频流")
                ext = pick_output_ext(info.get("ext"), export_ext)
                dst_path = os.path.join(output_dir, _out_name(info["name"], suffix, ext))
                self._check_space(output_dir, info, ctx)

                ctx.log("%s → %s（总长 %s，%s）"
                        % (info["name"], os.path.basename(dst_path),
                           fmt_duration(info["duration"]), remove_text))
                item["dst"] = dst_path
                ctx.step("处理中")
                ctx._base = 0.0
                ctx._weight = 0.0

                result = do_trim(self.ff, src, dst_path, info, trim_options, ctx)
                if result.get("mode") == "fast":
                    ctx.log("提示：快速模式切点对齐最近关键帧，开头可能残留数秒画面")
                item["status"] = "done"
                item["error"] = None
                job.result = result
            except Canceled:
                item["status"] = "canceled"
                raise
            except FFToolError as exc:
                item["status"] = "failed"
                item["error"] = str(exc)
                ctx.log("跳过：%s" % exc)
            except Exception as exc:
                item["status"] = "failed"
                item["error"] = str(exc)
                ctx.log("跳过：%s" % exc)
            job.set_progress((index + 1) / float(total))

        failed = [i for i in job.items if i["status"] == "failed"]
        if failed and len(failed) == len(job.items):
            raise FFToolError("全部 %d 个文件处理失败：%s" % (len(failed), failed[0]["error"]))
        if failed:
            job.add_log("完成，其中 %d 个失败" % len(failed))

    # ---------- 合并 ----------

    def _run_merge(self, job, temp_dir):
        config = self.config_provider() or {}
        options = job.options
        output_dir = options.get("output_dir") or ""
        on_exists = options.get("on_exists") or config.get("on_exists") or "rename"
        mode = options.get("mode") or "auto"
        group_by = (options.get("merge_group") or "params").lower()
        if group_by not in MERGE_GROUPS:
            group_by = "params"
        orientation_ratio = normalize_orientation_ratio(options.get("orientation_ratio"))
        post_action = (options.get("post_action") or "keep").lower()
        if post_action not in MERGE_POST_ACTIONS:
            post_action = "keep"
        self._ensure_output_dir(output_dir)

        ctx = Ctx(job, 0, 1, temp_dir)
        ctx.step("读取视频信息")
        infos = []
        for path in job.files:
            job.check_cancel()
            info = self.ff.probe(path, use_cache=False)
            if not info.get("has_video"):
                raise FFToolError("%s 没有视频流，无法合并" % info["name"])
            infos.append(info)
            ctx.log("已识别 %s（%s）" % (info["name"], fmt_duration(info["duration"])))

        merge_options = {
            "mode": mode,
            "crf": config.get("crf") or 20,
            "preset": config.get("preset") or "veryfast",
            "on_exists": on_exists,
            "output_name": (options.get("output_name") or "").strip(),
            "output_ext": options.get("output_ext") or "",
        }

        compatible, reason = check_merge_compatible(infos)

        # 分组策略：参数（流特征）/ 分辨率（像素尺寸）/ 横竖屏。
        # 「参数」策略下只有直接拼接才需要分组（参数一致就一个组）；
        # 「分辨率」「横竖屏」策略无论直接拼接还是标准合并都要分组 —— 分组
        # 本身就是用户要的语义（同分辨率的合一个、竖屏归竖屏）。
        if group_by == "params":
            want_groups = (mode == "copy") and not compatible
            group_mode = "copy"
        else:
            want_groups = True
            # 直接拼接时按「原始参数」处理；但分辨率组内编码/帧率可能不同，
            # 拼接会失败，因此「按分辨率合并」默认走统一重编码（标准合并）。
            group_mode = "copy" if mode == "copy" else "reencode"

        if want_groups:
            if group_by == "params":
                groups = group_files("params", job.files, infos)
            elif group_by == "resolution":
                groups = group_files("resolution", job.files, infos)
            else:
                groups = group_files("orientation", job.files, infos, orientation_ratio)
            mergeable = [g for g in groups if len(g["paths"]) >= 2]
            if not mergeable:
                if group_by == "params":
                    raise FFToolError("各文件参数不一致，且没有可合并的同参数分组（%s）" % reason)
                raise FFToolError("按当前分组方式没有可合并的分组（每组都只有 1 个文件）")
            ctx.log("按「%s」分为 %d 组，将分别合并为 %d 个视频："
                    % (GROUP_TEXT.get(group_by, group_by), len(groups), len(mergeable)))
            for idx, group in enumerate(groups, start=1):
                ctx.log("  第 %d 组 · %s · %d 个文件"
                        % (idx, group["label"], len(group["paths"])))
            ctx._base = 0.0
            ctx._weight = 0.0
            grouped_options = dict(merge_options)
            grouped_options["group_mode"] = group_mode
            results = do_merge_grouped(self.ff, groups, output_dir,
                                       grouped_options, ctx, job)
            job.items = [{"src": "、".join(r["paths"]), "dst": r["dst"],
                          "status": r["status"] if r["status"] != "skipped" else "done",
                          "error": r["error"], "label": r["label"],
                          "files": r["files"]} for r in results]
            failed = [r for r in results if r["status"] == "failed"]
            if failed and len(failed) == len(results):
                raise FFToolError("全部分组均失败：%s" % failed[0]["error"])
            job.result = {
                "mode": "copy-grouped" if group_mode == "copy" else "reencode-grouped",
                "group_by": group_by,
                "groups": results,
                "group_count": len(results),
                "done_count": len([r for r in results if r["status"] == "done"]),
            }
            # 只有「真正被合并」的源视频才参与合并后处理：
            # 单独成组的文件在 do_merge_grouped 里就被跳过了（没有成品），绝不能被删/被移。
            merged_sources = []
            for entry in results:
                if entry["status"] == "done" and len(entry["paths"]) >= 2:
                    merged_sources.extend(entry["paths"])
            done_n = self._post_merge_sources(job, merged_sources, post_action, ctx)
            job.result["post_action"] = post_action
            job.result["post_sources"] = len(merged_sources)
            job.result["post_done"] = done_n
            job.set_progress(1.0)
            return

        first = infos[0]
        ext = pick_output_ext(first.get("ext"), options.get("output_ext") or "")
        name = (options.get("output_name") or "").strip()
        if not name:
            name = os.path.splitext(first["name"])[0] + "_合并"
        name = _safe_name(name)
        if not name.lower().endswith("." + ext):
            name = "%s.%s" % (name, ext)
        target = resolve_output_path(os.path.join(output_dir, name), on_exists)
        if target is None:
            raise FFToolError("输出文件已存在，已按「跳过」设置终止")

        ctx.log("合并 %d 个文件 → %s" % (len(infos), os.path.basename(target)))
        ctx._base = 0.0
        ctx._weight = 0.0
        result = do_merge(self.ff, job.files, infos, target, merge_options, ctx)
        job.items = [{"src": path, "dst": target, "status": "done", "error": None}
                     for path in job.files]
        if not isinstance(result, dict):
            result = {"mode": mode}
        job.result = result
        # 只有一个文件时不算「合并」，源视频原样保留
        merged_sources = list(job.files) if len(job.files) >= 2 else []
        done_n = self._post_merge_sources(job, merged_sources, post_action, ctx)
        job.result["post_action"] = post_action
        job.result["post_sources"] = len(merged_sources)
        job.result["post_done"] = done_n
        job.set_progress(1.0)

    def _post_merge_sources(self, job, sources, action, ctx):
        """合并成功后处理「参与合并的源视频」：归档到「已合并视频」文件夹，或删除。

        调用方负责筛出真正被合并的文件（同组 ≥ 2 个）。这里只做搬运，并且：
          · 逐个容错，任何一个失败都不影响其余文件，也不影响已经合并好的成品；
          · 目标文件夹里已有同名文件时自动改名，绝不覆盖已有文件；
          · 源文件本来就在「已合并视频」里时跳过，避免嵌套出「已合并视频/已合并视频」。
        返回成功处理的文件数。
        """
        if action not in ("move", "delete") or not sources:
            return 0
        verb = "移动" if action == "move" else "删除"
        ctx.log("合并完成，开始%s %d 个源视频" % (verb, len(sources)))
        done = 0
        for src in sources:
            job.check_cancel()
            name = os.path.basename(src) or src
            if not os.path.exists(src):
                ctx.log("  跳过（源文件已不存在）：%s" % name)
                continue
            try:
                if action == "move":
                    parent = os.path.dirname(src)
                    if os.path.basename(parent) == MERGED_DIR_NAME:
                        ctx.log("  跳过（已在「%s」中）：%s" % (MERGED_DIR_NAME, name))
                        continue
                    folder = os.path.join(parent, MERGED_DIR_NAME)
                    os.makedirs(folder, exist_ok=True)
                    dst = resolve_output_path(os.path.join(folder, name), "rename")
                    if not dst:
                        dst = os.path.join(folder, name)
                    shutil.move(src, dst)
                    ctx.log("  已移动：%s → %s/" % (name, MERGED_DIR_NAME))
                else:
                    os.remove(src)
                    ctx.log("  已删除：%s" % name)
            except OSError as exc:
                ctx.log("  %s失败：%s（%s）" % (verb, name, exc))
                continue
            done += 1
        ctx.log("源视频%s完成：成功 %d 个，失败 %d 个" % (verb, done, len(sources) - done))
        return done

    # ---------- 辅助 ----------

    def _ensure_output_dir(self, output_dir):
        if not output_dir:
            raise FFToolError("请先选择输出目录")
        if not os.path.isdir(output_dir):
            try:
                os.makedirs(output_dir, exist_ok=True)
            except OSError as exc:
                raise FFToolError("无法创建输出目录：%s" % exc)
        if not os.access(output_dir, os.W_OK):
            raise FFToolError("输出目录没有写入权限：%s" % output_dir)

    def _check_space(self, output_dir, info, ctx):
        try:
            stat = os.statvfs(output_dir)
            free = stat.f_bavail * stat.f_frsize
        except OSError:
            return
        need = info.get("size") or 0
        if need and free < need:
            ctx.log("警告：输出目录剩余空间 %.1fGB，小于源文件 %.1fGB，可能写入失败"
                    % (free / 1073741824.0, need / 1073741824.0))

    def _cleanup_work(self, job_id):
        shutil.rmtree(os.path.join(self.work_dir, job_id), ignore_errors=True)

    # ---------- 持久化 ----------

    def _save(self):
        with self._save_lock:
            with self._lock:
                order = list(self.order)[-MAX_JOBS:]
                payload = []
                for job_id in order:
                    job = self.jobs.get(job_id)
                    if job is None:
                        continue
                    data = job.to_dict(with_logs=True)
                    payload.append(data)
            try:
                tmp = self.state_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump({"jobs": payload}, handle, ensure_ascii=False)
                os.replace(tmp, self.state_file)
            except OSError:
                pass

    def _load(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        for entry in data.get("jobs") or []:
            try:
                job = Job(entry.get("type") or "trim", entry.get("files") or [], {})
                job.id = entry.get("id") or job.id
                job.status = entry.get("status") or "failed"
                job.created = entry.get("created") or time.time()
                job.started = entry.get("started")
                job.finished = entry.get("finished")
                job.progress = (entry.get("progress") or 0) / 100.0
                job.step = entry.get("step") or ""
                job.error = entry.get("error")
                job.result = entry.get("result")
                job.items = entry.get("items") or job.items
                job.logs = entry.get("logs") or []
                job.options = entry.get("options") or {}
                job.files = [i.get("src") for i in job.items if i.get("src")]
                if job.status in ("running", "queued"):
                    job.status = "failed"
                    job.error = "服务重启导致任务中断"
                    job.add_log("任务因服务重启而中断，请重新提交")
                self.jobs[job.id] = job
                self.order.append(job.id)
            except Exception:
                continue


def _out_name(src_name, suffix, ext):
    stem = os.path.splitext(src_name)[0]
    return _safe_name("%s%s.%s" % (stem, suffix, ext))


def _safe_name(name):
    name = (name or "").strip().strip("/")
    for ch in ("/", "\\", "\x00"):
        name = name.replace(ch, "_")
    return name or "output"
