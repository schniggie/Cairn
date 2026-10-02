from __future__ import annotations

import ipaddress
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalize_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if any(ord(c) < 33 for c in value) or len(value) > 2048:
        raise ValueError('目标地址包含空白/控制字符或过长')
    if '://' not in value:
        value = 'http://' + value
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError('请提供 HTTP(S) 目标地址，账号密码应通过测试身份提供')
    try:
        parsed.port
        host = parsed.hostname.encode('idna').decode('ascii')
    except (ValueError, UnicodeError) as exc:
        raise ValueError('目标地址无效') from exc
    if any(c in host for c in ('%', '\\', '*')):
        raise ValueError('目标主机无效')
    if ':' not in host and not all(part and all(c.isalnum() or c == '-' for c in part) for part in host.rstrip('.').split('.')):
        raise ValueError('目标主机无效')
    if ':' in host:
        ipaddress.ip_address(host)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or '/', parsed.query, ''))


def normalize_repo(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if not value.startswith('/') or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError('请提供 Kali 上的绝对本地代码目录')
    if '..' in PurePosixPath(value).parts:
        raise ValueError('代码目录不能包含上级路径跳转')
    return str(PurePosixPath(value))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class ResearchBudget(StrictModel):
    minutes: int = Field(default=45, ge=1, le=1440)
    requests: int = Field(default=300, ge=1, le=100000)
    max_cost_usd: float = Field(default=2, gt=0, le=1000, allow_inf_nan=False)
    max_steps: int = Field(default=20, ge=1, le=10000)


class CreateResearch(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=12000)
    url: str | None = None
    repo: str | None = None
    authorization_confirmed: bool = False
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    _url = field_validator('url')(normalize_url)
    _repo = field_validator('repo')(normalize_repo)

    @model_validator(mode='after')
    def material_required(self):
        if not self.url and not self.repo:
            raise ValueError('至少提供网站地址或代码目录')
        return self


class ResearchHint(StrictModel):
    content: str = Field(min_length=1, max_length=12000)


class ResearchMaterials(StrictModel):
    url: str | None = None
    repo: str | None = None
    authorization_confirmed: bool = False
    _url = field_validator('url')(normalize_url)
    _repo = field_validator('repo')(normalize_repo)


class BudgetChanges(StrictModel):
    minutes: int | None = Field(default=None, ge=1, le=1440)
    requests: int | None = Field(default=None, ge=1, le=100000)
    max_cost_usd: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)
    max_steps: int | None = Field(default=None, ge=1, le=10000)

    @model_validator(mode='after')
    def require_values(self):
        if not self.model_fields_set or any(getattr(self,key) is None for key in self.model_fields_set):
            raise ValueError('至少提供一个有效预算字段')
        return self


class UpdateResearchBudget(StrictModel):
    budget: BudgetChanges
    authorization_confirmed: bool = False
