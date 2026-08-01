# vem

`vem` stores Python virtual environments in a centralized per-user **house** and
places links to them in project directories. Environments never move; links can
be added, moved, and removed independently.

```console
pip install .
vem create --name my-project .venv
vem status
vem link --name my-project ../other/.venv
vem doctor
```

Use `--house PATH` or `VEM_HOUSE` to select a separate house. The default is the
platform application-data directory. Run `vem COMMAND --help` for all options.

