#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
步骤 3：成文 + 配图。

输入：approve.py 批准过的 candidates 文件
每条流程：抓参考报道正文 → LLM 撰写文章（多源交叉核实）→ 生成写实封面图
         → data/articles/<slug>/{article.json, cover.jpg, preview.html}

article.json 字段与后端 import_articles 命令对齐（camelCase）。

用法：
    python build_articles.py data/candidates_20261023_1200.json
    python build_articles.py <file> --only 1,3       # 只生成第 1、3 条已批准的
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime

import requests
from bs4 import BeautifulSoup

import imagegen
from common import (ARTICLES_DIR, call_llm, extract_json, load_config,
                    title_to_slug)

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}


def fetch_reference(url):
    """抓参考报道正文（前 60 个 <p>），供 LLM 作为事实依据。"""
    try:
        r = requests.get(url, headers=UA, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'aside', 'figure']):
            tag.decompose()
        paragraphs = []
        for p in soup.find_all('p'):
            t = re.sub(r'\s+', ' ', p.get_text()).strip()
            if len(t) > 40:
                paragraphs.append(t)
        return '\n\n'.join(paragraphs[:60])[:6000]
    except Exception as e:
        print(f"  [ref] fetch failed ({e}); relying on model's web search")
        return ''


def build_prompt(cand, reference_text, config, gallery=0):
    cats = ', '.join(config['categories'])
    gallery_field = GALLERY_FIELD.format(n=gallery) if gallery > 0 else ''
    gallery_instruction = GALLERY_INSTRUCTION.format(n=gallery) if gallery > 0 else ''
    return f"""You are a staff writer for glob.news, a general-audience news site for Western readers.

Write a complete, publishable English news story based on this REAL event.

REFERENCE REPORTING (from {cand['source_name']}):
Title: {cand['title']}
Description: {cand.get('description','')}
Link: {cand['source_url']}
--- Begin reference article text ---
{reference_text or '(reference text unavailable — use your web search to verify facts, and stay strictly faithful to the reference title/description above)'}
--- End reference article text ---

WRITING REQUIREMENTS:
1. 500-700 words total, 7-9 paragraphs, in the "body" array (plain text strings, no markdown, no subheadings).
2. First paragraph is a strong news lede (who/what/when/where/why).
3. Warm, curious, precise tone of a quality general-news outlet. Accessible language, no jargon without brief explanation.
4. STRICT facts discipline: only state facts present in the reference or well-established context. NO invented quotes, NO invented numbers, NO speculation presented as fact. If unsure of a detail, omit it.
5. Do not mention "according to reports" repeatedly; write as original coverage of the event.
6. "excerpt": a 1-sentence standfirst (max 180 chars).
7. "keyPoints": 3-5 short factual takeaways (each max 120 chars).
8. "tags": 3-6 lowercase topic tags.
9. "title": final headline, under 90 chars, inviting but accurate, not clickbait.
10. "category": exactly one of: {cats} (the reference suggestion was "{cand['category']}").
11. "cover_prompt": ONE English scene description (max 60 words) for a photorealistic news photograph of the central subject of THIS story (the actual place/object/animal/scene). Describe a plausible generic scene; do NOT name real people; do NOT request faces of identifiable individuals; no text/logos in the image.{gallery_instruction}

Output ONLY valid JSON:
{{
  "title": "...",
  "excerpt": "...",
  "category": "...",
  "body": ["...", "...", "..."],
  "keyPoints": ["...", "..."],
  "tags": ["...", "..."],
  "cover_prompt": "..."{gallery_field}
}}"""

GALLERY_FIELD = ''',
  "gallery_prompts": ["a second photorealistic scene description, different angle or subject aspect", "..."]'''

GALLERY_INSTRUCTION = """
12. "gallery_prompts": also provide {n} ADDITIONAL distinct photorealistic scene descriptions (each max 60 words, same rules as cover_prompt) showing DIFFERENT angles/aspects/subjects of the story (e.g. the wider scene, a key object close-up, the environment)."""


REFERENCE_URL = None  # set per-candidate in build_one


