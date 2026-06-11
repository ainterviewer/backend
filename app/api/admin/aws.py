import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from uvicorn.config import logger

from ainterviewer.settings import settings as lib_settings

from ...dependencies import ScopeChecker
from ...types import Scope

router = APIRouter(
    prefix="/aws/ec2",
    dependencies=[
        Depends(ScopeChecker(Scope.ADMIN)),
    ],
)


async def proxy_to_ec2_manager(request: Request, full_path: str):
    """
    Proxy all /api/aws/ec2/* requests to the EC2 manager proxy server.
    This allows admin users to control EC2 instances from the main app.
    """
    target_url = f"{lib_settings.llm.llm_endpoint}/api/aws/ec2/{full_path}"

    logger.info(
        f"Proxying EC2 request: {request.method} {request.url.path} -> {target_url}"
    )

    try:
        # Get request body and headers
        body = await request.body()
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "connection")
        }

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
        ) as client:
            response = await client.request(
                method=request.method,
                url=target_url,
                params=request.query_params,
                headers=headers,
                content=body,
            )

            # Forward the response
            response_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower()
                not in (
                    "content-encoding",
                    "transfer-encoding",
                    "connection",
                )
            }

            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=response_headers,
                media_type=response.headers.get("content-type"),
            )

    except httpx.TimeoutException:
        logger.error(f"Timeout connecting to EC2 proxy: {target_url}")
        raise HTTPException(
            status_code=504,
            detail="Timeout connecting to EC2 manager service",
        )
    except httpx.RequestError as e:
        logger.error(f"Failed to connect to EC2 proxy: {repr(e)}")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach EC2 manager service: {str(e)}",
        )


# Register one route per method so each gets a unique operationId in the
# OpenAPI schema (a single multi-method route would emit duplicates).
for _method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
    router.add_api_route(
        "/{full_path:path}",
        proxy_to_ec2_manager,
        methods=[_method],
        name=f"proxy_to_ec2_manager_{_method.lower()}",
    )
