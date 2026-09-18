# Local API mock tools

Start the optional mock service on the existing `infra_default` network:

```sh
docker compose -f compose.office-api-mock.yml up -d --build
```

The source defaults to the sibling `office/api-mock` repository; set
`OFFICE_API_MOCK_SOURCE_DIR` to an absolute path from an infra worktree.
The persistent SQLite volume uses `SEED_MODE=if-empty`.

- Host OpenAPI: `http://127.0.0.1:18080/openapi.json`
- Worker origin: `http://office-api-mock.test:8080`
- The local worker config opts this exact origin into private-network API
  access. Other origins, loopback and metadata addresses remain blocked.
- API tool `{parameter}` path placeholders consume tool arguments. Existing
  `{{variable}}` placeholders continue to use session variables.

Register business operations as API tools from the running OpenAPI schema.
Use `authMethod=none` for these mock endpoints and publish the tool versions
before selecting them in an agent. Health and `/admin/*` maintenance endpoints
must not be part of the business tool catalogue.
