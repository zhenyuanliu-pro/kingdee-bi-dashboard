"""
金蝶云星辰 OpenAPI 客户端（Python，服务端调用）

已实测验证（2026-09-30）：
- X-Api-Signature 算法与官方文档示例签名逐字一致
- 主动获取授权 → 换取 app-token → 业务接口，多账套连通

踩过的坑（代码里已处理）：
1. page_size > 100 会被静默截断为 100，但 total_page 仍按请求值计算 → 会丢数据且不报错。
   本客户端强制 page_size=100，并校验 rows 数 == count。
2. 销售出库单列表 headers.total_amount（价税合计汇总）返回空字符串，必须逐单累加 total_amount。
3. 应收账款明细表(ar_order_statement_report) 是按客户的流水账（余额为负号口径），
   不是逐单未收余额；账龄需用 FIFO 把收款冲销到出库单上自行计算。
4. 主动获取授权接口返回 code/msg（文档字段表写的是 errcode/description），两者都要兼容。
5. 业务接口从浏览器调用时 CORS 头重复会被拦截；服务端调用无此问题。

依赖：pip install requests
"""
import base64
import hashlib
import hmac
import json
import random
import time
import urllib.parse
from pathlib import Path

import requests

API = "https://api.kingdee.com"
PAGE_SIZE_MAX = 100


def _enc(s: str) -> str:
    # 与 JS encodeURIComponent 等价，编码字母大写
    return urllib.parse.quote(str(s), safe="-_.~")


def hmac_hex_b64(key: str, msg: str) -> str:
    """HMAC-SHA256 → 16进制字符串 → 对该字符串做 Base64（注意不是对原始字节）"""
    hex_str = hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return base64.b64encode(hex_str.encode()).decode()


