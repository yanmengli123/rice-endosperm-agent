"""One PDF orchestration slot; queue waiting never occupies chat workers."""

from arq import cron

from yuxi.literature.pdf_translation import QUEUE, process_pdf_translation, reconcile_pdf_translations
from yuxi.storage.redis import get_arq_redis_settings
from yuxi.storage.postgres.manager import pg_manager


async def startup(ctx):
    pg_manager.initialize()
    await pg_manager.create_tables()
    await pg_manager.ensure_business_schema()
    await reconcile_pdf_translations(ctx)


async def shutdown(ctx):
    await pg_manager.close()


class WorkerSettings:
    functions = [process_pdf_translation]
    cron_jobs = []
    queue_name = QUEUE
    redis_settings = get_arq_redis_settings()
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 1
    job_timeout = 3600
    max_tries = 1
    health_check_key = "literature:pdf-worker:health"
    health_check_interval = 30


class MaintenanceSettings:
    functions = []
    cron_jobs = [cron(reconcile_pdf_translations, minute=set(range(60)), run_at_startup=True)]
    queue_name = "arq:literature-pdf-maintenance"
    redis_settings = get_arq_redis_settings()
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 1
    job_timeout = 120
    max_tries = 1
    health_check_key = "literature:pdf-maintenance:health"
    health_check_interval = 30
