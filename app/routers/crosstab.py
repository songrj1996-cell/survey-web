"""routers/crosstab:倍市得跑数表模式上传接口（HTTP 壳）。

文件解析、列结构生成、session 创建全部在 services/crosstab_service。
后续方案/统计/报告复用 survey 的共享流程，仅上传入口在此。
"""
from fastapi import APIRouter, File, Request, UploadFile

from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.crosstab_service import handle_crosstab_upload

router = APIRouter()


@router.post("/api/upload/crosstab")
async def upload_crosstab(
    request: Request,
    survey_file: UploadFile = File(...),
    data_file: UploadFile = File(...),
    crosstab_file: UploadFile = File(...),
):
    survey_content = await survey_file.read()
    data_content = await data_file.read()
    ct_content = await crosstab_file.read()
    login = await _current_login(request)
    result = await handle_crosstab_upload(
        survey_content, survey_file.filename or "survey.xlsx",
        data_content, data_file.filename or "data.xlsx",
        ct_content, crosstab_file.filename or "crosstab.xlsx",
        login,
    )
    await audit_log(
        request, "survey", "上传跑数表数据",
        f"问卷：{survey_file.filename or '?'}；数据：{data_file.filename or '?'}；"
        f"跑数表：{crosstab_file.filename or '?'}；样本行数：{result['total_rows']}",
        metadata={"session_id": result["session_id"], "rows": result["total_rows"], "mode": "crosstab"},
    )
    return result
