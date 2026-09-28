#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""往阿里云云解析(DNS)加一条记录 —— 只用标准库,不需要装 aliyun CLI。

用的是阿里云 RPC 风格的 API 签名(HMAC-SHA1),版本 2015-01-09。

用法:
    export ALIYUN_AK_ID=...
    export ALIYUN_AK_SECRET=...
    python3 aliyun_dns.py list                       # 先看现有记录,避免冲突
    python3 aliyun_dns.py add apps CNAME cevtuocjw.github.io
    python3 aliyun_dns.py check apps                 # 确认加上了
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

ENDPOINT = "https://alidns.aliyuncs.com/"

def _ssl_context():
    """python.org 装的 Python 不自带 CA,优先用 certifi 的证书链。"""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()
DOMAIN = "cevtuogrnd.com"
VERSION = "2015-01-09"


def _pe(s):
    """阿里云要求的 RFC3986 百分号编码:空格→%20,~ 不转义。"""
    return urllib.parse.quote(str(s), safe="~")


def _sign(params, secret):
    items = sorted(params.items())
    canon = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in items)
    string_to_sign = "GET&%2F&" + _pe(canon)
    digest = hmac.new((secret + "&").encode("utf-8"),
                      string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def call(action, ak_id, ak_secret, **extra):
    params = {
        "Format": "JSON",
        "Version": VERSION,
        "AccessKeyId": ak_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Action": action,
    }
    params.update({k: v for k, v in extra.items() if v is not None})
    params["Signature"] = _sign(params, ak_secret)
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "cevtuo-dns/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30,
                                      context=_ssl_context()) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(body)
        except Exception:
            return {"Code": f"HTTP{e.code}", "Message": body[:300]}


def creds():
    ak = os.environ.get("ALIYUN_AK_ID") or os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
    sk = os.environ.get("ALIYUN_AK_SECRET") or os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    if not ak or not sk:
        print("缺凭据:请设置 ALIYUN_AK_ID 和 ALIYUN_AK_SECRET", file=sys.stderr)
        sys.exit(2)
    return ak, sk


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    ak, sk = creds()

    if cmd == "list":
        r = call("DescribeDomainRecords", ak, sk, DomainName=DOMAIN,
                 PageSize="100")
        recs = r.get("DomainRecords", {}).get("Record", [])
        if not recs:
            print("拿不到记录列表:", json.dumps(r, ensure_ascii=False)[:300])
            return 1
        print(f"{DOMAIN} 现有 {len(recs)} 条记录:")
        for x in recs:
            print("  %-8s %-14s -> %-40s TTL=%s" % (
                x.get("RR"), x.get("Type"), x.get("Value"), x.get("TTL")))
        return 0

    if cmd == "add":
        rr, rtype, value = sys.argv[2], sys.argv[3], sys.argv[4]
        ttl = sys.argv[5] if len(sys.argv) > 5 else "600"
        r = call("AddDomainRecord", ak, sk, DomainName=DOMAIN, RR=rr,
                 Type=rtype, Value=value, TTL=ttl)
        if r.get("RecordId"):
            print(f"✓ 已添加: {rr}.{DOMAIN}  {rtype}  ->  {value}  (TTL {ttl})")
            print("  RecordId:", r["RecordId"])
            return 0
        print("✗ 失败:", json.dumps(r, ensure_ascii=False))
        return 1

    if cmd == "check":
        rr = sys.argv[2]
        r = call("DescribeDomainRecords", ak, sk, DomainName=DOMAIN,
                 RRKeyWord=rr, PageSize="20")
        recs = r.get("DomainRecords", {}).get("Record", [])
        hit = [x for x in recs if x.get("RR") == rr]
        if hit:
            for x in hit:
                print(f"✓ 存在: {x['RR']}.{DOMAIN}  {x['Type']}  ->  {x['Value']}")
            return 0
        print(f"✗ 没找到 {rr} 的记录")
        return 1

    print("未知命令:", cmd)
    return 1


if __name__ == "__main__":
    sys.exit(main())
