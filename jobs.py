# -*- coding: utf-8 -*-
"""后台任务 + 进度。导入/生成/导出都在后台线程跑,界面轮询进度。"""
import threading
import time
import traceback
import uuid

_lk = threading.Lock()
_jobs = {}
MAX_JOBS = 60


class Cancelled(Exception):
    pass


class Ctx:
    """交给任务函数的上下文:报进度、写日志、响应取消。"""

    def __init__(self, job):
        self.job = job

    def set_total(self, n):
        self.job["total"] = int(n)

    def step(self, msg=None, done=None):
        if done is not None:
            self.job["done"] = int(done)
        elif self.job.get("total"):
            self.job["done"] = min(self.job["done"] + 1, self.job["total"])
        if msg:
            self.log(msg)

    def progress(self, done, total, msg=None):
        self.job["done"] = int(done)
        self.job["total"] = int(total)
        if msg:
            self.job["message"] = str(msg)

    def log(self, msg):
        self.job["message"] = str(msg)
        self.job["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
        if len(self.job["log"]) > 400:
            del self.job["log"][:-400]

    def should_stop(self):
        return bool(self.job.get("cancel"))


def _finish(job, status, message=None, result=None, error=None):
    """统一收尾:把进度补满,清掉取消标记,这样界面不会停在半截进度条上。"""
    job["status"] = status
    job["finished"] = time.time()
    job["running"] = False
    job["cancel"] = False          # 结束后不允许再取消
    if result is not None:
        job["result"] = result
    if error is not None:
        job["error"] = error
    if job.get("total"):
        job["done"] = job["total"]  # 关键:完成时进度条必须走到 100%
    if message:
        job["message"] = message
    elif not job.get("message"):
        job["message"] = "完成"


def _run(job, fn):
    ctx = Ctx(job)
    try:
        job["status"] = "running"
        result = fn(ctx)
        if job.get("cancel"):
            _finish(job, "cancelled", "已取消")
        else:
            _finish(job, "done", None, result=result)
    except Cancelled:
        _finish(job, "cancelled", "已取消")
    except Exception as e:
        job["log"].append(traceback.format_exc()[-1500:])
        _finish(job, "error", f"出错: {e}", error=str(e))


def start(kind, title, fn):
    jid = uuid.uuid4().hex[:10]
    job = {
        "id": jid, "kind": kind, "title": title, "status": "queued",
        "done": 0, "total": 0, "message": "排队中…", "log": [],
        "result": None, "error": None, "cancel": False, "running": True,
        "started": time.time(), "finished": None,
    }
    with _lk:
        _jobs[jid] = job
        if len(_jobs) > MAX_JOBS:
            for k in sorted(_jobs, key=lambda k: _jobs[k]["started"])[:-MAX_JOBS]:
                if not _jobs[k]["running"]:
                    _jobs.pop(k, None)
    threading.Thread(target=_run, args=(job, fn), daemon=True).start()
    return job


def get(jid):
    return _jobs.get(jid)


def cancel(jid):
    j = _jobs.get(jid)
    if j and j["running"]:
        j["cancel"] = True
        return True
    return False


def list_jobs(limit=20):
    with _lk:
        out = sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)
    return [{k: v for k, v in j.items() if k != "log"} for j in out[:limit]]


def recent_activity(limit=12):
    out = []
    for j in list_jobs(limit):
        out.append({"id": j["id"], "kind": j["kind"], "title": j["title"],
                    "status": j["status"], "done": j["done"],
                    "total": j["total"], "message": j["message"]})
    return out
