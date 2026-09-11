"""Persist real output files as bounded, immutable browser assets."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from naumi_agent.config.settings import AppConfig
from naumi_agent.tools.base import Tool, ToolMetadata
from naumi_agent.tools.output_protocol import RICH_OUTPUT_PROTOCOL

MAX_ASSET_BYTES = 20 * 1024 * 1024
ASSET_TYPES = {
    '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
    '.webp': 'image/webp', '.gif': 'image/gif', '.pdf': 'application/pdf',
    '.csv': 'text/csv', '.json': 'application/json', '.txt': 'text/plain',
    '.md': 'text/markdown',
}
ASSET_NAME = re.compile(r'^[a-f0-9]{64}\.(svg|png|jpg|webp|gif|pdf|csv|json|txt|md)$')
_SVG_TAGS = {
    'svg', 'g', 'defs', 'title', 'desc', 'path', 'rect', 'circle', 'ellipse',
    'line', 'polyline', 'polygon', 'text', 'tspan', 'textPath', 'linearGradient',
    'radialGradient', 'stop', 'clipPath', 'mask', 'pattern', 'use', 'marker',
}


def asset_root(config: AppConfig) -> Path:
    return Path(config.memory.session_db_path).resolve().parent / 'output-assets'


def validate_svg(content: bytes) -> bytes:
    if len(content) > 500_000:
        raise ValueError('SVG 超过 500 KB，请简化图片')
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', content, re.I):
        raise ValueError('SVG 不支持外部实体或文档类型声明')
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError('SVG 格式不完整，请检查标签是否闭合') from exc
    if root.tag not in {'svg', '{http://www.w3.org/2000/svg}svg'}:
        raise ValueError('图片必须以 svg 元素为根节点')
    nodes = list(root.iter())
    if len(nodes) > 10000:
        raise ValueError('SVG 元素过多，请简化图片')
    for element in nodes:
        if element.tag.startswith('{') and not element.tag.startswith('{http://www.w3.org/2000/svg}'):
            raise ValueError('SVG 包含不支持的命名空间')
        if element.tag.split('}')[-1] not in _SVG_TAGS:
            raise ValueError('SVG 仅支持静态矢量元素，不支持脚本、嵌入网页或动画')
        for key, value in element.attrib.items():
            name = key.split('}')[-1].lower()
            if name.startswith('on') or name in {'src', 'base'}:
                raise ValueError('SVG 不支持事件处理器或外部资源')
            if name == 'href' and not re.fullmatch(r'#[\w.-]+', value):
                raise ValueError('SVG 引用只能指向图片内部元素')
            if '\\' in value or re.search(r'@import|expression\s*\(|javascript:', value, re.I):
                raise ValueError('SVG 样式包含不支持的内容')
            for url in re.findall(r'url\s*\((.*?)\)', value, re.I):
                if not re.fullmatch(r"['\"]?#[\w.-]+['\"]?", url.strip()):
                    raise ValueError('SVG 样式不能加载外部资源')
    return content


def validate_asset(content: bytes, suffix: str) -> None:
    if not content or len(content) > MAX_ASSET_BYTES:
        raise ValueError('文件不能为空，且不能超过 20 MB')
    if suffix not in ASSET_TYPES:
        raise ValueError('支持 SVG、PNG、JPG、WEBP、GIF、PDF、CSV、JSON、TXT、MD')
    if suffix == '.svg':
        validate_svg(content)
    elif suffix in {'.png', '.jpg', '.gif', '.webp'}:
        formats = {'.png': 'PNG', '.jpg': 'JPEG', '.gif': 'GIF', '.webp': 'WEBP'}
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.format != formats[suffix]:
                    raise ValueError('图片内容与文件扩展名不一致')
                pixels = image.width * image.height
                frames = getattr(image, 'n_frames', 1)
                if pixels > 40_000_000 or frames > 200 or pixels * frames > 100_000_000:
                    raise ValueError('图片像素或动画帧数过大，请缩小后返回')
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError('图片无法解码或尺寸过大，请重新生成') from exc
    elif suffix == '.pdf' and not content.startswith(b'%PDF-'):
        raise ValueError('PDF 文件头无效')
    elif suffix in {'.csv', '.json', '.txt', '.md'}:
        try:
            content.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError('文本文件请使用 UTF-8 编码') from exc
        if suffix == '.json':
            try:
                json.loads(content)
            except (ValueError, RecursionError) as exc:
                raise ValueError('JSON 文件格式无效') from exc


def publish_bytes(config: AppConfig, content: bytes, suffix: str) -> str:
    validate_asset(content, suffix)
    name = hashlib.sha256(content).hexdigest() + suffix
    directory = asset_root(config)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    # Atomic hard-link publication never replaces an asset used by readers.
    fd, temporary = tempfile.mkstemp(prefix='.publish-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink() or target.read_bytes() != content:
                raise ValueError('已保存的内容校验失败，请检查资源目录') from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return f'/api/v1/output-assets/{name}'


class OutputPublishTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return 'output_publish'

    @property
    def description(self) -> str:
        return ('返回图片、数据文件或交互组件。传入 svg 生成真实矢量图片；'
                '传入 path 发布工作区已有图片/PDF/CSV/JSON/文本文件。'
                '无参数查看卡片、图表、表格、HTML 交互组件的输出协议。')

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            concurrency_safe=True, requires_confirmation=False,
            user_facing_name='生成与返回内容',
            search_hint='image svg picture visualization chart table cards interactive html output',
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': '工作区内已有文件的路径'},
            'svg': {'type': 'string', 'maxLength': 500000,
                    'description': '自包含静态 SVG 源码，生成实际图片文件'},
            'title': {'type': 'string', 'maxLength': 120, 'description': '图片或文件标题'},
        }, 'additionalProperties': False}

    async def execute(self, path: str = '', svg: str = '', title: str = '') -> str:
        if not path and not svg:
            return RICH_OUTPUT_PROTOCOL
        if path and svg:
            raise ValueError('path 与 svg 只能选择一种')
        if len(title) > 120:
            raise ValueError('标题不能超过 120 个字符')
        config = self._engine.config
        if svg:
            content, suffix = svg.encode('utf-8'), '.svg'
        else:
            workspace = Path(self._engine.workspace_root).resolve()
            candidate = Path(path).expanduser()
            source = (candidate if candidate.is_absolute() else workspace / candidate).resolve()
            if not source.is_relative_to(workspace) or '.git' in source.parts:
                raise ValueError('只能返回当前工作区内的文件')
            if not source.is_file():
                raise ValueError('文件不存在，请先生成或确认路径')
            if source.stat().st_size > MAX_ASSET_BYTES:
                raise ValueError('文件超过 20 MB，请拆分或压缩后返回')
            suffix = source.suffix.lower()
            suffix = '.jpg' if suffix == '.jpeg' else suffix
            content = await asyncio.to_thread(self._read_bounded, source)
            title = title or source.name
        url = await asyncio.to_thread(publish_bytes, config, content, suffix)
        label = re.sub(r'[\[\]\r\n\\]', ' ', title or '生成图片').strip()
        prefix = '!' if ASSET_TYPES[suffix].startswith('image/') else ''
        markdown = f'{prefix}[{label}]({url})'
        return json.dumps({'url': url, 'mime_type': ASSET_TYPES[suffix],
                           'bytes': len(content), 'markdown': markdown}, ensure_ascii=False)

    @staticmethod
    def _read_bounded(path: Path) -> bytes:
        with path.open('rb') as stream:
            return stream.read(MAX_ASSET_BYTES + 1)
