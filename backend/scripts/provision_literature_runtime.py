"""Administrator-only provisioning; writes private runtime env files, never prints secrets."""

import asyncio
import os
import re
import secrets
from pathlib import Path

import asyncpg
from dotenv import dotenv_values
from sqlalchemy.engine import make_url


async def provision():
    source = {k: v for k, v in dotenv_values(os.getenv("YUXI_PROVISION_ENV", ".env")).items() if v is not None}
    admin_url = make_url(os.environ.get("POSTGRES_URL") or source["POSTGRES_URL"])
    output = Path(os.getenv("YUXI_RUNTIME_ENV_OUTPUT", "/tmp/yuxi-runtime"))
    output.mkdir(parents=True, exist_ok=True)
    connection = await asyncpg.connect(admin_url.set(drivername="postgresql").render_as_string(hide_password=False))
    runtime_urls = {}
    try:
        async with connection.transaction():
            if not await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version='0116_literature_production_security')"
            ):
                raise RuntimeError("Apply administrator migrations first")
            for name, group, filename in (
                ("yuxi_api_login", "yuxi_api_runtime", "api.env"),
                ("yuxi_pdf_login", "yuxi_pdf_runtime", "pdf.env"),
            ):
                file = output / filename
                existing = dotenv_values(file) if file.exists() else {}
                previous = make_url(existing["POSTGRES_URL"]) if existing.get("POSTGRES_URL") else None
                password = previous.password if previous and previous.username == name else secrets.token_hex(32)
                if not re.fullmatch(r"[a-f0-9]{64}", password or ""):
                    raise RuntimeError("Unexpected runtime credential format")
                unsafe = await connection.fetchval(
                    "SELECT rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication "
                    "FROM pg_roles WHERE rolname=$1",
                    name,
                )
                if unsafe:
                    raise RuntimeError("Refuse existing privileged login")
                if unsafe is None:
                    await connection.execute(
                        f"CREATE ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                    )
                await connection.execute(f"ALTER ROLE {name} PASSWORD '{password}'")
                await connection.execute(f"GRANT {group} TO {name}")
                values = {
                    k: v
                    for k, v in source.items()
                    if not k.startswith("POSTGRES_")
                    and k != "YUXI_SECRET_MASTER_KEY"
                    and not k.endswith("MIGRATION_URL")
                    and not k.endswith("SCHEDULER_DATABASE_URL")
                }
                if filename == "pdf.env":
                    values = {
                        k: v
                        for k, v in values.items()
                        if k.startswith(("MINIO_", "SUPER_TRANSLATE_", "REDIS_"))
                        or k in ("RUNNING_IN_DOCKER", "SAVES_DIR", "NO_PROXY", "no_proxy")
                    }
                values.update(
                    {
                        "POSTGRES_URL": admin_url.set(username=name, password=password).render_as_string(
                            hide_password=False
                        ),
                        "YUXI_DB_SKIP_MIGRATIONS": "true",
                        "YUXI_ENV_FILE": "/app/.env",
                        "RUNNING_IN_DOCKER": "true",
                    }
                )
                variable = "YUXI_API_RUNTIME_URL" if filename == "api.env" else "YUXI_PDF_RUNTIME_URL"
                runtime_urls[variable] = values["POSTGRES_URL"]
                content = "".join(
                    k + "='" + v.replace("\\", "\\\\").replace("'", "\\'") + "'\n" for k, v in sorted(values.items())
                )
                file.write_text(content, encoding="utf-8")
                file.chmod(0o600)
    finally:
        await connection.close()
    compose_file = output / "compose.env"
    compose_file.write_text("".join(k + "='" + v + "'\n" for k, v in runtime_urls.items()), encoding="utf-8")
    compose_file.chmod(0o600)
    print("Restricted API/PDF logins provisioned; private runtime files written")


if __name__ == "__main__":
    try:
        asyncio.run(provision())
    except Exception as error:
        raise SystemExit(f"Runtime provisioning failed ({type(error).__name__}); no credentials logged") from None
