# dataset: databases for lazy people

![build](https://github.com/pudo/dataset/workflows/build/badge.svg)

In short, **dataset** makes reading and writing data in databases as simple as reading and writing JSON files.

> **Note — this is a hard fork of [pudo/dataset](https://github.com/pudo/dataset).**
> The `import dataset` name is unchanged, but the 3.0 line breaks away from
> upstream with a redesigned API (five write verbs, one `auto_create` flag) and
> is not a drop-in upgrade. The distribution/package name for the fork is TBD;
> see `CHANGELOG.md` under 3.0.0 for the full list of breaking changes.

[Read the docs](https://dataset.readthedocs.io/)

To install dataset, fetch it with ``pip``:

```bash
$ pip install dataset
```

**Note:** as of version 1.0, **dataset** is split into two packages, with the
data export features now extracted into a stand-alone package, **datafreeze**.
See the relevant repository [here](https://github.com/pudo/datafreeze).
