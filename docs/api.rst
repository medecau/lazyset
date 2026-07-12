
API documentation
=================

Connecting
----------

.. autofunction:: dataset.connect

Database
--------

.. autoclass:: dataset.Database
   :members: tables, views, table, query, begin, commit, rollback, close
   :special-members: __getitem__, __contains__


Table
-----

.. autoclass:: dataset.Table
   :members: exists, columns, find, find_one, count, distinct, insert, insert_ignore, update, upsert, delete, create_column, create_column_by_example, drop_column, create_index, drop, has_column, has_index
   :special-members: __len__, __iter__


Types
-----

Column types for ``primary_type`` and
:py:meth:`create_column <dataset.Table.create_column>` are exposed as
attributes on ``db.types`` (an instance of :py:class:`Types
<dataset.types.Types>`), e.g. ``db.types.text`` or ``db.types.string(255)``.

.. autoclass:: dataset.types.Types
   :members:


Data Export
-----------

  **Note:** Data exporting has been extracted into a stand-alone package, datafreeze. See the relevant repository here_.

.. _here: https://github.com/pudo/datafreeze

