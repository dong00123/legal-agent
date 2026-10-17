"""接口层。

    api/
      deps.py         公共依赖：登录主体、客户端识别、分页、异常构造
      v1/             按业务域拆分的路由
        meta.py       /health /meta/*         （无需登录）
        auth.py       /auth/*                 （登录、改口令、团队）
        cases.py      /cases/*                （案件与材料）
        chat.py       /chat /contract/review /doc/draft /search/*
        tasks.py      /tasks/*                （任务中心、Trace、引用）
        docs.py       /drafts/*               （文书草稿与导出）
        knowledge.py  /knowledge/* /statutes/* /updates/*
        admin.py      /admin/*                （团队、用户、配置、审计、统计）
        ws.py         /ws/tasks/{id}          （进度推送）

分层约定：**接口层不写业务规则**。校验入参、取登录主体、调 service、
把异常交给 ``main.py`` 注册的处理器。判定"能不能看这个案件"的规则只有
一份，在 ``security.can_access_case``；接口层只负责把 ``current_user``
传下去（漏传会抛错而不是静默放行，见 ``services/case_service.INTERNAL``）。

**这个文件刻意不 import 任何子模块。** 否则 ``import backend.app.api``
就会把整棵路由树连同 FastAPI 依赖一起拉起来，而 ``scripts/`` 里的脚本、
测试收集阶段、以及 Alembic 类工具常常只需要其中一个模块。
"""
