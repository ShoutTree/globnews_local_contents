#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
步骤 1：选题。

RSS 抓取真实新闻源 → 关键词粗滤 → LLM 聚类/挑选/改写（附真实 reference link）
→ 链接可达性校验 → 与线上已发布文章去重 → data/candidates_*.json（每条 approved:false）

之后运行 approve.py 人工筛选要生成的文章。

用法：
    python select_topics.py                # 默认抓取
    python select_topics.py --count 10     # 候选数量
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from common import (DATA_DIR, call_llm, extract_json, load_config,
                    title_to_slug)

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}


def fetch_feeds(config):
    """抓全部 RSS 源，返回去重条目列表。"""
    window = timedelta(hours=config['hours_window'])
    now = datetime.now(timezone.utc)
    items, seen_links = [], set()

    for src in config['rss_sources']:
        try:
            d = feedparser.parse(src['url'], request_headers=UA)
            if d.bozo and not d.entries:
                print(f"  [rss] {src['name']}: parse failed ({d.bozo_exception})")
                continue
            taken = 0
            for e in d.entries:
                if taken >= config['max_items_per_source']:
                    break
                link = (e.get('link') or '').strip()
                title = (e.get('title') or '').strip()
                if not link or link in seen_links:
                    continue
                # 时间过滤（解析失败则保留，交 LLM/人工判断）
                published = None
                try:
                    pt = e.get('published_parsed') or e.get('updated_parsed')
                    if pt:
                        published = datetime(*pt[:6], tzinfo=timezone.utc)
                except Exception:
                    published = None
                if published and (now - published) > window:
                    continue
                summary = re.sub(r'<[^>]+>', ' ', e.get('summary') or '')
                summary = re.sub(r'\s+', ' ', summary).strip()[:400]
                seen_links.add(link)
                items.append({
                    'title': title,
                    'link': link,
                    'summary': summary,
                    'source': src['name'],
                    'category_hint': src['category_hint'],
                    'published': published.isoformat() if published else '',
                })
                taken += 1
            print(f"  [rss] {src['name']}: {taken} items")
        except Exception as e:
            print(f"  [rss] {src['name']}: {e}")

    return items


def coarse_filter(items, config):
    """标题/摘要关键词粗滤：war/politics/disaster 等。"""
    kws = [k.lower() for k in config['exclude_keywords']]
    kept, dropped = [], 0
    for it in items:
        text = (it['title'] + ' ' + it['summary']).lower()
        if any(k in text for k in kws):
            dropped += 1
            continue
        kept.append(it)
    print(f"  [filter] coarse filter: kept {len(kept)}, dropped {dropped}")
    return kept


def build_prompt(items, config):
    today = datetime.now().strftime('%Y-%m-%d')
    count = config['topic_count']
    cats = ', '.join(config['categories'])
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(
            f"{i}. [{it['source']} | {it['category_hint']}] {it['title']}\n"
            f"   Link: {it['link']}\n"
            f"   Summary: {it['summary']}")
    catalog = '\n'.join(lines)

    return f"""Today is {today}. You are the story editor of glob.news, a general-audience news site for Western readers.

Below is a catalog of REAL news items fetched from established outlets via RSS. Select and shape the {count} best story candidates for our audience.

SELECTION CRITERIA (in priority order):
1. Stories Western general audiences love to read and SHARE: animals & nature wins, surprising science/space/archaeology discoveries, feel-good human achievements, consumer tech, travel wonders, sports feel-good moments, fun oddities, culture. Genuinely strange-but-true stories are especially welcome — assign them category "weird" (Weird & Wonderful).
2. STRICTLY EXCLUDE: war, military conflict, politics, elections, disasters with casualties, crime, divisive social controversy, celebrity gossip scandals.
3. Merge duplicate coverage of the same event across sources (keep the best link).
4. Prefer positive/curious/wholesome angles; skip anything grim.

For each selected story output:
- "title": a fresh, inviting English headline (under 90 chars). Do NOT copy the source headline verbatim — write your own, accurate, no clickbait.
- "description": 1-2 sentences explaining what happened and why it's delightful/interesting.
- "category": exactly one of: {cats}
- "source_url": the EXACT Link from the catalog item this story is based on (copy it character-for-character)
- "source_name": the outlet name from the catalog (e.g. "BBC Science")
- "score": 1-10 share-worthiness

CATALOG:
{catalog}

Output ONLY a valid JSON array of {count} objects. No other text."""


