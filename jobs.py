# -*- coding: utf-8 -*-
"""后台任务 + 进度。导入/生成/导出都在后台线程跑,界面轮询进度。"""
import threading
import time
import traceback
import uuid

_lk = threading.Lock()
_jobs = {}
MAX_JOBS = 60

# 「在途条目」和「要跳过的条目」—— ⚠️ 刻意**不放进 job 字典**。
# 因为 /api/job 会把 job 整个丢给 json_resp,而 set 不是 JSON 类型,
# 塞进去会让**每一次轮询都 500**(进度窗口直接空掉),而且看不出是为什么。
_live = {}                          # jid -> {"inflight": {key: t0}, "skip": set()}


class Cancelled(Exception):
    pass


class Ctx:
    """交给任务函数的上下文:报进度、写日志、响应取消/跳过。"""

    def __init__(self, job):
        self.job = job
        self.jid = job.get("id")

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

    # ---------------------------------------------------------- 跳过单条 ----
    # 用户原话:「如果有遇到卡壳的我可以中途跳过(多链接时候),我有跳过这一章
    # 继续的按键,确保我可以一定做完,而不是只能取消」。
    #
    # 「取消」是整批不要了,「跳过」是**丢掉这一条、后面的继续**。
    # 两者必须分开:只有一个"取消"的时候,一个卡住的页面会逼用户放弃整批。
    def begin_item(self, key):
        with _lk:
            _live.setdefault(self.jid, {"inflight": {}, "skip": set()})
            _live[self.jid]["inflight"][key] = time.time()
            n = len(_live[self.jid]["inflight"])
        # `alive` 是**给界面和排查用的**(在途条数),必须是 JSON 能序列化的标量 ——
        # 所以放 job 字典里的只有这个数,`inflight`/`skip` 那两个集合仍然留在 _live。
        self.job["alive"] = n

    def end_item(self, key):
        with _lk:
            d = _live.get(self.jid)
            if d:
                d["inflight"].pop(key, None)
                n = len(d["inflight"])
            else:
                n = 0
        self.job["alive"] = n

    def should_skip(self, key):
        with _lk:
            d = _live.get(self.jid)
            return bool(d and key in d["skip"])

    def request_skip(self):
        """把**跑得最久的那个在途条目**标成"跳过"。

        ⚠️ 为什么不把全部在途一起跳掉:同时有 3 条在跑,而真正卡住的通常
        只有一个 —— 全跳会白扔两条正常抓取的。卡住的那条**正好就是跑了
        最久的**,所以"挑最久的那个"既简单又准。
        再点一次就跳下一个,直到用户满意。
        """
        with _lk:
            d = _live.get(self.jid)
            if not d or not d["inflight"]:
                return None
            key = oldest(d["inflight"])
            d["skip"].add(key)
            return key


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
    with _lk:                      # 收尾后把在途表扔掉,别一直涨
        _live.pop(job.get("id"), None)


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
        "started": time.time(), "finished": None, "alive": 0,
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


def oldest(inflight):
    """在途表里**跑得最久**的那一个(启动时间最早的)。

    ⚠️⚠️ 这里必须 `min` 不能 `max` —— 这是 2026-10-10 实测踩到的:
    第一版写的 `max(inflight, key=启动时间)`,取到的是**最新开始**的那条,
    于是点「跳过」跳掉的是一个**刚刚开始、本来完全正常**的链接,
    而真正卡住的那条纹丝不动。
    更要命的是它**看起来是对的**:接口回 `ok:true`、日志里也真的写着
    「已跳过 xxx」、进度也确实往前走了 —— 只有把跳过的**是哪一条**打出来
    才看得出来跳错了。判据不能是"有没有跳过",得是"跳过的是不是那条"。
    """
    return min(inflight, key=lambda k: inflight[k])


def skip(jid):
    """跳过当前跑得最久的那一条。返回被跳过的条目键,没有在途条目时返回 None。"""
    j = _jobs.get(jid)
    if not j or not j["running"]:
        return None
    with _lk:
        d = _live.get(jid)
        if not d or not d["inflight"]:
            return None
        key = oldest(d["inflight"])
        d["skip"].add(key)
    j.setdefault("log", []).append(
        f"{time.strftime('%H:%M:%S')} ⏭ 已请求跳过第 {_shown(key)} 条")
    j["message"] = f"正在跳过第 {_shown(key)} 条…"
    return key


def _shown(key):
    """条目键是 0 开始的索引,给用户看要 +1。"""
    try:
        return int(key) + 1
    except Exception:
        return key


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
