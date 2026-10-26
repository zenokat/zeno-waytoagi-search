"""后三工具共用的单篇文章缓存：七天内复用同修订内容，按需补格式和选区。

社区日志仍按原月级和条目规则读取；本模块只由全文检索、篇章结构与正文读取调用。
图片只复用仍存在的本地文件；缓存与素材均为单机可清除的原型资产。
"""

import copy
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlparse

from experiments.zen43.core.source import write_json

ARTICLE_TTL_SECONDS = 7 * 24 * 60 * 60
ARTICLE_CACHE_VERSION = 1


class ArticleRevisionChanged(ValueError):
    """一次操作取得不同修订时的内部信号；由调用协调层重做，不直接作为首次业务错误。"""


def retry_article_snapshot(operation, *args, **kwargs):
    """执行 operation 及其参数并返回业务结果；修订变化后用已刷新的快照最多重做一次。

    fetch_article 已在观察到不同修订时淘汰旧快照；重做整个操作会重新解析章节和选区，
    不把前一次取得的片段带入结果。仅捕获修订信号，来源读取或参数错误仍直接抛出。
    """
    for attempt in range(2):
        try:
            return operation(*args, **kwargs)
        except ArticleRevisionChanged as error:
            if attempt == 1:
                raise ValueError("文章持续更新，自动刷新后仍无法取得一致内容；请稍后重试") from error


def load_article_cache(context, article):
    """输入调用 context 与 article URL/token，返回缓存路径及有效快照；缺失、损坏或过期返回空字典。"""
    parsed = urlparse(article)
    token = parsed.path.rstrip("/").rsplit("/", 1)[-1] if parsed.scheme else article
    kind = "docx" if "/docx/" in parsed.path else "wiki"
    key = hashlib.sha256(json.dumps([context.profile or "default", kind, token]).encode()).hexdigest()[:24]
    path = context.cache / f"article-{key}.json"
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        return path, {}
    if not isinstance(cached, dict) or cached.get("version") != ARTICLE_CACHE_VERSION:
        return path, {}
    fetched_at = cached.get("fetched_at")
    if not isinstance(fetched_at, (int, float)) or not 0 <= time.time() - fetched_at < ARTICLE_TTL_SECONDS:
        return path, {}
    return path, cached


def fetch_article(context, article, scope="full", format="xml", **options):
    """按 context、article、scope、format 和 options 取得文章；返回文档副本，先缓存后按需请求。"""
    path, cached = load_article_cache(context, article)
    parameters = {"scope": scope, "format": format, "max_depth": -1, **options}
    request_key = json.dumps(parameters, sort_keys=True)
    document = cached.get("responses", {}).get(request_key)
    hit = document is not None
    if not hit:
        # 仍由原 fetch 完成去评论及 tips/excerpt 检查，失败响应不会进入缓存。
        document = context.fetch(article, scope, format, **options)
        if not cached or cached["revision"] != document["revision_id"]:
            # 新请求观察到修订变化时，整个旧快照失效，不把两个修订的内容拼起来。
            cached = {"version": ARTICLE_CACHE_VERSION, "fetched_at": time.time(),
                      "revision": document["revision_id"], "responses": {}, "images": {}}
        cached["responses"][request_key] = document
        # 补一种格式不修改 fetched_at，避免不断延长旧内容期限。
        write_json(path, cached)
    context.diagnostics.setdefault("article_cache", []).append(
        {"article": article, **parameters, "cache_hit": hit, "revision": document["revision_id"],
         "snapshot_fetched_at": cached["fetched_at"], "cache_path": str(path)})
    return copy.deepcopy(document)


def preview_article_image(context, article, token, output):
    """按 context、article、token 复用已下载图片；未命中时下载至 output，返回实际素材元数据。"""
    path, cached = load_article_cache(context, article)
    image = cached.get("images", {}).get(token)
    hit = bool(image and Path(image.get("saved_path", "")).is_file())
    if not hit:
        image = context.call(["docs", "+media-preview", "--token", token, "--output", str(output)])["data"]
        if cached:
            cached.setdefault("images", {})[token] = image
            write_json(path, cached)
    context.diagnostics.setdefault("article_images", []).append({"token": token, "cache_hit": hit})
    return copy.deepcopy(image)
