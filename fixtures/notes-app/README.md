# Notes service

A small Node/SQLite notes service used as a controlled, synthetic test application.
It is not customer data, an existing user deployment, or the FirstRun product.
Requires a Node 22.16+ runtime with the built-in SQLite module.

## Local setup

<!-- firstrun:setup:start -->
```bash
npm ci --offline --no-audit --no-fund
npm run dev
```
<!-- firstrun:setup:end -->

The service exposes `GET /health`, `POST /notes` with a JSON `message`, and
`GET /notes/<id>`. The default development port is 3000.
