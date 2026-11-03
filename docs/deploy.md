# Production deploy (runbook)

Stand: Yandex Cloud Compute VM, Ubuntu 22.04, static IP (see `SSH_HOST` secret).

```
Internet ──80/443──▶ Nginx (host) ──▶ 127.0.0.1:8000 api (docker) ──▶ postgres (docker network only)
```

## Layout on the VM

| Path | What |
|---|---|
| `/opt/awe` | git checkout of `main` |
| `/opt/awe/.env` | production secrets, `chmod 600`, **not in git** |
| `/etc/nginx/sites-available/awe` | reverse proxy to `127.0.0.1:8000` |
| `~/.ssh/gh_actions_deploy` | key used by GitHub Actions (restricted in `authorized_keys`) |

Firewall: Yandex security group + UFW allow only 22, 80, 443. Postgres has no published port.

## Automatic deploy

`.github/workflows/deploy.yml` runs on every push to `main` (and manually via *Run workflow*):

1. SSH to the VM with the deploy key, host key pinned via `SSH_KNOWN_HOSTS`.
2. `git reset --hard <sha>` in `/opt/awe`.
3. `docker compose up -d --build` with `docker-compose.yml` + `docker-compose.prod.yml`.
4. Wait for `GET /health` on the VM, then `docker image prune -f` (disk is small).
5. Public smoke test `http://<SSH_HOST>/health` through Nginx.

Migrations run on container start (`alembic upgrade head`), so they must stay backward compatible with the previous release.

## Manual operations

```bash
ssh ubuntu@<SSH_HOST>
cd /opt/awe
export COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml

docker compose ps
docker compose logs -f --tail 100 api
docker compose up -d --build        # redeploy current checkout
```

Rollback to a previous commit:

```bash
git reset --hard <good-sha> && docker compose up -d --build
```

Database backup:

```bash
docker compose exec -T postgres pg_dump -U postgres awe | gzip > ~/awe-$(date +%F).sql.gz
```

## Secrets

`.env` keys required by `docker-compose.prod.yml`: `POSTGRES_PASSWORD`, `DATABASE_URL`, `JWT_SECRET_KEY`, `FERNET_KEY`.
Losing `FERNET_KEY` makes stored gift card codes unreadable — keep it in the team password manager.
