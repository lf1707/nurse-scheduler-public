"""Default content shown on the public About page."""

from __future__ import annotations

PUBLIC_REPOSITORY_URL = "https://github.com/lf1707/nurse-scheduler-public"
PUBLIC_LICENSE_URL = f"{PUBLIC_REPOSITORY_URL}/blob/main/LICENSE"


def default_about_text(app_name: str) -> str:
    return f"""{app_name} 是一个面向医院护理团队的多租户智能排班系统，源码仓库：{PUBLIC_REPOSITORY_URL}。

本项目参考并移植了 roster-wizard 的 OR-Tools CP-SAT 排班思路，但两者定位不同：
1. roster-wizard 是基于 Django 的单实例排班工具，核心是录入员工、技能配比、班次序列、请假和偏好后生成班表。
2. {app_name} 重构为 FastAPI + SQLAlchemy + PostgreSQL 服务端架构，面向多机构 SaaS 场景，使用 PostgreSQL RLS 隔离租户数据，并提供租户、订阅、用户角色与平台管理能力。
3. 领域模型更贴近护理场景：护士档案维护业务角色、技能、科室、请假和个人排班约束；约束可明确启用，未启用时保持无限制，避免默认值被误写成强制规则。
4. 生成链路扩展为 Celery 后台任务，前端轮询生成进度，保存历史结果，支持 CSV/TXT/JSON/PDF 导出与无解原因诊断。
5. 提供中文管理界面和开箱即用的生产部署方案，包括 Docker Compose、Nginx TLS、数据库备份、CI 与非 root 运行。

感谢 roster-wizard 提供技能配比、班次序列与 CP-SAT 求解基础；{app_name} 在其基础上重新实现服务端、数据模型和运维边界，面向国内医院护理排班场景。"""