class KingdeeClient:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    # ---------- 签名 ----------
    def _signed_headers(self, method: str, path: str, params: dict) -> dict:
        keys = sorted(params)
        qs2 = "&".join(f"{_enc(_enc(k))}={_enc(_enc(params[k]))}" for k in keys)
        ts = str(int(time.time() * 1000))
        nonce = str(random.randint(10**9, 10**10 - 1))
        raw = f"{method.upper()}\n{_enc(path)}\n{qs2}\nx-api-nonce:{nonce}\nx-api-timestamp:{ts}\n"
        return {
            "Content-Type": "application/json",
            "X-Api-ClientID": self.client_id,
            "X-Api-Auth-Version": "2.0",
            "X-Api-TimeStamp": ts,
            "X-Api-SignHeaders": "X-Api-TimeStamp,X-Api-Nonce",
            "X-Api-Nonce": nonce,
            "X-Api-Signature": hmac_hex_b64(self.client_secret, raw),
        }

    def _request(self, method, path, params=None, body=None, extra_headers=None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = self._signed_headers(method, path, params)
        headers.update(extra_headers or {})
        qs1 = "&".join(f"{_enc(k)}={_enc(params[k])}" for k in sorted(params))
        url = f"{API}{path}" + (f"?{qs1}" if qs1 else "")
        r = requests.request(method, url, headers=headers,
                             data=json.dumps(body) if body else None, timeout=30)
        return r.json()

    # ---------- 授权 ----------
    def pull_authorize(self, outer_instance_id: str) -> dict:
        r = self._request("POST", "/jdyconnector/app_management/push_app_authorize",
                          {"outerInstanceId": outer_instance_id})
        code = r.get("errcode", r.get("code"))
        if code not in (0, 200, "0"):
            raise RuntimeError(f"主动获取授权失败: {r}")
        data = r["data"]
        return data[0] if isinstance(data, list) else data

    def get_app_token(self, app_key: str, app_secret: str) -> str:
        r = self._request("GET", "/jdyconnector/app_management/kingdee_auth_token",
                          {"app_key": app_key, "app_signature": hmac_hex_b64(app_secret, app_key)})
        if r.get("errcode") != 0:
            raise RuntimeError(f"换取 app-token 失败: {r}")
        return r["data"]["app-token"]


class Account:
    """单个账套会话：负责 token 缓存（<24h）与业务接口调用"""
    TOKEN_TTL = 20 * 3600  # 提前刷新，官方有效期 24h；换 token 接口限 2 次/分钟

    def __init__(self, client: KingdeeClient, company: str, outer_instance_id: str):
        self.client, self.company, self.oid = client, company, outer_instance_id
        self.token, self.domain, self.token_at = None, None, 0
        self.auth = None

    def ensure_token(self, force=False):
        if force or not self.token or time.time() - self.token_at > self.TOKEN_TTL:
            self.auth = self.client.pull_authorize(self.oid)
            if int(self.auth.get("status", 0)) != 1:
                raise RuntimeError(f"{self.company} 授权已失效(status=0)")
            self.domain = self.auth["domain"]
            self.token = self.client.get_app_token(self.auth["appKey"], self.auth["appSecret"])
            self.token_at = time.time()

    def get(self, path, params=None):
        self.ensure_token()
        extra = {"app-token": self.token, "X-GW-Router-Addr": self.domain}
        r = self.client._request("GET", path, params, extra_headers=extra)
        if r.get("errcode") == 1030002006:  # 授权密钥失效 → 重新拉授权后重试一次
            self.ensure_token(force=True)
            extra = {"app-token": self.token, "X-GW-Router-Addr": self.domain}
            r = self.client._request("GET", path, params, extra_headers=extra)
        return r

    def get_all(self, path, params=None):
        """自动翻页；强制 page_size=100 并校验完整性"""
        rows, page = [], 1
        while True:
            r = self.get(path, {**(params or {}), "page": page, "page_size": PAGE_SIZE_MAX})
            if r.get("errcode") != 0:
                raise RuntimeError(f"{self.company} {path} 失败: {r}")
            d = r["data"]
            rows += d.get("rows") or []
            if page >= int(d.get("total_page") or 1):
                break
            page += 1
            time.sleep(0.15)  # 500 次/分钟/账套
        count = int(d.get("count") or len(rows))
        if count != len(rows):
            raise RuntimeError(f"{self.company} {path} 行数不完整: {len(rows)}/{count}")
        return rows


# ---------- 集团内部往来剔除 ----------
def load_config(path="config.json"):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def exclusion_reason(customer_name: str, group_name: str, cfg: dict):
    """返回剔除原因；None 表示正常客户。规则见 config.json -> exclusion_rules"""
    r = cfg["exclusion_rules"]
    name, group = (customer_name or "").strip(), (group_name or "").strip()
    if name in set(r["name_exact"]):
        return "集团内部公司"
    for kw in r["name_contains"]:
        if kw in name:
            return kw
    if group in set(r["customer_group"]):
        return f"客户分类:{group}"
    return None


if __name__ == "__main__":
    # 离线自检：用官方文档示例验证签名算法
    raw = ("GET\n%2Fjdyconnector%2Fapp_management%2Fkingdee_auth_token\n"
           "app_key=bVZgAZOv1&app_signature=MzZlYTk0ODk4MWZlNjdiODNmNWU4YzViNzYxNGM5MTFlOGJkN2NjMzk0MTJkZGNhZGM0NzZhN2YxZDJmOTlkZA%253D%253D\n"
           "x-api-nonce:4427456950\nx-api-timestamp:1670305063559\n")
    expect = "OTFiZTliNDFiMjNkYTI3YzVhNzg4MDI4ZGU3MWY1ZTA5ZTk1NjVlNGM1YTI1ZjIxY2Y5YTA3ZGY2OGI1MGQ1MQ=="
    assert hmac_hex_b64("f2adcfef73369bfc4e1384677d38a0ff", raw) == expect
    # 验证 path/参数编码与文档一致
    assert _enc("/jdyconnector/app_management/kingdee_auth_token") == "%2Fjdyconnector%2Fapp_management%2Fkingdee_auth_token"
    assert _enc(_enc("MzZ==")) == "MzZ%253D%253D"
    print("签名自检通过")