def verify_links(candidates):
    """校验 source_url 可达（HTTP <400）。不可达的剔除。"""
    ok = []
    for c in candidates:
        url = c.get('source_url', '')
        try:
            r = requests.head(url, headers=UA, timeout=15, allow_redirects=True)
            if r.status_code >= 400:
                r = requests.get(url, headers=UA, timeout=20, allow_redirects=True, stream=True)
                r.close()
            good = r.status_code < 400
        except Exception:
            good = False
        if good:
            ok.append(c)
        else:
            print(f"  [verify] DEAD LINK dropped: {url[:90]}")
    return ok


def dedupe_against_site(candidates, config):
    """与线上已发布文章按 slug/标题去重。"""
    try:
        r = requests.get(config['site']['site_api'] + '/news/posts/',
                         params={'page_size': 500}, headers=UA, timeout=30)
        r.raise_for_status()
        existing = r.json().get('results', [])
    except Exception as e:
        print(f"  [dedupe] cannot fetch site posts ({e}); skip dedupe")
        return candidates
    existing_slugs = {p.get('slug') for p in existing}
    existing_titles = {re.sub(r'\W+', '', (p.get('title') or '').lower()) for p in existing}
    kept = []
    for c in candidates:
        slug = title_to_slug(c['title'])
        tnorm = re.sub(r'\W+', '', c['title'].lower())
        if slug in existing_slugs or tnorm in existing_titles:
            print(f"  [dedupe] duplicate of published article: {c['title'][:70]}")
            continue
        kept.append(c)
    return kept


def main():
    parser = argparse.ArgumentParser(description='Select story candidates from real RSS sources')
    parser.add_argument('--count', type=int, default=None, help='Number of candidates (default from config)')
    args = parser.parse_args()

    config = load_config()
    if args.count:
        config['topic_count'] = args.count

    print(f"=== glob.news topic selection — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    print("[1/4] fetching RSS feeds ...")
    items = fetch_feeds(config)
    if not items:
        print("No RSS items fetched. Check network / sources.")
        return
    items = coarse_filter(items, config)

    print("[2/4] asking LLM to pick & shape candidates ...")
    response, model = call_llm(build_prompt(items, config),
                               "You are a senior story editor for a Western general-audience news site. Output only valid JSON.")
    candidates = extract_json(response)
    if not candidates:
        print("Failed to parse LLM output. Raw first 500 chars:")
        print(response[:500])
        return

    # 防幻觉：source_url 必须来自输入目录
    known_links = {it['link'] for it in items}
    candidates = [c for c in candidates if c.get('source_url') in known_links]
    for c in candidates:
        c['category'] = c.get('category') if c.get('category') in config['categories'] else 'world'
        c['approved'] = False
    print(f"  [llm] {len(candidates)} candidates after hallucination filter")

    print("[3/4] verifying links ...")
    candidates = verify_links(candidates)

    print("[4/4] deduping against published articles ...")
    candidates = dedupe_against_site(candidates, config)

    candidates_dir = os.path.join(DATA_DIR, 'candidates')
    os.makedirs(candidates_dir, exist_ok=True)
    out = os.path.join(candidates_dir, f"candidates_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'source_model': model,
            'total': len(candidates),
            'candidates': candidates,
        }, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(candidates)} candidates -> {out}")
    print("\nCandidates:")
    for i, c in enumerate(candidates, 1):
        print(f"  {i:2d}. [{c['category']:10s}] (score {c.get('score','?')}) {c['title'][:80]}")
        print(f"      source: {c.get('source_name')} -> {c.get('source_url','')[:80]}")
    print(f"\nNext: python build_articles.py {out}")


if __name__ == '__main__':
    main()
