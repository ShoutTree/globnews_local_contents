#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片生成：后端由 config.json 的 image.backend 决定，换模型只改那一处。

支持的 backend 类型：
- dashscope_multimodal : qwen-image 系列（同步长连接，写实摄影风格首选）
- dashscope_wanx       : 通义万相 wanx t2i（异步任务轮询）

用法（测试）：
    python imagegen.py --prompt "a lighthouse at dawn" --out /tmp/test.jpg
"""

import argparse
import base64
import json
import os
import time
import urllib.request

from common import load_api_key, load_config

DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/api/v1"


def generate(prompt, out_path, config=None, size=None):
    """主入口：按配置的后端生成图片并写入 out_path（jpg）。"""
    config = config or load_config()
    img_cfg = config['image']
    backend = img_cfg['backends'][img_cfg['backend']]
    full_prompt = f"{prompt}. {img_cfg['style_suffix']}"

    t0 = time.time()
    if backend['type'] == 'dashscope_multimodal':
        _gen_multimodal(full_prompt, backend, out_path, config, size)
    elif backend['type'] == 'dashscope_wanx':
        _gen_wanx(full_prompt, backend, out_path, config, size)
    else:
        raise ValueError(f"Unknown image backend type: {backend['type']}")
    print(f"  [imagegen] {img_cfg['backend']}({backend['model']}) -> {out_path} ({time.time()-t0:.1f}s)")


def _headers(config):
    key = load_api_key('qwen', config)
    if not key:
        raise RuntimeError("no qwen/dashscope api key")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _http_json(url, body, headers, timeout=300):
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _gen_multimodal(prompt, backend, out_path, config, size):
    """qwen-image 系列：/services/aigc/multimodal-generation/generation（同步）"""
    url = DASHSCOPE_BASE + "/services/aigc/multimodal-generation/generation"
    body = {
        "model": backend['model'],
        "input": {"messages": [{
            "role": "user",
            "content": [{"text": prompt}],
        }]},
        "parameters": {
            "n": 1,
            "size": size or backend['size'],
            "watermark": False,
            "prompt_extend": False,
        },
    }
    result = _http_json(url, body, _headers(config))
    output = result.get('output', {})
    if output.get('code'):
        raise RuntimeError(f"dashscope error {output.get('code')}: {output.get('message')}")
    for choice in output.get('choices') or []:
        for c in (choice.get('message', {}).get('content') or []):
            if 'image' in c:
                _save_b64_or_url(c['image'], out_path)
                return
    raise RuntimeError(f"no image in response: {json.dumps(output)[:300]}")


def _gen_wanx(prompt, backend, out_path, config, size):
    """通义万相 t2i：异步提交 + 轮询。size 需映射到预设。"""
    w, h = size if isinstance(size, (list, tuple)) else _map_size(backend['size'])
    submit_url = DASHSCOPE_BASE + "/services/aigc/text2image/imagegeneration"
    body = {
        "model": backend['model'],
        "input": {"prompt": prompt[:780]},
        "parameters": {"n": 1, "size": f"{w}*{h}", "watermark": False, "prompt_extend": False},
    }
    headers = _headers(config)
    headers["X-DashScope-Async"] = "enable"
    task = _http_json(submit_url, body, headers)
    task_id = task.get('output', {}).get('task_id')
    if not task_id:
        raise RuntimeError(f"no task_id: {json.dumps(task)[:300]}")

    poll_url = f"{DASHSCOPE_BASE}/tasks/{task_id}"
    for _ in range(60):
        time.sleep(3)
        req = urllib.request.Request(poll_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read().decode('utf-8'))
        status = r.get('output', {}).get('task_status')
        if status == 'SUCCEEDED':
            for res in r.get('output', {}).get('results', []):
                if res.get('url'):
                    _save_b64_or_url(res['url'], out_path)
                    return
            raise RuntimeError("succeeded but no image url")
        if status in ('FAILED', 'CANCELED'):
            raise RuntimeError(f"wanx task {status}: {json.dumps(r)[:300]}")
    raise TimeoutError("wanx task polling timeout (180s)")


def _map_size(size_pair):
    """wanx 只接受预设尺寸，把请求尺寸映射到最接近的预设（16:9 -> 1280*720）。"""
    allowed = [(768, 768), (576, 1024), (1024, 576), (1024, 1024),
               (720, 1280), (1280, 720), (864, 1152), (1152, 864)]
    w, h = size_pair
    return min(allowed, key=lambda s: abs(s[0] / s[1] - w / h))


def _save_b64_or_url(image, out_path):
    """qwen-image 返回 b64（含 data: 前缀），wanx 返回 URL。"""
    if image.startswith('http'):
        with urllib.request.urlopen(urllib.request.Request(image, headers={"User-Agent": "Mozilla/5.0"}), timeout=120) as resp:
            data = resp.read()
    else:
        b64 = image.split(',', 1)[-1]
        data = base64.b64decode(b64)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'wb') as f:
        f.write(data)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--prompt', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    generate(args.prompt, args.out)
