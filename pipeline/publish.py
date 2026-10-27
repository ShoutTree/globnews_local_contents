#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
步骤 4：发布。

流程（每篇）：校验 article.json → 上传封面/画廊图到 los1 /media/ → 回填 coverImage URL
           → import_articles 入库（--status staging|published）→ 验证

用法：
    # 单篇
    python publish.py --article data/articles/2026/10/24/<slug>/article.json --target prod

    # 批量：发布某个日期目录下的全部文章（YYYY-MM-DD 或 YYYY/MM/DD）
    python publish.py --date 2026-10-24 --target prod

    # 批量：发布任意目录下的全部文章（递归找 article.json）
    python publish.py --dir data/articles/2026/10 --target prod

    # 预览（列出将发布什么，不执行）
    python publish.py --date 2026-10-24 --target prod --dry-run
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import urllib.request

from common import ARTICLES_DIR, load_config

REQUIRED = ['slug', 'title', 'excerpt', 'category', 'authorId', 'body', 'sourceUrl']


def run(cmd, **kw):
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def publish_one(art_path: str, target: str, config) -> bool:
    """发布单篇。返回成功与否。"""
    site = config['site']
    with open(art_path, 'r', encoding='utf-8') as f:
        art = json.load(f)

    missing = [k for k in REQUIRED if not art.get(k)]
    if missing:
        print(f"  SKIP {art_path}: missing fields {missing}")
        return False
    if not isinstance(art.get('body'), list) or len(art['body']) < 4:
        print(f"  SKIP {art_path}: body too short")
        return False

    slug = art['slug']
    art_dir = os.path.dirname(os.path.abspath(art_path))

    print(f"== publishing [{target}] {slug}")

    # 1. 上传封面 + 画廊图片（存在时）
    cover_local = os.path.join(art_dir, 'cover.jpg')
    cover_remote = f"{site['media_dir']}/{slug}.jpg"
    cover_url = f"{site['media_url_prefix']}/{slug}.jpg"

    uploads = []
    if os.path.exists(cover_local):
        uploads.append((cover_local, cover_remote))
        art['coverImage'] = cover_url
    else:
        print("  [img] no cover.jpg; coverImage falls back to prompt cover")

    # 画廊图：gallery_N.jpg → media，并把 body 中 __GALLERY__gallery_N.jpg 占位替换为 URL
    for i in range(1, 4):
        gname = f'gallery_{i}.jpg'
        glocal = os.path.join(art_dir, gname)
        if os.path.exists(glocal):
            uploads.append((glocal, f"{site['media_dir']}/{slug}-{gname}"))
            gurl = f"{site['media_url_prefix']}/{slug}-{gname}"
            for b in art.get('body', []):
                if isinstance(b, dict) and b.get('src') == f'__GALLERY__{gname}':
                    b['src'] = gurl

    if uploads:
        print(f"  [img] uploading {len(uploads)} image(s) ...")
        for local, remote in uploads:
            r = run(['scp', local, f"{site['ssh_host']}:{remote}"])
            if r.returncode != 0:
                print(f"  FAILED image upload: {r.stderr}")
                return False

    # 2. 上传 article.json 并导入 Django
    status = 'staging' if target == 'staging' else 'published'
    art_file = os.path.join(art_dir, '_publish_tmp.json')
    with open(art_file, 'w', encoding='utf-8') as f:
        json.dump(art, f, ensure_ascii=False)
    remote_json = f"/tmp/globnews_{slug}.json"
    r = run(['scp', art_file, f"{site['ssh_host']}:{remote_json}"])
    os.unlink(art_file)
    if r.returncode != 0:
        print(f"  FAILED json upload: {r.stderr}")
        return False
    r = run(['ssh', site['ssh_host'],
             f"{site['manage']} import_articles {remote_json} --status {status}"])
    print('  ' + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''))
    if r.returncode != 0:
        print(f"  FAILED import: {r.stderr}")
        return False

    # 3. 验证（staging 域名有 BasicAuth，走服务器内网直连 Django 验证）
    secret = get_preview_secret(site) if status == 'staging' else ''
    verify = (f"curl -s -o /dev/null -w '%{{http_code}}' 'http://127.0.0.1:8001/api/news/posts/{slug}/"
              + (f"?preview={secret}'" if secret else "'"))
    rv = run(['ssh', site['ssh_host'], verify])
    code = rv.stdout.strip()
    ok = code == '200'
    print(f"  API check -> {code} {'OK' if ok else 'FAIL'}")
    if not ok:
        return False
    if status == 'staging':
        print(f"  preview: {site['staging_url']}/article/{slug}?preview={secret}")
    else:
        print(f"  live:    https://glob.news/article/{slug}")
    return True


def get_preview_secret(site):
    r = run(['ssh', site['ssh_host'],
             "grep '^PREVIEW_SECRET=' /www/wwwroot/backend_services/glob.news/service_login/.env | cut -d= -f2"])
    return r.stdout.strip() if r.returncode == 0 else ''


