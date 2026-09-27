"""制备谱系模块的业务异常。"""

from __future__ import annotations

from typing import Any

from ..errors import ConflictError, DomainError, NotFoundError, PermissionDenied, ValidationError


class FrozenError(ConflictError):
    """对象已被冻结，不能再执行会改变数量或状态的动作。"""

    code = "frozen"

    def __init__(self, message: str, evaluation: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.evaluation = evaluation


class TransferStateError(ConflictError):
    """跨摊转移的状态不允许当前动作。"""

    code = "transfer_state"


class InventoryError(ConflictError):
    """操作会导致库存余额为负或破坏数量守恒。"""

    code = "inventory_shortage"


class ManualConfirmationRequired(ConflictError):
    """存在慎用提示，必须由人工显式确认后才能发放。"""

    code = "manual_confirmation_required"

    def __init__(self, message: str, evaluation: dict[str, Any]) -> None:
        super().__init__(message)
        self.evaluation = evaluation


class ServingDenied(PermissionDenied):
    """命中明确忌口或容器不可发放，不得提供。"""

    code = "serving_denied"

    def __init__(self, message: str, evaluation: dict[str, Any]) -> None:
        super().__init__(message)
        self.evaluation = evaluation


__all__ = [
    "DomainError",
    "ConflictError",
    "NotFoundError",
    "PermissionDenied",
    "ValidationError",
    "FrozenError",
    "TransferStateError",
    "InventoryError",
    "ManualConfirmationRequired",
    "ServingDenied",
]
