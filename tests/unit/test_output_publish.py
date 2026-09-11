import asyncio
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from naumi_agent.api.routes.output_assets import router
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import APIConfig, AppConfig, MemoryConfig
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolResult
from naumi_agent.tools.output_publish import OutputPublishTool, asset_root, validate_asset

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
       '<rect width="50" height="30" fill="#789"/></svg>')


@pytest.fixture
def context(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    config = AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / 'sessions.db')),
                       api=APIConfig(api_keys=['test-token']))
    return SimpleNamespace(config=config, workspace_root=workspace)


async def test_generate_svg_and_publish_immutable_concurrently(context):
    tool = OutputPublishTool(context)
    results = await asyncio.gather(*(tool.execute(svg=SVG, title='图表') for _ in range(4)))
    parsed = [json.loads(r) for r in results]
    assert len({r['url'] for r in parsed}) == 1
    assert parsed[0]['markdown'].startswith('![图表](/api/v1/output-assets/')
    files = list(asset_root(context.config).iterdir())
    assert len(files) == 1
    assert files[0].read_text() == SVG


@pytest.mark.parametrize('svg', [
    '<svg><script>alert(1)</script></svg>',
    '<svg><foreignObject><p>bad</p></foreignObject></svg>',
    '<svg onload="alert(1)"/>', '<svg><use href="https://example.com/x"/></svg>',
    '<svg><rect fill="url(https://example.com/a)"/></svg>',
    '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',
    '<svg><rect></svg>', '<html/>',
])
async def test_reject_active_or_malformed_svg(context, svg):
    with pytest.raises(ValueError):
        await OutputPublishTool(context).execute(svg=svg)
    assert not asset_root(context.config).exists()


async def test_workspace_boundary_missing_empty_and_invalid_files(context, tmp_path):
    tool = OutputPublishTool(context)
    (tmp_path / 'private.txt').write_text('private')
    (context.workspace_root / 'empty.txt').write_text('')
    for path in ('../private.txt', 'missing.png', 'empty.txt'):
        with pytest.raises(ValueError):
            await tool.execute(path=path)
    with pytest.raises(ValueError):
        await tool.execute(path='anything', svg=SVG)
    with pytest.raises(ValueError):
        validate_asset(b'not an image', '.png')
    with pytest.raises(ValueError):
        validate_asset(b'{}', '.exe')
    with pytest.raises(ValueError):
        validate_asset(b'{', '.json')
    with pytest.raises(ValueError):
        validate_asset(b'\xff', '.txt')
    with pytest.raises(ValueError):
        validate_asset(b'x' * (20 * 1024 * 1024 + 1), '.txt')


async def test_raster_decode_and_asset_authentication(context):
    output = io.BytesIO()
    Image.new('RGB', (64, 32), '#889977').save(output, format='PNG')
    source = context.workspace_root / 'result.png'
    source.write_bytes(output.getvalue())
    result = json.loads(await OutputPublishTool(context).execute(path='result.png'))
    source.unlink()
    app = FastAPI()
    app.state.config = context.config
    app.include_router(router, prefix='/api/v1')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://test',
    ) as client:
        assert (await client.get(result['url'])).status_code == 401
        response = await client.get(result['url'], headers={'Authorization': 'Bearer test-token'})
        assert response.status_code == 200
        assert response.content == output.getvalue()
        assert response.headers['content-type'] == 'image/png'
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert 'sandbox' in response.headers['content-security-policy']
        headers = {'Authorization': 'Bearer test-token'}
        missing = await client.get('/api/v1/output-assets/' + '0' * 64 + '.svg', headers=headers)
        assert missing.status_code == 404
        invalid = await client.get('/api/v1/output-assets/config.yaml', headers=headers)
        assert invalid.status_code == 404


async def test_shared_slash_path_publishes_actual_file(context):
    (context.workspace_root / 'report.csv').write_text('name,value\nA,12', encoding='utf-8')
    tool = OutputPublishTool(context)

    async def execute_tool(call, **kwargs):
        assert call.name == 'output_publish'
        return ToolResult(call_id=call.id, status='success',
                          content=await tool.execute(**json.loads(call.arguments)))

    context.execute_tool = execute_tool
    result = await execute_slash_command(context, '/output report.csv')
    assert '[report.csv](/api/v1/output-assets/' in result
    help_text = await execute_slash_command(context, '/output')
    assert 'metrics' in help_text and 'html' in help_text


def test_output_is_known_to_permission_system(context):
    checker = PermissionChecker(mode=PermissionMode.MODERATE)
    decision = checker.check('output_publish', {'svg': SVG}, tool=OutputPublishTool(context))
    assert decision.allowed
