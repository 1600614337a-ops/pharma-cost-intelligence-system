"""Start the local review workbench."""

from __future__ import annotations

import argparse
import json

import uvicorn

from .config import DeploymentConfigurationError, DeploymentSettings
from .execution import GovernedTestExecutor
from .identity import SignedProxyIdentityProvider
from .web import create_review_app


def main() -> int:
    parser = argparse.ArgumentParser(description="启动成本分析任务审核工作台")
    parser.add_argument("--database", default=None)
    parser.add_argument(
        "--candidate",
        action="append",
        default=["08_RPA任务输出/2026-05_银黄口服液_RPA任务候选.json"],
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--preflight", action="store_true", help="只执行部署前检查，不启动服务")
    args = parser.parse_args()
    try:
        settings = DeploymentSettings.from_environment(database_path=args.database)
    except (DeploymentConfigurationError, ValueError) as exc:
        parser.error(str(exc))
    readiness = settings.readiness()
    if args.preflight:
        print(json.dumps(readiness.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0 if readiness.ready else 2
    if not readiness.ready:
        blocked = "；".join(item.message for item in readiness.checks if item.status == "block")
        parser.error(f"部署前检查未通过：{blocked}")
    test_executor = None
    if settings.test_submit_enabled:
        test_executor = GovernedTestExecutor(settings.test_submission_settings())
    identity_provider = None
    admin_token = None
    if settings.auth_mode == "local_token":
        admin_token = settings.admin_token.get_secret_value() if settings.admin_token else None
    else:
        identity_provider = SignedProxyIdentityProvider(
            signing_secret=settings.identity_signing_secret.get_secret_value(),  # type: ignore[union-attr]
            trusted_proxy_networks=settings.trusted_proxy_networks,
            max_age_seconds=settings.identity_max_age_seconds,
        )
    app = create_review_app(
        database_path=settings.database_path,
        admin_token=admin_token,
        candidate_files=args.candidate,
        test_executor=test_executor,
        identity_provider=identity_provider,
        deployment_settings=settings,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
