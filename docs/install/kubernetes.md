# Install on Kubernetes

`deploy/k8s/` in the repository holds plain manifests: a Deployment with a PersistentVolumeClaim mounted at `/data`, a Service on port 8765, an Ingress, and an example Secret with the settings. The Secret's example keeps the vaults in an S3 bucket; see [Storage](storage.md).

1. Copy the example Secret and fill it in. Make the master key with `mindin master-key generate`, or with `docker run --rm ghcr.io/gronare/mindin:latest master-key generate`.

   ```sh
   cp deploy/k8s/secret.example.yaml secret.yaml
   $EDITOR secret.yaml
   ```

   It needs `VAULT_PUBLIC_URL`, `VAULT_HOSTED_MASTER_KEY`, a sign-in method and the storage. [Configuration](../reference/configuration.md) lists every setting.

2. In `deploy/k8s/ingress.yaml`, set `ingressClassName`, the host, and `secretName` to a TLS certificate for that host.

3. Apply the manifests and invite yourself:

   ```sh
   kubectl apply -f secret.yaml -f deploy/k8s/deployment.yaml -f deploy/k8s/service.yaml -f deploy/k8s/ingress.yaml
   kubectl exec deploy/notes-vault-mcp -- mindin accounts invite you@example.com
   ```

4. Open `VAULT_PUBLIC_URL/account` and sign in.

## Notes

- The Deployment runs one pod with the `Recreate` strategy. Its probes hit `/.well-known/oauth-protected-resource/mcp`; with a path in `VAULT_PUBLIC_URL`, such as `/vault`, that is `/.well-known/oauth-protected-resource/vault/mcp`.
- The 512 MiB memory limit is enough without [search by meaning](semantic-search.md#kubernetes).
- Back up `/data/auth` together with the master key; see [Back up and rotate keys](../how-to/back-up-and-rotate.md).
- An Ingress controller that rewrites the `Host` header needs [`VAULT_ALLOWED_HOSTS`](../how-to/reverse-proxy.md).
