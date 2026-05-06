"""兼容性垫片：SecretManager 已移至 myagent.safety.secrets。"""
from myagent.safety.secrets import SecretManager

__all__ = ["SecretManager"]