def get_remote_status(slug: str, site) -> str:
    """查文章在远端的状态：'published' | 'staging' | 'missing' | 'unknown'。"""
    r = run(['ssh', site['ssh_host'],
             f"{site['manage']} shell -c \"from api_news.models import NewsPost; p=NewsPost.objects.filter(slug='{slug}').first(); print(p.status if p else 'missing')\""])
    out = r.stdout.strip().splitlines()
    status = out[-1].strip() if out else 'unknown'
    return status if status in ('published', 'staging', 'missing') else 'unknown'


def collect_article_files(args) -> list:
    """根据参数收集要发布的 article.json 路径列表。"""
    if args.candidates:
        try:
            paths = json.loads(args.candidates)
        except json.JSONDecodeError as e:
            sys.exit(f"--candidates must be a JSON list: {e}")
        if not isinstance(paths, list):
            sys.exit('--candidates must be a JSON list of article.json paths')
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            sys.exit(f"article files not found: {missing}")
        return paths
    if args.article:
        if not os.path.exists(args.article):
            sys.exit(f"article not found: {args.article}")
        return [args.article]
    if args.date:
        y, m, d = args.date.replace('-', '/').split('/')
        date_dir = os.path.join(ARTICLES_DIR, y, m, d)
        if not os.path.isdir(date_dir):
            sys.exit(f"date directory not found: {date_dir}")
        return sorted(glob.glob(os.path.join(date_dir, '*', 'article.json')))
    if args.dir:
        if not os.path.isdir(args.dir):
            sys.exit(f"directory not found: {args.dir}")
        return sorted(glob.glob(os.path.join(args.dir, '**', 'article.json'), recursive=True))
    sys.exit("specify --article <path> | --date YYYY-MM-DD | --dir <path>")


def main():
    parser = argparse.ArgumentParser(description='Publish article(s) to glob.news (staging or prod)')
    parser.add_argument('--article', default=None, help='single article.json path')
    parser.add_argument('--candidates', default=None,
                        help='(internal, used by build_articles.py) JSON list of article.json paths')
    parser.add_argument('--date', default=None, help='publish all articles under data/articles/YYYY/MM/DD/')
    parser.add_argument('--dir', default=None, help='publish all article.json under a directory (recursive)')
    parser.add_argument('--target', choices=['staging', 'prod'], default='staging')
    parser.add_argument('--dry-run', action='store_true', help='list what would be published, do nothing')
    parser.add_argument('--featured', action='store_true',
                        help='置为首页精选：取消其他文章 featured，本次发布的文章 featured=True')
    args = parser.parse_args()

    config = load_config()
    files = collect_article_files(args)
    if not files:
        sys.exit('no article.json found for the given --article/--date/--dir')

    # 摘要预览
    names = []
    for fp in files:
        try:
            with open(fp, encoding='utf-8') as f:
                a = json.load(f)
            names.append(f"{a.get('slug', '?')[:60]}  <-  {os.path.dirname(fp)}")
        except Exception as e:
            names.append(f"(unreadable: {fp}: {e})")

    print(f"Publishing {len(files)} article(s) -> [{args.target}]\n")
    for n in names:
        print(f"  - {n}")
    print()

    if args.dry_run:
        print("DRY RUN — nothing published.")
        return

    # --featured：先取消全部精选，最后一篇发布时置为 featured
    if args.featured and not args.dry_run:
        site = config['site']
        r = run(['ssh', site['ssh_host'],
                 f"{site['manage']} shell -c \"from api_news.models import NewsPost; n=NewsPost.objects.filter(featured=True).update(featured=False); print(f'cleared {{n}} featured')\""])
        print('  ' + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else 'no prior featured'))
        # 最后一篇标 featured（写回其 article.json，随正常导入流程生效）
        if files:
            with open(files[-1], encoding='utf-8') as f:
                last = json.load(f)
            last['featured'] = True
            with open(files[-1], 'w', encoding='utf-8') as f:
                json.dump(last, f, ensure_ascii=False, indent=2)

    site = config['site']
    ok = fail = skipped = 0
    for fp in files:
        try:
            # 发布前置检查
            with open(fp, encoding='utf-8') as f:
                _art = json.load(f)
            slug = _art.get('slug', '?')
            remote = get_remote_status(slug, site)
            if args.target == 'staging' and remote == 'staging':
                print(f"== SKIP (already in staging) {slug}")
                skipped += 1
                continue
            if args.target == 'prod' and remote != 'staging':
                if remote == 'published':
                    print(f"== SKIP (already published) {slug}")
                    skipped += 1
                    continue
                print(f"== ERROR {slug}: not in staging (remote status: {remote}).")
                print("   流程要求先过 staging 预览确认，再发生产：")
                print(f"   python publish.py --article {fp} --target staging")
                fail += 1
                continue
            if publish_one(fp, args.target, config):
                ok += 1
            else:
                fail += 1
        except KeyboardInterrupt:
            print("\nInterrupted.")
            break
        except Exception as e:
            fail += 1
            print(f"  FAILED {fp}: {e}")

    print(f"\n{'=' * 60}\nDone: {ok} published, {skipped} skipped, {fail} failed ({args.target})")
    if fail:
        sys.exit(1)


if __name__ == '__main__':
    main()
