# app-translator

Import an app you already have. Paste a link to a `fly.toml`, get back a link that
installs the app on your compute space.

## What it does

1. Fetches the config (a raw URL, a GitHub blob link, or a repo that contains a `fly.toml`).
2. Translates everything that maps onto an OpenHost manifest, and inspects the base image's
   metadata to fill in what the config leaves out (entrypoint, listening port, the user it
   runs as, its own data-directory settings).
3. Shows you a prefilled form with every assumption, substitution and dropped field called out.
4. Generates a git repo — `openhost.toml`, a `Dockerfile`, a startup script, the original
   config, and `TRANSLATION_NOTES.md` — publishes it, and hands back an install link.

## Repo hosting

Generated repos need somewhere the compute space can clone them from. Two backends:

- **self-served** (default, no credentials): the repo is written into this app's own data
  directory and served over git's dumb HTTP protocol under `/git/`, which the manifest marks
  as a public path so the router can clone it.
- **forgejo**: set `FORGEJO_BASE_URL` and `FORGEJO_TOKEN` and generated repos are pushed to
  that Forgejo/Gitea instance instead.

## Secrets

Values the importer thinks are credentials (by name, or because you ticked the box) are not
baked into the image. They become a grant on the compute space's `secrets` service, and the
generated startup script fetches them all in one batched call at boot, exporting them before
exec'ing the app. If one is missing the app exits with a message saying which.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest

# running it outside a compute space needs the env vars the platform normally injects:
OPENHOST_APP_NAME=app-translator OPENHOST_ZONE_DOMAIN=example.com \
  OPENHOST_APP_DATA_DIR=/tmp/at-data OPENHOST_ROUTER_URL=http://127.0.0.1:9999 \
  OPENHOST_APP_TOKEN=dev PORT=8099 \
  .venv/bin/hypercorn --bind 127.0.0.1:8099 'app_translator.web.app:create_app()'
```

## Status

First pass: `fly.toml` only, single-service only. The intermediate representation is shared,
so other formats are a parser each — see the mapping docs in the `openhost` repo under
`docs/config_translation/`.
