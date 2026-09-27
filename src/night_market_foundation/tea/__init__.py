"""药膳茶饮制备谱系业务模块。

在基础服务（组织/操作者/场所、角色权限、请求幂等、SQLite 事务、哈希审计链）
之上，提供原料批号、配方版本、制备谱系、守恒流水、忌口判定、冻结召回与
跨摊转移能力。
"""

from __future__ import annotations

from .service import TeaService

__all__ = ["TeaService"]
