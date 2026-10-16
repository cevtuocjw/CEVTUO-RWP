# -*- coding: utf-8 -*-
"""负向对照跑台:把 2026-10-16 那几处修好的东西**逐个放回去**,
确认 `verify-collection-create.py` 会**真的红**。

────────────────────────────────────────────────────────────────────────
为什么必须有这个文件
────────────────────────────────────────────────────────────────────────
「断言全绿」什么都不能说明 —— 除非你能说出**它怎么才会红**。
这个项目已经栽过三次:负向对照写成恒等操作、断言靠运气、探针把被裁掉的内容
算成内容。所以规矩是:**新增断言就要跑一次对照**,而且对照要打在被测的那一侧。

这个跑台测的就是"看得见"这件事本身:
    · 失败只在弹窗里说了 —— 拿掉 ⇒ 用户又看不见了
    · 渲染炸了会退回列表 —— 拿掉 ⇒ 又停在半死页面上
    · 漏出来的异常会自己喊 —— 拿掉 ⇒ 又变成静默

────────────────────────────────────────────────────────────────────────
三条自伤(都踩过),这里都堵上了
────────────────────────────────────────────────────────────────────────
1. **`restore` 只复制,绝不在里面删备份** —— 否则第一条对照之后每条 restore
   都静默失败,源码留在改坏的状态,后面测什么都是红的。
2. **备份只在收尾(`finally`)删**,并且收尾用 `cmp -s` **逐字节**确认还原成功。
3. **每处变异都断言锚点命中恰好 1 次** —— 锚点失配必须当场报错,
   而不是"少改了一处"然后看起来像"断言在空跑"。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "ui" / "index.html"
VERIFIER = HERE / "verify-collection-create.py"
BASE_URL = os.environ.get("RWP_BASE", "http://127.0.0.1:8611")

# (名字, 原文, 替换成什么, 期望哪几条断言变红)
MUTATIONS = [
    (
        "失败只在 toast 里说(回到修复前)",
        "      modalErr(msg);            // 钉在弹窗里\n",
        "      /* NEGCTL: 不钉在弹窗里了 */\n",
        ["失败的原因**留在屏幕上**"],
    ),
    (
        "setView 裸调渲染函数(回到无人处理的 rejection)",
        "  Promise.resolve().then(run).catch(err=>{",
        "  run(); ((err)=>{",
        ["退回了合集列表", "无人处理的 promise 拒绝"],
    ),
    (
        "去掉全局兜底(回到静默)",
        "window.addEventListener('unhandledrejection', e=>{",
        "false && window.addEventListener('unhandledrejection', e=>{",
        ["无人处理的 promise 拒绝被接住并提示了"],
    ),
]


def run_verifier():
    p = subprocess.run([sys.executable, str(VERIFIER)],
                       cwd=str(HERE), capture_output=True, text=True,
                       env={**os.environ, "RWP_BASE": BASE_URL}, timeout=900)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def apply(text, old, new):
    n = text.count(old)
    if n != 1:
        raise AssertionError("锚点命中 %d 次(必须恰好 1 次): %r" % (n, old[:60]))
    return text.replace(old, new)


def main():
    if not TARGET.exists():
        print("找不到 %s" % TARGET)
        return 2
    orig = TARGET.read_bytes()
    backup = Path(tempfile.mkdtemp(prefix="negctl-")) / "index.html"
    backup.write_bytes(orig)
    print("备份 %s(%d 字节)\n" % (backup, len(orig)))

    ok, bad = [], []
    try:
        # 先确认**没改**的时候是全绿的 —— 否则下面"红了"也可能只是环境坏了
        rc, out = run_verifier()
        print("基线:退出码 %s" % rc)
        if rc != 0:
            print(out[-2500:])
            print("\n⚠️ 基线就是红的 —— 先修基线,对照没有意义。")
            return 2
        ok.append("基线全绿")
        print("  基线全绿 ✓\n")

        for name, old, new, expect in MUTATIONS:
            print("─" * 68)
            print("对照:%s" % name)
            src = TARGET.read_text(encoding="utf-8")
            try:
                TARGET.write_text(apply(src, old, new), encoding="utf-8")
            except AssertionError as e:
                print("  ✗ 变异施加失败:%s" % e)
                bad.append("%s(锚点失配)" % name)
                continue
            try:
                rc, out = run_verifier()
            finally:
                shutil.copyfile(backup, TARGET)      # ⚠️ 只复制,不删备份
                if TARGET.read_bytes() != orig:
                    print("  ✗✗ 还原失败 —— 立刻中止")
                    return 2

            tail = out.strip().splitlines()[-1] if out.strip() else "(无输出)"
            if rc == 2:
                print("  ✗ 验证器自己坏了(退出码 2),这次对照无效")
                bad.append("%s(验证器崩了)" % name)
                continue
            if rc == 0:
                print("  ✗ **没红** —— 这条断言在空跑!  %s" % tail)
                bad.append("%s(没红)" % name)
                continue
            # 红了:还要确认**是期望的那几条**红的,不是别处崩了
            missed = [e for e in expect if e not in out]
            if missed:
                print("  ✗ 红了,但不是期望的那几条;缺:%s" % missed)
                print("     实际失败行:%s" % [
                    l.strip() for l in out.splitlines() if l.strip().startswith("✗")])
                bad.append("%s(红错了地方)" % name)
                continue
            print("  ✓ 红了,而且正是这几条:%s" % expect)
            ok.append(name)
    finally:
        # ⚠️ 收尾:还原 + **逐字节**确认 + 才删备份
        shutil.copyfile(backup, TARGET)
        same = TARGET.read_bytes() == orig
        print("\n还原:%s" % ("逐字节一致 ✓" if same else "✗✗ 不一致!"))
        if not same:
            print("备份还在:%s" % backup)
        else:
            shutil.rmtree(backup.parent, ignore_errors=True)

    print("\n══ 负向对照:%d 成立 / %d 不成立 ══" % (len(ok), len(bad)))
    for b in bad:
        print("   ✗ " + b)
    if not bad:
        print("绿灯是有意义的。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
