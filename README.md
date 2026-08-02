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

`vem create --python PYTHON` accepts either an executable path or a command name
found on `PATH`. On Windows, the `.exe` suffix may be omitted. As a shorthand,
`vem create -v 3.11 .venv` (or `--ver 3.11`) is equivalent to
`vem create --python python3.11 .venv`.

Use `--house PATH` or `VEM_HOUSE` to select a separate house. The default is the
platform application-data directory. Run `vem COMMAND --help` for all options.
