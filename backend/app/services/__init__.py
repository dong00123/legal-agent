"""业务服务层。

分层：``api/`` 只做协议转换（解析请求、鉴权、序列化），业务规则一律落在这里。
这样 Web 端与 EXE 端、以及管理脚本调用的是**同一套规则** —— 而不是"接口里一套、
脚本里另一套"，后者迟早会分叉出两种行为。

**本文件刻意不 import 任何子模块。** 服务之间互相引用（``stats_service`` 汇总
其余各表、``knowledge_service`` 读 ``config_service`` 的白名单），
如果在包的 ``__init__`` 里统一导入，会让"导入任意一个服务"变成"导入全部服务"，
进而把延迟导入写成的依赖关系重新变成循环导入。
各调用方按需 ``from backend.app.services.xxx_service import ...``。

模块清单：

======================  ====================================================
task_service            任务记录读写（落库、裁剪、查询）
orchestrator            任务生命周期编排（graph.run + 落库 + 归档）
user_service            用户与团队
case_service            案件档案与材料（含材料访问审计）
knowledge_service       知识库 / 法规库 / 更新包 / 系统配置
config_service          运行时可改配置白名单
citation_service        引用终审结果落表与统计
draft_service           文书草稿（双端续编）
audit_service           审计查询（只读，不提供删除）
stats_service           运营统计（任务量 / 采纳率 / 拦截率 / 耗时分布）
======================  ====================================================
"""

__all__: list[str] = []
