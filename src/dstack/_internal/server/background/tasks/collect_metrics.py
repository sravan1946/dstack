import asyncio
from typing import Dict, List

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from dstack._internal.core.models.runs import JobStatus
from dstack._internal.server.db import get_session_ctx
from dstack._internal.server.models import JobMetricsPoint, JobModel
from dstack._internal.server.schemas.runner import MetricsResponse
from dstack._internal.server.services.jobs import get_job_provisioning_data
from dstack._internal.server.services.runner import client
from dstack._internal.server.services.runner.ssh import runner_ssh_tunnel
from dstack._internal.server.utils.common import run_async
from dstack._internal.utils.common import batched
from dstack._internal.utils.logging import get_logger

logger = get_logger(__name__)


MAX_JOBS_FETCHED = 100
BATCH_SIZE = 10


async def collect_metrics():
    async with get_session_ctx() as session:
        res = await session.execute(
            select(JobModel)
            .where(
                JobModel.status.in_([JobStatus.RUNNING]),
            )
            .options(selectinload(JobModel.project))
            .order_by(JobModel.last_processed_at.asc())
            .limit(MAX_JOBS_FETCHED)
        )
        job_models = res.scalars().all()

    for batch in batched(job_models, BATCH_SIZE):
        await _collect_jobs_metrics(batch)


async def _collect_jobs_metrics(job_models: List[JobModel]):
    tasks = []
    for job_model in job_models:
        tasks.append(_collect_job_metrics(job_model))
    await asyncio.gather(*tasks)


async def _collect_job_metrics(job_model: JobModel):
    jpd = get_job_provisioning_data(job_model)
    if jpd is None:
        return
    try:
        res = await run_async(
            _pull_runner_metrics,
            job_model.project.ssh_private_key,
            jpd,
        )
    except Exception:
        logger.exception("Failed to collect job %s metrics", job_model.job_num)
        return
    if isinstance(res, bool):
        logger.error("Failed to connect to job %s to collect metrics", job_model.job_num)
        return
    async with get_session_ctx() as session:
        point = JobMetricsPoint(
            job_id=job_model.id,
            timestamp_micro=res.timestamp_micro,
            cpu_usage_micro=res.cpu_usage_micro,
            memory_usage_bytes=res.memory_usage_bytes,
            memory_working_set_bytes=res.memory_working_set_bytes,
        )
        session.add(point)
        await session.commit()


@runner_ssh_tunnel(ports=[client.REMOTE_RUNNER_PORT], retries=1)
def _pull_runner_metrics(
    ports: Dict[int, int],
) -> MetricsResponse:
    runner_client = client.RunnerClient(port=ports[client.REMOTE_RUNNER_PORT])
    return runner_client.get_metrics()
