"""Authenticated access to immutable, explicitly published output assets."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from naumi_agent.api.deps import AuthDep
from naumi_agent.tools.output_publish import ASSET_NAME, ASSET_TYPES, asset_root

router = APIRouter()


@router.get('/output-assets/{name}')
async def get_output_asset(name: str, request: Request, auth: str = AuthDep):
    if not ASSET_NAME.fullmatch(name):
        raise HTTPException(status_code=404, detail='内容不存在或尚未生成')
    directory = asset_root(request.app.state.config).resolve()
    path = directory / name
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory):
        raise HTTPException(status_code=404, detail='内容不存在或已被清理，请重新生成')
    mime = ASSET_TYPES[path.suffix]
    return FileResponse(
        path, media_type=mime, filename=name,
        content_disposition_type='inline' if mime.startswith('image/') else 'attachment',
        headers={
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            'Cache-Control': 'private, max-age=31536000, immutable',
        },
    )