def fact_check_prompt(article_text, reference_text, cand):
    return f"""You are a meticulous fact-checker for glob.news. Below are (A) a draft news article and (B) the original reference reporting it is based on.

Cross-check EVERY factual claim in the draft article against (B) and, when needed, your own web search (search for the study/paper/event to confirm names, numbers, dates, institutions).

For each significant factual claim in the draft (ignore pure interpretation/common knowledge), output one entry:
- "claim": the specific claim (short)
- "status": "supported" (reference or reliable search confirms it) / "unsupported" (cannot be verified anywhere) / "contradicted" (reference or search shows it is wrong)
- "evidence": one sentence citing where it is confirmed or what conflicts

Also output:
- "verdict": "pass" if no contradicted and at most 1 unsupported; "warn" if 2-3 unsupported, 0 contradicted; "fail" otherwise
- "notes": brief overall note

--- (A) DRAFT ARTICLE ---
{article_text}
--- END DRAFT ---

--- (B) REFERENCE REPORTING ({cand['source_name']}: {cand['source_url']}) ---
{reference_text or '(reference text unavailable — rely on web search of the event)'}
--- END REFERENCE ---

Output ONLY valid JSON:
{{
  "verdict": "pass|warn|fail",
  "claims": [{{"claim": "...", "status": "supported|unsupported|contradicted", "evidence": "..."}}],
  "notes": "..."
}}"""


def revise_prompt(article_json_text, factcheck_json_text, reference_text, cand):
    return f"""You are the editor of glob.news. The draft article below failed fact-checking. Rewrite the article JSON to fix the problems.

FACT-CHECK RESULTS (fix every "contradicted" and "unsupported" claim — correct it, or delete that claim entirely):
{factcheck_json_text}

RULES: keep the same story and structure; never introduce new unverified facts; prefer removing a claim over guessing; keep 500-700 words unless trimming false claims requires less. Output the SAME JSON schema as before.

--- DRAFT ARTICLE ---
{article_json_text}
--- END ---

--- REFERENCE REPORTING ({cand['source_name']}) ---
{reference_text[:4000]}
--- END ---

Output ONLY valid JSON (same schema as the draft)."""


def run_fact_check(art, reference, cand, config):
    """核查文章事实。返回 factcheck dict。fail 时自动修订一轮并复查。"""
    article_text = art['title'] + '\n\n' + art['excerpt'] + '\n\n' + '\n\n'.join(art['body'])

    print("      fact-check pass 1 ...")
    response, _ = call_llm(
        fact_check_prompt(article_text, reference, cand),
        "You are a rigorous, skeptical fact-checker. Output only valid JSON.",
        config, temperature=0.2, max_tokens=4000)
    fc = extract_json(response)
    if not fc or 'claims' not in fc:
        print("      WARNING: fact-check output unparseable; treating as warn")
        return {'verdict': 'warn', 'claims': [], 'notes': 'fact-check output unparseable — MANUAL REVIEW REQUIRED'}

    if fc.get('verdict') == 'fail':
        print(f"      verdict=FAIL ({sum(1 for c in fc['claims'] if c['status']=='contradicted')} contradicted, "
              f"{sum(1 for c in fc['claims'] if c['status']=='unsupported')} unsupported) -> revising article ...")
        art_text = json.dumps(art, ensure_ascii=False)
        rev_response, _ = call_llm(
            revise_prompt(art_text, json.dumps(fc, ensure_ascii=False), reference, cand),
            "You are a careful news editor. Fix only factual problems. Output only valid JSON.",
            config, temperature=0.3)
        revised = extract_json(rev_response)
        if revised and isinstance(revised.get('body'), list) and len(revised['body']) >= 4:
            # 保留元字段，只更新内容字段
            for k in ('title', 'excerpt', 'body', 'keyPoints', 'tags', 'cover_prompt', 'category'):
                if k in revised:
                    art[k] = revised[k]
            print("      fact-check pass 2 after revision ...")
            article_text2 = revised['title'] + '\n\n' + revised['excerpt'] + '\n\n' + '\n\n'.join(revised['body'])
            response2, _ = call_llm(
                fact_check_prompt(article_text2, reference, cand),
                "You are a rigorous, skeptical fact-checker. Output only valid JSON.",
                config, temperature=0.2, max_tokens=4000)
            fc2 = extract_json(response2)
            if fc2 and 'claims' in fc2:
                fc2['revised'] = True
                fc = fc2

    counts = {s: sum(1 for c in fc['claims'] if c.get('status') == s)
              for s in ('supported', 'unsupported', 'contradicted')}
    print(f"      fact-check verdict: {fc.get('verdict')}  "
          f"(supported {counts['supported']}, unsupported {counts['unsupported']}, contradicted {counts['contradicted']})")
    return fc


