"""routers:HTTP 接口,每条业务线一个文件。

边界:收请求、做基础/权限校验、调 service、转 HTTP 响应;不编排复杂业务流程、
不直接拼 Dify prompt、不直接操作外部系统、不直接读写 JSON 文件。
"""
