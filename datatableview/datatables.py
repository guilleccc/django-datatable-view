import re
import copy
from collections import OrderedDict

from django.db import models
from django.forms.util import flatatt
from django.template.loader import render_to_string
try:
    from django.utils.encoding import python_2_unicode_compatible
except ImportError:
    from .compat import python_2_unicode_compatible

import six

from .exceptions import ColumnError
from . import columns
from .utils import (normalize_config, apply_options, get_field_definition, ColumnInfoTuple,
                    ColumnOrderingTuple)

COLUMN_TYPES = {
    columns.TextColumn: [models.CharField, models.TextField, models.FileField],
    columns.DateColumn: [models.DateField],
    columns.BooleanColumn: [models.BooleanField, models.NullBooleanField],
    columns.IntegerColumn: [models.IntegerField, models.AutoField],
    columns.FloatColumn: [models.FloatField, models.DecimalField],

    # This is a special type for fields that should be passed up, since there is no intuitive
    # meaning for searches done agains the FK field directly.
    columns.ForeignKeyColumn: [models.ForeignKey],
}

def pretty_name(name):
    if not name:
        return ''
    return name[0].capitalize() + name[1:]

def get_column_for_modelfield(model_field):
    for ColumnClass, modelfield_classes in COLUMN_TYPES.items():
        if isinstance(model_field, tuple(modelfield_classes)):
            return ColumnClass

# Borrowed from the Django forms implementation 
def columns_for_model(model, fields=None, exclude=None):
    field_list = []
    opts = model._meta
    for f in sorted(opts.fields):
        if fields is not None and f.name not in fields:
            continue
        if exclude and f.name in exclude:
            continue

        column_class = get_column_for_modelfield(f)
        column = column_class(sources=[f.name], label=pretty_name(f.verbose_name))
        column.name = f.name
        field_list.append((f.name, column))

    field_dict = OrderedDict(field_list)
    if fields:
        field_dict = OrderedDict(
            [(f, field_dict.get(f)) for f in fields
                if ((not exclude) or (exclude and f not in exclude))]
        )
    return field_dict

# Borrowed from the Django forms implementation 
def get_declared_columns(bases, attrs, with_base_columns=True):
    """
    Create a list of form field instances from the passed in 'attrs', plus any
    similar fields on the base classes (in 'bases'). This is used by both the
    Form and ModelForm metclasses.

    If 'with_base_columns' is True, all fields from the bases are used.
    Otherwise, only fields in the 'declared_fields' attribute on the bases are
    used. The distinction is useful in ModelForm subclassing.
    Also integrates any additional media definitions
    """
    local_columns = [
        (column_name, attrs.pop(column_name)) \
                for column_name, obj in list(six.iteritems(attrs)) \
                if isinstance(obj, columns.Column)
    ]
    local_columns.sort(key=lambda x: x[1].creation_counter)

    # If this class is subclassing another Form, add that Form's columns.
    # Note that we loop over the bases in *reverse*. This is necessary in
    # order to preserve the correct order of columns.
    if with_base_columns:
        for base in bases[::-1]:
            if hasattr(base, 'base_columns'):
                local_columns = list(six.iteritems(base.base_columns)) + local_columns
    else:
        for base in bases[::-1]:
            if hasattr(base, 'declared_columns'):
                local_columns = list(six.iteritems(base.declared_columns)) + local_columns

    return OrderedDict(local_columns)

class DatatableOptions(object):
    def __init__(self, options=None):
        self.model = getattr(options, 'model', None)
        self.columns = getattr(options, 'columns', None)  # table headers
        self.exclude = getattr(options, 'exclude', None)
        self.ordering = getattr(options, 'ordering', None)  # override to Model._meta.ordering
        # self.start_offset = getattr(options, 'start_offset', None)  # results to skip ahead
        self.page_length = getattr(options, 'page_length', 25)  # length of a single result page
        self.search = getattr(options, 'search', None)  # client search string
        self.search_fields = getattr(options, 'search_fields', None)  # extra searchable ORM fields
        self.unsortable_columns = getattr(options, 'unsortable_columns', None)
        self.hidden_columns = getattr(options, 'hidden_columns', None)  # generated, but hidden
        self.structure_template = getattr(options, 'structure_template', "datatableview/default_structure.html")

        self.result_counter_id = getattr(options, 'result_counter_id', 'id_count')


