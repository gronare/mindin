# Contributing

Bug reports and pull requests are welcome. For a security problem, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Run the checks

```console
$ uv sync
$ uv run ruff check .
$ uv run ruff format --check .
$ uv run pytest -q
```

## Sign off your commits

A contribution is made under the project's [MIT licence](LICENSE). Every commit in a pull request carries a sign-off, which certifies the [Developer Certificate of Origin](https://developercertificate.org): that you wrote the change or have the right to submit it under that licence.

```console
$ git commit -s
```

`-s` adds a `Signed-off-by:` line with the name and email of your git identity. It has to match the commit's author; a check on the pull request fails otherwise. To sign off commits you already made, run `git rebase --signoff main` and push again.