PREVIEW_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title} — preview</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; max-width: 720px; margin: 40px auto; padding: 0 20px; color: #1a1a2e; line-height: 1.75; background: #fafafa; }}
  .badge {{ display:inline-block; background:#0b57d0; color:#fff; border-radius:999px; padding:4px 14px; font-size:12px; font-family: system-ui; font-weight:700; text-transform:uppercase; letter-spacing:.08em; }}
  h1 {{ font-size: 2.1rem; line-height: 1.2; margin: 18px 0 10px; }}
  .meta {{ color:#666; font-family: system-ui; font-size: 14px; margin-bottom: 24px; }}
  figure {{ margin: 24px 0; }} figure img {{ width: 100%; border-radius: 14px; }}
  figcaption {{ text-align:center; color:#999; font-size:12px; font-family:system-ui; margin-top:6px; }}
  .excerpt {{ font-size: 1.15rem; color:#444; font-style: italic; }}
  .box {{ background:#eef4ff; border-left:4px solid #0b57d0; border-radius:10px; padding:16px 20px; font-family: system-ui; font-size:14px; margin: 28px 0; }}
  .box h2 {{ margin:0 0 10px; font-size:15px; letter-spacing:.05em; text-transform:uppercase; }}
  .src {{ background:#f0f0f0; border-radius:10px; padding:14px 18px; font-family: system-ui; font-size:13px; color:#444; margin: 28px 0; }}
  .fc {{ border:1px solid #ddd; border-radius:10px; padding:16px 20px; font-family: system-ui; font-size:13px; margin: 28px 0; }}
  .fc h2 {{ margin:0 0 4px; font-size:15px; letter-spacing:.05em; text-transform:uppercase; }}
  .fc .verdict {{ font-weight:700; }}
  .fc .notes {{ color:#666; margin: 4px 0 10px; }}
  .fc ul {{ list-style:none; padding:0; margin:0; }}
  .fc li {{ padding:6px 0; border-top:1px solid #eee; }}
  .fc .st {{ display:inline-block; width:96px; font-weight:700; }}
  .st-supported {{ color:#16a34a; }} .st-unsupported {{ color:#d97706; }} .st-contradicted {{ color:#dc2626; }}
  .v-pass {{ color:#16a34a; }} .v-warn {{ color:#d97706; }} .v-fail {{ color:#dc2626; }}
  .footer-note {{ color:#999; font-size:12px; font-family:system-ui; text-align:center; margin-top:40px; }}
</style>
</head>
<body>
<span class="badge">{category}</span>
<h1>{title}</h1>
<p class="meta">Glob News Desk · AI-assisted, human-reviewed · {published} · {read_minutes} min read</p>
{factcheck_banner}
<figure><img src="cover.jpg" alt="cover"><figcaption>AI-generated illustration · Glob News</figcaption></figure>
<p class="excerpt">{excerpt}</p>
{body_html}
<div class="box"><h2>Key takeaways</h2><ol>{keypoints_html}</ol></div>
{factcheck_panel}
<div class="src"><strong>Sources:</strong> Reporting informed by <a href="{source_url}">{source_name}</a>.</div>
<p class="footer-note">Local preview — not yet published. Review body, image &amp; fact-check, then: python publish.py --article {slug} --target staging</p>
</body>
</html>"""

FACTCHECK_PANEL = """<div class="fc">
<h2>Fact-check report <span class="verdict v-{v}">[{v}{revised}]</span></h2>
<p class="notes">{notes}</p>
<ul>{claims}</ul>
</div>"""

FACTCHECK_BANNER = """<p class="meta" style="color:#dc2626">⚠ Fact-check verdict: <strong>{v}</strong> — review the report at the bottom before publishing.</p>"""


def write_preview(dir_path, art):
    def block_html(b):
        if isinstance(b, str):
            return f'<p>{b}</p>'
        if isinstance(b, dict) and b.get('type') == 'image':
            caption = b.get('caption', '')
            return (f'<figure><img src="{b.get("src", "")}" alt="">'
                    f'<figcaption>{caption} · AI-generated illustration</figcaption></figure>')
        return ''
    body_html = '\n'.join(block_html(b) for b in art['body'])
    keypoints_html = ''.join(f'<li>{k}</li>' for k in art['keyPoints'])
    fc = art.get('factCheck') or {}
    verdict = fc.get('verdict', 'warn')
    claims_html = ''.join(
        f'<li><span class="st st-{c.get("status","unsupported")}">[{c.get("status","?")}]</span> '
        f'{c.get("claim","")} <span style="color:#888">— {c.get("evidence","")}</span></li>'
        for c in fc.get('claims', []))
    fc_panel = (FACTCHECK_PANEL.format(
        v=verdict, revised=' · revised' if fc.get('revised') else '',
        notes=fc.get('notes', ''), claims=claims_html or '<li>no claims recorded</li>')
        if fc else '')
    fc_banner = FACTCHECK_BANNER.format(v=verdict) if verdict != 'pass' else ''
    html = PREVIEW_TEMPLATE.format(
        category=art['category'].upper(),
        title=art['title'],
        published=art.get('publishedAt', '')[:16].replace('T', ' '),
        read_minutes=art['readMinutes'],
        factcheck_banner=fc_banner,
        excerpt=art['excerpt'],
        body_html=body_html,
        keypoints_html=keypoints_html,
        factcheck_panel=fc_panel,
        source_url=art['sourceUrl'],
        source_name=art['sourceName'],
        slug=art['slug'],
    )
    path = os.path.join(dir_path, 'preview.html')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    return path


def build_one(cand, config, gallery=0):
    print(f"\n{'='*70}\nBuilding: {cand['title'][:80]}\n{'='*70}")

    print("[1/5] fetching reference text ...")
    reference = fetch_reference(cand['source_url'])

    print("[2/5] writing article ...")
    response, model = call_llm(
        build_prompt(cand, reference, config, gallery),
        "You are a professional English news writer. Strictly factual, no fabrication. Output only valid JSON.",
        temperature=0.5)
    art = extract_json(response)
    if not art or not art.get('body'):
        print("  FAILED: cannot parse article JSON")
        return None
    if not isinstance(art.get('body'), list) or len(art['body']) < 4:
        print(f"  FAILED: body too short ({len(art.get('body', []))} paragraphs)")
        return None
    if art.get('category') not in config['categories']:
        art['category'] = cand['category']

    words = sum(len(p.split()) for p in art['body'])
    slug = title_to_slug(art['title'])
    read_minutes = max(2, words // 200)
    art.setdefault('keyPoints', [])
    art.setdefault('tags', [])

    print("[3/5] fact-checking against reference ...")
    factcheck = run_fact_check(art, reference, cand, config)

    article = {
        'slug': slug,
        'title': art['title'],
        'excerpt': art['excerpt'],
        'category': art['category'],
        'authorId': config['author_by_category'].get(art['category'], 'globdesk'),
        'publishedAt': datetime.now().astimezone().isoformat(timespec='seconds'),
        'readMinutes': read_minutes,
        'tags': art['tags'],
        'coverPrompt': art.get('cover_prompt', ''),
        'coverImage': '',           # publish.py 上传封面后回填 URL
        'featured': False,
        'body': art['body'],
        'keyPoints': art['keyPoints'],
        'sourceName': cand['source_name'],
        'sourceUrl': cand['source_url'],
        # 内部质检报告（import_articles 忽略未知字段，不会入后端库）
        'factCheck': factcheck,
    }

    out_dir = os.path.join(ARTICLES_DIR, datetime.now().strftime('%Y/%m/%d'), slug)
    os.makedirs(out_dir, exist_ok=True)

    # 图片资产走 R2：本地 assets_r2/imgs/news/<slug>/，生成后自动同步，URL 直接写 CDN 域名
    r2 = config.get('assets_r2') or {}
    date_path = datetime.now().strftime('%Y/%m/%d')   # 与 data/articles/YYYY/MM/DD/<slug>/ 对齐
    r2_img_dir = os.path.join(r2.get('local_dir', ''), 'imgs', date_path, slug)
    r2_url_base = f"{r2.get('public_url', '')}/imgs/{date_path}/{slug}"
    os.makedirs(r2_img_dir, exist_ok=True)

    print(f"[4/5] generating cover image ({words} words article) ...")
    cover_path = os.path.join(r2_img_dir, 'cover.jpg')
    try:
        imagegen.generate(art.get('cover_prompt') or art['title'], cover_path, config)
        article['coverImage'] = f"{r2_url_base}/cover.jpg"
    except Exception as e:
        print(f"  image generation FAILED: {e} (article kept, cover empty -> falls back to prompt cover)")

    # 画廊：额外生成 N 张配图，按 image 块插入正文 1/2、2/3 处
    gallery = gallery
    if gallery > 0:
        print(f"[4.5] generating {gallery} gallery image(s) ...")
        paras = [b for b in article['body'] if isinstance(b, str)]
        gallery_prompts = art.get('gallery_prompts') or []
        for gi in range(min(gallery, len(gallery_prompts))):
            gname = f'gallery_{gi + 1}.jpg'
            gpath = os.path.join(r2_img_dir, gname)
            try:
                imagegen.generate(gallery_prompts[gi], gpath, config)
                # 插到正文文字段 1/2 与 2/3 处
                body = article['body']
                text_idx = [j for j, b in enumerate(body) if isinstance(b, str)]
                pos = text_idx[max(1, len(text_idx) * (gi + 1) // (gallery + 1))] if text_idx else 1
                body.insert(pos, {'type': 'image', 'src': f'{r2_url_base}/{gname}',
                                  'caption': gallery_prompts[gi][:80]})
            except Exception as e:
                print(f"  gallery image {gi + 1} FAILED: {e}")

    # 自动同步图片到 R2（增量，只传这篇的目录）
    if r2_img_dir and os.path.isdir(r2_img_dir) and os.listdir(r2_img_dir):
        print("[4.7] syncing images to Cloudflare R2 ...")
        try:
            subprocess.run([r2.get('sync_script'), f'imgs/{date_path}/{slug}'],
                           check=True, timeout=300)
        except Exception as e:
            print(f"  WARNING: R2 sync failed ({e}) — run manually: "
                  f"{r2.get('sync_script')} imgs/{date_path}/{slug}")

    with open(os.path.join(out_dir, 'article.json'), 'w', encoding='utf-8') as f:
        json.dump(article, f, indent=2, ensure_ascii=False)
    preview = write_preview(out_dir, article)
    print(f"  OK -> {out_dir}/  (preview: {preview})")
    if factcheck.get('verdict') != 'pass':
        print(f"  !! fact-check verdict = {factcheck.get('verdict')} — REVIEW THE FACT-CHECK PANEL BEFORE PUBLISHING")
    return out_dir


def main():
    parser = argparse.ArgumentParser(description='Build articles & covers from approved candidates')
    parser.add_argument('file', help='candidates JSON file')
    parser.add_argument('--only', type=str, default=None,
                        help='comma-separated 1-based indices among APPROVED candidates')
    parser.add_argument('--gallery', type=int, default=0,
                        help='extra gallery images per article (0-3)')
    parser.add_argument('--target', choices=['staging'], default='staging',
                        help='生成后自动发布的环境。只支持 staging（生产发布必须走 publish.py --target prod）')
    args = parser.parse_args()

    if args.target != 'staging':
        parser.error('--target 只接受 staging；要发生产请用 publish.py --target prod')

    with open(args.file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 筛选约定：数组里剩下的条目就是要做的（人工直接删掉不要的）。
    # approved 标记仅供 approve.py 使用，不做强制。
    all_c = data.get('candidates', [])
    approved = [c for c in all_c if c.get('approved')]
    if not approved and all_c:
        print(f"Note: no approved:true flags — using all {len(all_c)} entries in the array "
              f"(delete unwanted entries from the JSON instead).")
        approved = all_c
    if not approved:
        print("No candidates in the array. Nothing to build.")
        return
    if args.only:
        idx = {int(x) for x in args.only.split(',') if x.strip()}
        approved = [c for i, c in enumerate(approved, 1) if i in idx]
        print(f"--only: building {len(approved)} of them")

    print(f"Building {len(approved)} article(s) ...")
    config = load_config()
    built = []
    for cand in approved:
        try:
            out = build_one(cand, config, args.gallery)
            if out:
                built.append(out)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            break
        except Exception as e:
            print(f"  FAILED: {e}")

    print(f"\n{'='*70}\nBuilt {len(built)} article(s):")
    for b in built:
        print(f"  {b}")

    # 自动发布到 staging 预览（生成 → staging 一条龙；生产必须显式走 publish.py --target prod）
    if built:
        print(f"\nPublishing {len(built)} article(s) to STAGING for review ...")
        article_files = [os.path.join(b, 'article.json') for b in built]
        r = subprocess.run(
            [sys.executable, 'publish.py',
             '--candidates', json.dumps(article_files),
             '--target', 'staging'],
            timeout=600)
        if r.returncode != 0:
            print("\nWARNING: staging publish had failures — fix and run publish.py manually")
        print(f"\nReview at https://staging.glob.news (BasicAuth + ?preview=<secret>)")
        print(f"Local previews:    open {built[0]}/preview.html")
        print(f"python publish.py --date {datetime.now().strftime('%Y/%m/%d')} --target prod")
        print(f"Publish to prod:   python publish.py --article {built[0]}/article.json --target prod")


if __name__ == '__main__':
    main()
