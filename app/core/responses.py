"""core/responses:通用响应工具(SSE event 格式化、文件下载响应)。"""
import io
import json
from urllib.parse import quote

from fastapi.responses import StreamingResponse


def sse_event(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _make_download_response(data: bytes, mime: str, filename: str) -> StreamingResponse:
    """构建带 RFC 5987 编码文件名的下载响应。"""
    encoded = quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        "Content-Length": str(len(data)),
    }
    return StreamingResponse(io.BytesIO(data), media_type=mime, headers=headers)
