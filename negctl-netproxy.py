# -*- coding: utf-8 -*-
"""负向对照:把修好的东西**逐个放回去**,确认 verify-netproxy.py 会红。

⚠️ 三条铁律(这个项目里都栽过):
  1. `restore` **只做复制**,备份只由收尾的 finally 删 —— 否则第一条对照之后
     每条 restore 都静默失败,源码留在改坏的状态,后面测什么都红。
  2. 收尾用逐字节比较确认还原,并 `grep` 查变异残留(留一个没还原的文件,
     后面每一轮验证都跑在改坏的代码上 —— 踩过)。
  3. **每个被改的文件都要先登记进 FILES 再写变异** —— 漏一个 = 那个文件的
     改动不会被还原,而脚本照样报"对照成立"(踩过两次)。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFIER = HERE / "verify-netproxy.py"
TMP = Path(tempfile.mkdtemp(prefix="negctl-netproxy-"))
BK = TMP / "backup"

# (名字, 相对文件, 原文锚点, 换成什么, 期望哪几条断言变红)
MUTATIONS = [
    (
        "空串当成「关掉」(这就是这一版真正的 bug)",
        "netproxy.py",
        '    if v:\n        if v.lower() in OFF:\n            return None                       # 用户明确要求「直连」',
        '    if v is not None:\n        if (not v) or v.lower() in OFF:\n            return None   # NEGCTL',
        ["空串 = **没设置**"],
    ),
    (
        "NO_PROXY 不管回环(界面会被代理吃掉)",
        "netproxy.py",
        '    os.environ["NO_PROXY"] = os.environ["no_proxy"] = NO_PROXY',
        '    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ""   # NEGCTL',
        ["NO_PROXY 必须覆盖回环"],
    ),
    (
        "Chrome 那条腿不给代理旗标(只修了 Python 一半)",
        "netproxy.py",
        "    p = detect()\n    if not p:\n        return None",
        "    return None   # NEGCTL\n    p = detect()\n    if not p:\n        return None",
        ["Chrome 的 --proxy-server"],
    ),
    (
        "capture.py 不再去问线路(真·只修了一半:Python 走了,浏览器没走)",
        "capture.py",
        "        _px = netproxy.chrome_flag()",
        "        _px = None   # NEGCTL",
        # ⚠️ 判据盯**调用点**,不盯那句 args.append("--proxy-server=…") ——
        #    我第一版就是盯错了:变异只把 chrome_flag() 换成 None,
        #    那行 append 原样还在 ⇒ 判据绿 ⇒ 报"没红",
        #    看起来像"断言在空跑",其实是**判据没对准被测的那一侧**。
        ["__CALL_SITE__"],
    ),
]

# 每个可能被改动的文件都要在这里(见铁律 3)
FILES = sorted({m[1] for m in MUTATIONS})
ORIG = {}


def run():
    p = subprocess.run([sys.executable, str(VERIFIER)], cwd=str(HERE),
                       capture_output=True, text=True, timeout=900,
                       env={**os.environ, "NEGCTL": "0"})
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def restore():
    for rel, blob in ORIG.items():
        (HERE / rel).write_bytes(blob)


def main():
    for rel in FILES:
        f = HERE / rel
        if not f.exists():
            print("缺文件:%s" % rel)
            return 2
        ORIG[rel] = f.read_bytes()
        dst = BK / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(ORIG[rel])
    print("备份 %s(%d 个文件)\n" % (BK, len(FILES)))

    ok, bad = [], []
    try:
        rc, out = run()
        print("基线:退出码 %s" % rc)
        if rc != 0:
            print(out[-1500:])
            print("⚠️ 基线就是红的 —— 先修基线,对照没有意义。")
            return 2
        print("  基线全绿 ✓\n")

        for name, rel, old, new, expect in MUTATIONS:
            print("─" * 66)
            print("对照:%s" % name)
            s = (HERE / rel).read_text(encoding="utf-8")
            n = s.count(old)
            if n != 1:
                print("  ✗ 锚点命中 %d 次(必须恰好 1 次)—— 这次对照无效" % n)
                bad.append(name + "(锚点失配)")
                continue
            (HERE / rel).write_text(s.replace(old, new), encoding="utf-8")
            try:
                rc, out = run()
                static_bad = None
                if expect == ["__CALL_SITE__"]:
                    # 静态对照:capture.py 必须**还在问** netproxy 要线路
                    src = (HERE / "capture.py").read_text(encoding="utf-8")
                    static_bad = ("netproxy.chrome_flag()" in src)
            finally:
                restore()
                if any((HERE / r).read_bytes() != b for r, b in ORIG.items()):
                    print("  ✗✗ 还原失败 —— 立刻中止")
                    return 2
            if expect == ["__CALL_SITE__"]:
                if static_bad is True:
                    print("  ✗ **没红** —— capture.py 还在问 netproxy 要线路")
                    bad.append(name + "(没红)")
                else:
                    print("  ✓ 红了(capture.py 不再问线路了)")
                    ok.append(name)
                continue
            if rc == 2:
                print("  ✗ 验证器自己坏了(退出码 2),这次对照无效")
                bad.append(name + "(验证器崩了)")
                continue
            if rc == 0:
                print("  ✗ **没红** —— 这条断言在空跑!")
                bad.append(name + "(没红)")
                continue
            missed = [e for e in expect if e not in out]
            if missed:
                print("  ✗ 红了,但不是期望的那几条;缺:%s" % missed)
                bad.append(name + "(红错了地方)")
                continue
            print("  ✓ 红了,而且正是这几条:%s" % expect)
            ok.append(name)
    finally:
        restore()
        same = all((HERE / r).read_bytes() == b for r, b in ORIG.items())
        residue = subprocess.run(
            ["grep", "-rl", "NEGCTL"] + [str(HERE / r) for r in FILES],
            capture_output=True, text=True).stdout.strip()
        print("\n还原:%s   变异残留:%s"
              % ("逐字节一致 ✓" if same else "✗✗ 不一致!", residue or "0 个文件"))
        if same and not residue:
            shutil.rmtree(TMP, ignore_errors=True)
        else:
            print("备份还在:%s" % BK)

    print("\n══ 负向对照:%d 成立 / %d 不成立 ══" % (len(ok), len(bad)))
    for b in bad:
        print("   ✗ " + b)
    if not bad:
        print("绿灯是有意义的。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
