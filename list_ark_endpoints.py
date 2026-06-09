#!/usr/bin/env python3
"""List your ARK inference endpoints (ep-...) — including third-party models
like MiniMax that you access through a created endpoint.

This uses Volcano Engine's management OpenAPI (ListEndpoints), which is signed
with your Volcano account AccessKey/SecretKey — NOT the ARK_API_KEY (that key
only works for inference). Add these to .env:

    VOLC_ACCESSKEY=AKLT...
    VOLC_SECRETKEY=...

Get them at console.volcengine.com -> 访问控制/IAM -> API access keys.

Usage:  ./.venv/bin/python list_ark_endpoints.py
"""
import json
import os
import sys

from chatbot import load_dotenv

from volcengine.base.Service import Service
from volcengine.ServiceInfo import ServiceInfo
from volcengine.ApiInfo import ApiInfo
from volcengine.Credentials import Credentials

HOST = "open.volcengineapi.com"
REGION = "cn-beijing"
SERVICE = "ark"
VERSION = "2024-01-01"


def ark_service(ak: str, sk: str) -> Service:
    info = ServiceInfo(
        HOST,
        {"Accept": "application/json"},
        Credentials(ak, sk, SERVICE, REGION),
        10, 10, "https",
    )
    apis = {
        "ListEndpoints": ApiInfo(
            "POST", "/", {"Action": "ListEndpoints", "Version": VERSION}, {}, {},
        ),
    }
    return Service(info, apis)


def main() -> None:
    load_dotenv()
    ak = os.environ.get("VOLC_ACCESSKEY", "").strip()
    sk = os.environ.get("VOLC_SECRETKEY", "").strip()
    if not ak or not sk:
        sys.exit("Set VOLC_ACCESSKEY and VOLC_SECRETKEY in .env "
                 "(console.volcengine.com -> IAM -> API access keys).")

    svc = ark_service(ak, sk)
    items, page = [], 1
    while True:
        raw = svc.json("ListEndpoints", {}, json.dumps({"PageNumber": page, "PageSize": 100}))
        data = json.loads(raw)
        if "Result" not in data:
            sys.exit(f"Unexpected response: {raw[:300]}")
        result = data["Result"]
        items.extend(result.get("Items", []))
        if len(items) >= result.get("Total", len(items)) or not result.get("Items"):
            break
        page += 1

    if not items:
        print("No inference endpoints found. Create one at console.volcengine.com/ark "
              "(在线推理 / Online Inference) for the model you want, e.g. MiniMax-M2.")
        return

    print(f"{len(items)} inference endpoint(s):\n")
    print(f"{'endpoint id':28} {'status':10} {'foundation model':32} name")
    print("-" * 90)
    for ep in items:
        fm = ep.get("FoundationModel", {}) or {}
        model = fm.get("Name", "") + (f":{fm['ModelVersion']}" if fm.get("ModelVersion") else "")
        print(f"{ep.get('Id',''):28} {ep.get('Status',''):10} {model:32} {ep.get('Name','')}")


if __name__ == "__main__":
    main()
