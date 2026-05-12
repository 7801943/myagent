"""
权限检查工具。

权限格式：`<domain>:<action>`，如 `chat`、`tool:cli_execute`、`tool:file_read`。
通配符：`tool:*` 表示所有工具权限。
空权限列表 → 允许所有（默认放行）。
"""


def check_permission(permissions: list[str], required: str) -> bool:
    """
    检查用户是否拥有所需权限。

    Args:
        permissions: 用户的权限列表（来自 UserContext.permissions）
        required: 所需权限标识（如 "chat"、"tool:cli_execute"）

    Returns:
        True 表示有权限，False 表示无权限
    """
    if not permissions:
        return True  # 无权限列表 → 默认放行
    if required in permissions:
        return True
    # 通配符匹配：tool:cli_execute → 检查 tool:*
    if ":" in required:
        prefix = required.split(":")[0] + ":*"
        return prefix in permissions
    return False


class PermissionDenied(Exception):
    """权限不足异常。"""
    def __init__(self, permission: str, user_id: str = ""):
        self.permission = permission
        self.user_id = user_id
        super().__init__(f"Permission denied: '{permission}' for user '{user_id}'")