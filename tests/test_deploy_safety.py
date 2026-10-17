"""部署安全守卫。

这一组测试盯的是同一类问题：**代码在开发机上跑得好好的，部署出去却带着一套
公开示例的安全配置**。这类缺陷不会让服务崩溃、不会让测试变红，
所以它不会被任何"功能测通了"的诊断发现 —— 只有被人利用的那天才暴露。

三件被拦住的事：

1. 容器编排不许再引用项目根的开发 ``.env``（那里是 DEBUG=true +
   出厂密钥 + admin123），只能用 ``deploy/.env``。
2. ``SECRET_KEY`` / ``ADMIN_PASSWORD`` 必须是 Compose 的**必需变量**
   （``${VAR:?提示}``）—— 没配就让 up 失败，而不是静默回落到默认值。
3. ``_exposed_environment()`` 的判据不能退回"只看 ENV"。
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.app.config import settings
from backend.app.main import _exposed_environment, _INITIAL_SECRET

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
DEPLOY_ENV_EXAMPLE = ROOT / "deploy" / ".env.example"


def _compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


# ============================================================ 1. 不再挂根 .env
def test_compose_does_not_mount_project_root_env() -> None:
    """compose 只能用 deploy/.env，不许再引用项目根那份开发配置。"""
    text = _compose_text()
    assert "env_file" in text, "编排里应该有 env_file（否则这条断言失去意义）"
    bad = re.findall(r"env_file:\s*\n\s*-+\s*(\S+)", text)
    assert bad, "没解析到任何 env_file 条目 —— 断言写错了，别让它静默通过"
    for path in bad:
        assert "../.env" not in path, (
            f"编排引用了 {path}。那是本机开发配置：DEBUG=true、"
            f"SECRET_KEY={_INITIAL_SECRET}、ADMIN_PASSWORD=admin123，"
            "整份挂进容器等于把示例值当生产配置用。"
        )
        assert path.strip().startswith("./") or path.startswith(".env"), (
            f"env_file 指向了 {path}，请改为 ./（deploy 目录下）"
        )


# ============================================================ 2. 必需变量语法
def test_secret_and_admin_password_are_required_vars() -> None:
    """密钥与管理员口令缺失时必须**启动失败**，而不是回落到默认值。"""
    text = _compose_text()
    for key in ("SECRET_KEY", "ADMIN_PASSWORD"):
        pattern = rf"\${{{key}:\?"
        assert re.search(pattern, text), (
            f"compose 里找不到 {key} 的必需变量语法（${{{key}:?提示}}）。"
            "写成 ${KEY:-默认值} 的效果是：部署的人忘了配，容器照样起来，"
            "只是用着一份公开已知的密钥 —— 而且没有任何报错。"
        )


def test_deploy_env_template_has_no_real_secrets() -> None:
    """模板里敏感项必须是**空的**，否则复制过去就用上了示例值。"""
    assert DEPLOY_ENV_EXAMPLE.exists(), (
        f"缺少 {DEPLOY_ENV_EXAMPLE} —— 没有它，部署的人只能去抄根目录那份开发配置"
    )
    text = DEPLOY_ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in ("SECRET_KEY", "ADMIN_PASSWORD", "DEMO_LAWYER_PASSWORD"):
        m = re.search(rf"^{key}=(.*)$", text, flags=re.M)
        assert m, f"模板里缺 {key} 这一项"
        assert m.group(1).strip() == "", (
            f"{key} 必须留空由运维填写。填了值（哪怕只是示例）的下场是："
            "绝大多数人会直接 cp 完就 up，那个示例值从此进入生产。"
        )


# ============================================================ 3. 容器判据
def _probe(monkeypatch, *, dockerenv: bool, cgroup: str | None, env: str) -> bool:
    """在**不真的改动文件系统**的前提下，把 `_exposed_environment` 跑一遍。"""
    from backend.app import main

    class _FakePath:
        """Path 替身。``read_text`` 抛 OSError 代表读不到 /proc（宿主机常见）。"""

        def __init__(self, target: str) -> None:
            self._target = str(target)

        def exists(self) -> bool:
            return dockerenv and self._target == "/.dockerenv"

        def read_text(self, *_a: object, **_k: object) -> str:
            if cgroup is None:
                raise OSError("没有 /proc/1/cgroup")
            return cgroup

    monkeypatch.setattr(main, "Path", _FakePath)
    monkeypatch.setattr(settings, "ENV", env, raising=False)
    return _exposed_environment()


def test_factory_key_warns_inside_container(monkeypatch) -> None:
    """容器里用出厂密钥必须告警 —— **即使 ENV 写的是 dev**。

    这是本组测试存在的理由：判据最早是 ``ENV == "prod"``，
    而容器化部署时没人会去改 .env 里的 ENV（部署往往是直接 cp 那份文件）。
    于是正好满足了告警不触发的条件。
    """
    probed = _probe(monkeypatch, dockerenv=True, cgroup="1:name=docker:/abc", env="dev")
    assert probed, (
        "容器里（有 /.dockerenv）却认为不安全不存在。"
        "一旦判据退回 ENV==\"prod\"，出厂密钥就能悄无声息地进生产。"
    )


def test_cgroup_marker_counts_without_dockerenv(monkeypatch) -> None:
    """没有 /.dockerenv 时，cgroup 里的 docker/kubepods 也算容器。"""
    for marker in ("12:pids:/docker/abc", "11:cpuset:/kubepods/pod123",
                   "0::/system.slice/containerd.service"):
        probed = _probe(monkeypatch, dockerenv=False, cgroup=marker, env="dev")
        assert probed, f"cgroup 含 {marker!r} 应判定为容器内"


def test_plain_host_without_env_prod_is_not_flagged(monkeypatch) -> None:
    """**反向用例**：本机开发不该被这条告警刷屏。

    只测正向会把判据放宽成 `return True` 也全绿（那样每次本地启服都会告警，
    告警刷屏的结果就是没人再看告警）。
    """
    probed = _probe(monkeypatch, dockerenv=False, cgroup=None, env="dev")
    assert not probed, "普通开发机（ENV=dev）被误判为需要生产级密钥"


def test_env_prod_alone_is_enough(monkeypatch) -> None:
    """ENV=prod 本身就是 Green；不依赖它在不在容器里。"""
    probed = _probe(monkeypatch, dockerenv=False, cgroup=None, env="prod")
    assert probed, "ENV=prod 必须触发告警，否则这条校验在非容器部署下形同虚设"
