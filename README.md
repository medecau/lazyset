# dataset: databases for lazy people

![build](https://github.com/pudo/dataset/workflows/build/badge.svg)

In short, **dataset** makes reading and writing data in databases as simple as reading and writing JSON files.

> **Note — this is a hard fork of [pudo/dataset](https://github.com/pudo/dataset).**
> It ships as **`lazyset`** on PyPI, but the `import dataset` name is unchanged.
> The redesigned API (five write verbs, one `auto_create` flag) breaks away from
> upstream and is not a drop-in upgrade; see `CHANGELOG.md` under 0.1.0 for the
> full list of breaking changes.

[Read the docs](https://dataset.readthedocs.io/)

To install lazyset, fetch it with ``pip``:

```bash
$ pip install lazyset
```

**Note:** as of version 1.0, **dataset** is split into two packages, with the
data export features now extracted into a stand-alone package, **datafreeze**.
See the relevant repository [here](https://github.com/pudo/datafreeze).
