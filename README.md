# ChitoProject

A private Telegram and Bale platform for receiving, tracking, and coordinating student projects. The current customer intake begins with a short discipline choice and exposes only the active Computer option.

The platform includes a customer intake flow, project tracking, a role-based collaboration team, a Persian RTL administration panel, and an archived conversation history. The customer-facing bot keeps the chat compact through edited control messages and inline actions.

- **Persian overview and setup:** [README_FA.md](README_FA.md)
- **VPS installation and deployment:** [docs/DEPLOYMENT_FA.md](docs/DEPLOYMENT_FA.md)
- **Admin roles and workflows:** [docs/ADMIN_GUIDE_FA.md](docs/ADMIN_GUIDE_FA.md)
- **Security model and operational notes:** [docs/SECURITY_FA.md](docs/SECURITY_FA.md)
- **Legacy source audit:** [docs/LEGACY_AUDIT_FA.md](docs/LEGACY_AUDIT_FA.md)
- **Contributor guide:** [CONTRIBUTING_FA.md](CONTRIBUTING_FA.md)

## Stack

Python 3.12.16, FastAPI, SQLAlchemy async, PostgreSQL, Alembic, and server-rendered HTML/CSS/JavaScript. Telegram and Bale use separate adapters over a shared application and database. VPS Python is managed by uv under `/opt/chitoproject-python`; the system Python is untouched.

## Quick start

For development setup, database initialization, and bot configuration, follow the [Persian README](README_FA.md). For a fresh Ubuntu VPS, follow the [deployment guide](docs/DEPLOYMENT_FA.md). Do not commit `.env` files, bot tokens, database credentials, customer files, or conversation data.
