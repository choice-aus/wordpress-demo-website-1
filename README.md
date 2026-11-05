# wordpress-demo-website-1

### Startup flow for this template ###

```bash
composer install
docker compose build
docker compose up -d
```

### Restart cleanly: ### 
```bash
docker compose down
docker compose up -d --build
```

### Sanity Checklist ###

After everything is up:

Visit:
👉 https://mysite1.test:4441/

phpMyAdmin:
👉 http://localhost:8082

Mailhog:
👉 http://localhost:8025

---

## Git Branching Strategy

### Branches

| Branch | Purpose |
|---|---|
| `main` | Trusted, production-ready code |
| `develop` | Sandbox for experimentation and early integration testing |
| `staging` | Shared sprint integration and testing |
| `release-*` | Temporary release branches for approved features |
| `feature/*` | Individual development tasks |

### Promotion workflow

1. Create feature branches from `main`.
2. Merge feature branches into `staging` as work progresses. Pull requests are optional.
3. Once a feature is approved, create a pull request from `feature/*` into the current `release-*` branch.
4. Use **Squash and merge** when merging features into a release branch.
5. Test the completed release.
6. Create a pull request from `release-*` into `main`.
7. Use **Create a merge commit** when promoting a release into `main`.

### Promotion restrictions

- Only `feature/*` branches may merge into `release-*`.
- Only `release-*` branches may merge into `main`.
- `staging` must never be promoted directly into `release-*` or `main`.
- Failed or unapproved features must not be included in a release.

### Merge methods

| Destination | Merge method |
|---|---|
| `staging` | Merge commit (PR optional) |
| `release-*` | Squash and merge |
| `main` | Merge commit |