class DatatableMetaclass(type):
    def __new__(cls, name, bases, attrs):
        declared_columns = get_declared_columns(bases, attrs, with_base_columns=False)
        new_class = super(DatatableMetaclass, cls).__new__(cls, name, bases, attrs)

        opts = new_class._meta = new_class.options_class(getattr(new_class, 'Meta', None))
        if opts.model:
            columns = columns_for_model(opts.model, opts.columns, opts.exclude)
            none_model_columns = [k for k, v in six.iteritems(columns) if not v]
            missing_columns = set(none_model_columns) - set(declared_columns.keys())

            if missing_columns:
                # TODO: Inspect for method handler, etc
                raise ColumnError("Unknown column name(s): %r" % (list(missing_columns),))

            for name, column in declared_columns.items():
                column.name = name
                if not column.sources:
                    column.sources = [name]
                if not column.label:
                    field, _, _, _ = opts.model._meta.get_field_by_name(name)
                    column.label = pretty_name(field.verbose_name)

            columns.update(declared_columns)
        else:
            columns = declared_columns

        new_class.declared_columns = declared_columns
        new_class.base_columns = columns
        return new_class


@python_2_unicode_compatible
class Datatable(six.with_metaclass(DatatableMetaclass)):
    options_class = DatatableOptions

    def __init__(self, object_list, url, view=None, callback_target=None, model=None,
                 query_config=None, **kwargs):
        self.object_list = object_list
        self.url = url
        self.view = view
        self.fallback_callback_target = callback_target
        self.model = self._meta.model or model
        if self.model is None and hasattr(object_list, 'model'):
            self.model = object_list.model
        self.columns = copy.deepcopy(self.base_columns)
        self.configure(self._meta.__dict__, kwargs, query_config)

        self.total_initial_record_count = None
        self.unpaged_record_count = None

    def configure(self, meta_config, view_config, query_config):
        declared_config = dict(meta_config, **view_config)
        self.config = normalize_config(declared_config, query_config, model=self.model)

        # Core options, not modifiable by client updates
        # if self.config.get('columns') is None:
        #     model_fields = self.model._meta.local_fields
        #     self.config['columns'] = list(map(lambda f: (six.text_type(f.verbose_name), f.name), model_fields))

        if self._meta.hidden_columns is None:
            self._meta.hidden_columns = []

        if self._meta.search_fields is None:
            self._meta.search_fields = []

        if self._meta.unsortable_columns is None:
            self._meta.unsortable_columns = []

        self._flat_column_names = []
        for column in self.columns:
            column = get_field_definition(column)
            flat_name = column.pretty_name
            if column.fields:
                flat_name = column.fields[0]
            self._flat_column_names.append(flat_name)

        self.ordering = {}
        if self.config['ordering']:
            for i, name in enumerate(self.config['ordering']):
                plain_name = name.lstrip('-+')
                index = self.get_column_index(plain_name)
                if index == -1:
                    continue
                sort_direction = 'desc' if name[0] == '-' else 'asc'
                self.ordering[plain_name] = ColumnOrderingTuple(i, index, sort_direction)

    # Data retrieval
    def get_column_index(self, name):
        if name.startswith('!'):
            return int(name[1:])
        try:
            return self._flat_column_names.index(name)
        except ValueError:
            return -1

    def _get_current_page(self):
        """
        If page_length is specified in the options or AJAX request, the result list is shortened to
        the correct offset and length.  Paged or not, the finalized object_list is then returned.
        """

        # Narrow the results to the appropriate page length for serialization
        if self.config['page_length'] != -1:
            i_begin = self.config['start_offset']
            i_end = self.config['start_offset'] + self.config['page_length']
            object_list = self._records[i_begin:i_end]

        return object_list

    def get_records(self):
        if not hasattr(self, '_records'):
            self.populate_records()

        return [self.get_record_data(obj) for obj in self._get_current_page()]

    def populate_records(self):
        self._records = apply_options(self.object_list, self)

    def preload_record_data(self, instance):
        """
        An empty hook for letting the view do something with ``instance`` before column lookups are
        called against the object. The tuple of items returned will be passed as keyword arguments
        to any of the ``get_column_FIELD_NAME_data()`` methods.

        """

        return {}

    def get_record_data(self, obj):
        """
        Returns a list of column data intended to be passed directly back to dataTables.js.

        Each column generates a 2-tuple of data. [0] is the data meant to be displayed to the client
        and [1] is the data in plain-text form, meant for manual searches.  One wouldn't want to
        include HTML in [1], for example.

        """

        data = {
            'pk': obj.pk,
            '_extra_data': {},  # TODO: callback structure for user access to this field
        }
        for i, (name, column) in enumerate(self.columns.items()):
            kwargs = self.preload_record_data(obj)
            kwargs.update({
                'datatable': self,
                'view': self.view,
            })
            value = column.value(obj, **kwargs)[1]
            processor = self._get_processor_method(i, column)
            if processor:
                value = processor(default_value=value)
            if isinstance(value, (tuple, list)):
                value = value[0]

            if six.PY2 and isinstance(value, str):  # not unicode
                value = value.decode('utf-8')
            data[str(i)] = six.text_type(value)
        return data

    def process_value(self, obj, **kwargs):
        return kwargs['default_value']

    def _get_processor_method(self, i, column):
        """
        Using a slightly mangled version of the column's name (explained below) each column's value
        is derived.

        Each field can generate customized data by defining a method on the view called either
        "get_column_FIELD_NAME_data" or "get_column_INDEX_data".

        If the FIELD_NAME approach is used, the name is the raw field name (e.g., "street_name") or
        else the friendly representation defined in a 2-tuple such as
        ("Street name", "subdivision__home__street_name"), where the name has non-alphanumeric
        characters stripped to single underscores.  For example, the friendly name
        "Region: Subdivision Type" would convert to "Region_Subdivision_Type", requiring the method
        name "get_column_Region_Subdivision_Type_data".

        Alternatively, if the INDEX approach is used, a method will be fetched called
        "get_column_0_data", or otherwise using the 0-based index of the column's position as
        defined in the view's ``datatable_options['columns']`` setting.

        Finally, if a third element is defined in the tuple, it will be treated as the function or
        name of a member attribute which will be used directly.

        """

        callback = column.processor
        if callback:
            if callable(callback):
                return callback
            return getattr(self, callback)

        # Treat the 'nice name' as the starting point for looking up a method
        name = column.label or column.name

        mangled_name = re.sub(r'[\W_]+', '_', name)

        f = getattr(self, 'get_column_%s_data' % mangled_name, None)
        if f:
            return f

        f = getattr(self, 'get_column_%d_data' % i, None)
        if f:
            return f

        if self.fallback_callback_target:
            f = getattr(self.fallback_callback_target, 'get_column_%s_data' % mangled_name, None)
            if f:
                return f

            f = getattr(self.fallback_callback_target, 'get_column_%d_data' % i, None)
            if f:
                return f
        return None


    # Template rendering features
    def __str__(self):
        context = {
            'url': self.url,
            'config': self.config,
            'columns': self.columns.values(),
        }
        return render_to_string(self.config['structure_template'], context)

    def __iter__(self):
        """
        Yields a 2-tuple for each column in the form ("Column Name", " data-attribute='asdf'"),
        """

        for column in self.columns.values():
            yield column

    @property
    def attributes(self):
        javascript_boolean = {
            True: 'true',
            False: 'false',
        }
        attributes = {
            'data-sortable': javascript_boolean[name not in self._meta.unsortable_columns],
            'data-visible': javascript_boolean[name not in self._meta.hidden_columns],
        }

        if name in self.ordering:
            attributes['data-sorting'] = ','.join(map(six.text_type, self.ordering[name]))

        return attributes