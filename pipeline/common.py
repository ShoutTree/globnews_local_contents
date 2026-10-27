#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""glob.news 流水线公共函数：配置加载、LLM 调用（多模型 fallback）、JSON 修复。"""

import json
import os
import re
import sys

PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PIPELINE_DIR, 'config.json')
DATA_DIR = os.path.join(PIPELINE_DIR, 'data')
ARTICLES_DIR = os.path.join(DATA_DIR, 'articles')


def load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_api_key(key_name, config=None):
    config = config or load_config()
    try:
        with open(config['keys_path'], 'r', encoding='utf-8') as f:
            keys = json.load(f)
        return keys.get(key_name, '')
    except Exception as e:
        print(f"Warning: Failed to load API keys: {e}", file=sys.stderr)
        return ''


def call_llm(prompt, system_msg, config=None, temperature=0.4, max_tokens=8000):
    """按 config.text_models 顺序尝试，返回 (content, model_name)。全部失败抛 RuntimeError。"""
    from openai import OpenAI
    config = config or load_config()

    last_error = None
    for m in config['text_models']:
        name = m['name']
        try:
            print(f"  [llm] trying {name} ...")
            api_key = load_api_key(m['api_key_name'], config)
            if not api_key:
                raise ValueError(f"no api key for '{m['api_key_name']}'")
            client = OpenAI(api_key=api_key, base_url=m['base_url'])
            kwargs = {
                'model': name,
                'messages': [
                    {'role': 'system', 'content': system_msg},
                    {'role': 'user', 'content': prompt},
                ],
                'temperature': temperature,
                'max_tokens': max_tokens,
            }
            if m.get('extra_body'):
                kwargs['extra_body'] = m['extra_body']
            resp = client.chat.completions.create(**kwargs)
            content = (resp.choices[0].message.content or '').strip()
            if content:
                print(f"  [llm] {name} ok ({len(content)} chars)")
                return content, name
            print(f"  [llm] {name} returned empty, trying next ...")
        except Exception as e:
            last_error = e
            print(f"  [llm] {name} failed: {e}")
    raise RuntimeError(f"All text models failed. Last error: {last_error}")


def extract_json(text):
    """从 LLM 回复中提取 JSON（数组或对象），带一轮自动修复。

    依据首个非空白字符选择最外层括号模式，贪婪匹配到末尾同类括号。
    """
    stripped = text.lstrip()
    pattern = r'\[[\s\S]*\]' if stripped.startswith('[') else r'\{[\s\S]*\}'
    m = re.search(pattern, text)
    if not m:
        return None
    raw = m.group()
    for candidate in (raw, _fix_json_string(raw)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _fix_json_string(s):
    fixed = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
    fixed = re.sub(r',\s*}', '}', fixed)
    fixed = re.sub(r',\s*]', ']', fixed)
    return fixed


def title_to_slug(title):
    slug = title.lower().strip()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s]+', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    if len(slug) > 80:
        slug = '-'.join(slug.split('-')[:10])
    return slug
