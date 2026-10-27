#!/usr/bin/env bash
# assets_r2/ → Cloudflare R2 bucket (glob-news-assets) 增量同步
#
# 用法:
#   ./sync_to_r2.sh                    # 全量同步 assets_r2/
#   ./sync_to_r2.sh imgs/news/<slug>   # 只同步某子目录（生成流水线按需调用）
#   ./sync_to_r2.sh --dry-run          # 预览将上传/删除什么
#   ./sync_to_r2.sh --no-delete        # 不删远端多余对象
#
# 线上 URL: https://assets.glob.news/<相对路径>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/sync_to_r2.py" "$@"
