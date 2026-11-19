"""Start the background worker with python -m backend.worker."""

import asyncio

from backend.config.settings import Settings
from backend.scripts.common.logging_config import configure_logging
from backend.scripts.core.services.pipeline import PipelineWorker
from backend.scripts.db.connections import connect_service


async def main():
    configure_logging()
    settings = Settings()
    async with connect_service(settings) as service:
        worker = PipelineWorker(service, settings)
        async with asyncio.TaskGroup() as group:
            for worker_number in range(settings.worker_concurrency):
                group.create_task(worker.run_forever(), name=f"worker-{worker_number}")


if __name__ == "__main__":
    asyncio.run(main())
