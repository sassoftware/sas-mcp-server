from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Any

import httpx

from sas_mcp_server.config import VIYA_ENDPOINT


class ML_DEPLOYMENT_ACTION(StrEnum):
    """Enum for ML deployment actions."""

    REGISTER = auto()
    PUBLISH = auto()


@dataclass
class MLRegisterProps:
    """Properties for ML deployment."""

    project_id: str
    _action: ML_DEPLOYMENT_ACTION = ML_DEPLOYMENT_ACTION.REGISTER


@dataclass
class MLPublishProps:
    """Properties for ML deployment."""

    project_id: str
    destination_name: str
    _action: ML_DEPLOYMENT_ACTION = ML_DEPLOYMENT_ACTION.PUBLISH


async def ml_register_publish(
    props: MLRegisterProps | MLPublishProps,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Deploy the champion model for automated machine learning project."""
    url = f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{props.project_id}/models/@championModel"
    params = httpx.QueryParams({"action": props._action.value})
    if isinstance(props, MLPublishProps):
        params = params.merge({"destinationName": props.destination_name})
    headers = httpx.Headers(
        {
            "Accept": (
                "application/json, "
                "application/vnd.sas.analytics.ml.pipeline.automation.project.champion.model.action.response+json, "
                "application/vnd.sas.error+json"
            ),
        }
    )
    resp = await client.put(
        url,
        params=params,
        headers=headers,
    )
    resp.raise_for_status()
    return resp.json()
