from babel.numbers import format_decimal
from babel.dates import format_datetime
from collections import namedtuple
from simpleeval import simple_eval, NameNotDefined, FunctionNotDefined
from simpleeval import DEFAULT_NAMES as EVAL_DEFAULT_NAMES
from simpleeval import DEFAULT_FUNCTIONS as EVAL_DEFAULT_FUNCTIONS
from typing import List
import datetime
import decimal

from .enums import *
from .errors import Error, ReportBroError, ReportBroInternalError


# parameter instance, the data map referenced by the parameter and the data map containing
# the context_id (this is usually the data map but can be different for collection
# parameters)
ParameterRef = namedtuple('ParameterRef', ['parameter', 'data', 'data_context'])
ContextEntry = namedtuple('ContextEntry', ['parameters', 'data', 'prev_entry'])
CONTEXT_ENTRY_PARAMETERS = 0
CONTEXT_ENTRY_DATA = 1
CONTEXT_ENTRY_PREV_ENTRY = 2


class Context:
    def __init__(self, report, parameters, data):
        self.report = report
        self.pattern_locale = report.document_properties.pattern_locale
        self.pattern_currency_symbol = report.document_properties.pattern_currency_symbol
        data.update(EVAL_DEFAULT_NAMES)
        # each new context (push_context) gets a new unique id
        self.id = 1
        data['__context_id'] = self.id
        self.eval_functions = EVAL_DEFAULT_FUNCTIONS.copy()
        self.eval_functions.update(
            len=len,
            decimal=decimal.Decimal,
            datetime=datetime
        )
        self.root_data = data
        self.root_data['page_number'] = 0
        self.root_data['page_count'] = 0
        self.context_stack: List[ContextEntry] = [ContextEntry(parameters=parameters, data=data, prev_entry=None)]
        # a range count is increased inside a table group band (e.g. to show header or sums for grouped rows),
        # if a range is set we have to evaluate parameter functions (e.g. sum/avg) because the range could be affected
        self.range_count = 0

    def get_parameter(self, name):
        """
        Return parameter reference for given parameter name.

        :param name: name of the parameter to find, the parameter can be present in the current
        context or any of its parents.
        :return: parameter reference which contains a parameter instance and
        its data map referenced by the parameter. None if no parameter was found.
        """
        if name.find('.') != -1:
            # this parameter is part of a collection, so we first get the reference to the
            # collection parameter and then return the parameter inside the collection, there can
            # also be multiple nested levels of collections where each collection is referenced
            # by a dot, e.g. "coll1.coll2.field"
            name_parts = name.split('.')
            collection_name = name_parts[0]
            param_ref = self._get_parameter(collection_name)
            if param_ref is not None and param_ref.parameter.type == ParameterType.map and\
                    collection_name in param_ref.data:
                parameter = param_ref.parameter
                data = param_ref.data
                while True:
                    collection_name = name_parts[0]
                    field_name = name_parts[1]
                    name_parts = name_parts[1:]
                    if len(name_parts) <= 1:
                        break
                    # nested map
                    if field_name in parameter.fields and collection_name in data:
                        parameter = parameter.fields[field_name]
                        data = data[collection_name]
                    else:
                        return None

                if field_name in parameter.fields and collection_name in data:
                    return ParameterRef(
                        parameter=parameter.fields[field_name], data=data[collection_name], data_context=data)
            return None
        else:
            return self._get_parameter(name)

    def _get_parameter(self, name, context_entry=None):
        if context_entry is None:
            context_entry = self.context_stack[-1]
        parameters = context_entry[CONTEXT_ENTRY_PARAMETERS]

        if name in parameters:
            data = context_entry[CONTEXT_ENTRY_DATA]
            return ParameterRef(parameter=parameters[name], data=data, data_context=data)
        else:
            parent_context_entry = context_entry[CONTEXT_ENTRY_PREV_ENTRY]
            if parent_context_entry:
                return self._get_parameter(name, context_entry=parent_context_entry)
        return None

    def get_parameter_data(self, param_ref):
        """
        Return data for given parameter reference.

        :param param_ref: a parameter reference which contains a parameter instance and
        its data map referenced by the parameter.
        :return: tuple of current data value of parameter, bool if parameter data exists
        """
        parameter = param_ref.parameter
        if self.range_count and parameter.is_range_function():
            return self.evaluate_parameter_func(parameter)
        elif parameter.name in param_ref.data:
            return param_ref.data[parameter.name], True
        return None, False

    @staticmethod
    def get_parameter_context_id(param_ref):
        """
        Return context_id for given parameter reference.

        This can be useful to find out if a parameter value has changed,
        e.g. parameter 'amount' in a list of invoice items has a different context_id
        in each list row (invoice item).

        :param param_ref: a parameter reference which contains a parameter instance and
        its data map referenced by the parameter.
        :return: unique context id or None if there is no context available.
        """
        if '__context_id' in param_ref.data_context:
            return param_ref.data_context['__context_id']
        return None

    def push_context(self, parameters, data):
        self.id += 1
        data['__context_id'] = self.id
        current_context_entry = self.context_stack[-1]
        self.context_stack.append(ContextEntry(parameters=parameters, data=data, prev_entry=current_context_entry))

    def pop_context(self):
        if len(self.context_stack) <= 1:
            raise ReportBroInternalError('Context.pop_context failed')
        self.context_stack = self.context_stack[:-1]

    def fill_parameters(self, expr, object_id, field, pattern=None):
        if expr.find('${') == -1:
            return expr
        rv = ''
        prev_c = None
        parameter_index = -1
        for i, c in enumerate(expr):
            if parameter_index == -1:
                if prev_c == '$' and c == '{':
                    parameter_index = i + 1
                    rv = rv[:-1]
                else:
                    rv += c
            else:
                if c == '}':
                    parameter_name = expr[parameter_index:i]
                    param_ref = self.get_parameter(parameter_name)
                    if param_ref is None:
                        raise ReportBroError(
                            Error('errorMsgInvalidExpressionNameNotDefined',
                                  object_id=object_id, field=field, info=parameter_name))
                    value, value_exists = self.get_parameter_data(param_ref)

                    if not value_exists:
                        raise ReportBroError(
                            Error('errorMsgMissingParameterData',
                                  object_id=object_id, field=field, info=parameter_name))

                    if value is not None:
                        rv += self.get_formatted_value(value, param_ref.parameter, object_id, pattern=pattern)
                    parameter_index = -1
            prev_c = c
        return rv

    def evaluate_expression(self, expr, object_id, field):
        if expr:
            try:
                data = dict(EVAL_DEFAULT_NAMES)
                expr = self.replace_parameters(expr, data=data)
                return simple_eval(expr, names=data, functions=self.eval_functions)
            except NameNotDefined as ex:
                raise ReportBroError(
                    Error('errorMsgInvalidExpressionNameNotDefined',
                          object_id=object_id, field=field, info=ex.name, context=expr))
            except FunctionNotDefined as ex:
                # avoid possible unresolved attribute reference warning by using getattr
                func_name = getattr(ex, 'func_name')
                raise ReportBroError(
                    Error('errorMsgInvalidExpressionFuncNotDefined',
                          object_id=object_id, field=field, info=func_name, context=expr))
            except SyntaxError as ex:
                raise ReportBroError(
                    Error('errorMsgInvalidExpression', object_id=object_id, field=field, info=ex.msg, context=expr))
            except Exception as ex:
                info = ex.message if hasattr(ex, 'message') else str(ex)
                raise ReportBroError(
                    Error('errorMsgInvalidExpression', object_id=object_id, field=field, info=info, context=expr))
        return True

    def evaluate_parameter_func(self, parameter):
        expr = Context.strip_parameter_name(parameter.expression)
        pos = expr.find('.')
        if pos == -1:
            raise ReportBroError(
                Error('errorMsgInvalidAvgSumExpression',
                      object_id=parameter.id, field='expression', context=parameter.name))
        else:
            parameter_name = expr[:pos]
            parameter_field = expr[pos+1:]
            param_ref = self.get_parameter(parameter_name)
            if param_ref is None or param_ref.parameter.type != ParameterType.array:
                raise ReportBroError(
                    Error('errorMsgInvalidAvgSumExpression',
                          object_id=parameter.id, field='expression', context=parameter.name))
            else:
                total = decimal.Decimal(0)
                items, data_exists = self.get_parameter_data(param_ref)
                if not data_exists or not isinstance(items, list):
                    raise ReportBroError(
                        Error('errorMsgInvalidAvgSumExpression',
                              object_id=parameter.id, field='expression', context=parameter.name))

                start, end = param_ref.parameter.get_range()
                if start is not None:
                    items = items[start:(end if end != -1 else len(items))]

                for item in items:
                    item_value = item.get(parameter_field)
                    if not isinstance(item_value, decimal.Decimal):
                        raise ReportBroError(
                            Error('errorMsgInvalidAvgSumExpression',
                                  object_id=parameter.id, field='expression', context=parameter.name))
                    total += item_value

                value = None
                if parameter.type == ParameterType.average:
                    value = total / len(items)
                elif parameter.type == ParameterType.sum:
                    value = total
                return value, True

    @staticmethod
    def strip_parameter_name(expr):
        if expr:
            return expr.strip().lstrip('${').rstrip('}')
        return expr

    @staticmethod
    def is_parameter_name(expr):
        return expr and expr.lstrip().startswith('${') and expr.rstrip().endswith('}')

    def get_formatted_value(self, value, parameter, object_id, pattern=None, is_array_item=False):
        rv = ''
        if is_array_item and parameter.type == ParameterType.simple_array:
            value_type = parameter.array_item_type
        else:
            value_type = parameter.type
        if value_type == ParameterType.string:
            if not isinstance(value, str):
                # this should not be possible because parameter types are already
                # validated in Report.parse_parameter_value
                raise ReportBroInternalError(f'value of parameter {parameter.name} must be str type')
            rv = value
        elif value_type in (ParameterType.number, ParameterType.average, ParameterType.sum):
            if pattern:
                used_pattern = pattern
                pattern_has_currency = (pattern.find('$') != -1)
            else:
                used_pattern = parameter.pattern
                pattern_has_currency = parameter.pattern_has_currency
            if used_pattern:
                try:
                    value = format_decimal(value, used_pattern, locale=self.pattern_locale)
                    if pattern_has_currency:
                        value = value.replace('$', self.pattern_currency_symbol)
                    rv = value
                except ValueError:
                    error_object_id = object_id if pattern else parameter.id
                    raise ReportBroError(
                        Error('errorMsgInvalidPattern', object_id=error_object_id, field='pattern', context=value))
            else:
                rv = str(value)
        elif value_type == ParameterType.date:
            used_pattern = pattern if pattern else parameter.pattern
            if used_pattern:
                try:
                    rv = format_datetime(value, used_pattern, locale=self.pattern_locale)
                except ValueError:
                    error_object_id = object_id if pattern else parameter.id
                    raise ReportBroError(
                        Error('errorMsgInvalidPattern',
                              object_id=error_object_id, field='pattern', context=value))
            else:
                rv = str(value)
        return rv

    def replace_parameters(self, expr, data):
        pos = expr.find('${')
        if pos == -1:
            return expr
        rv = ''
        pos2 = 0
        while pos != -1:
            if pos != 0:
                rv += expr[pos2:pos]
            pos2 = expr.find('}', pos)
            if pos2 != -1:
                parameter_name = expr[pos+2:pos2]

                param_ref = self.get_parameter(parameter_name)
                parameter_name = parameter_name.replace('.', '_')
                if param_ref:
                    value, _ = self.get_parameter_data(param_ref)
                else:
                    value = None

                data[parameter_name] = value
                rv += parameter_name
                pos2 += 1
                pos = expr.find('${', pos2)
            else:
                pos2 = pos
                pos = -1
        rv += expr[pos2:]
        return rv

    def inc_range_count(self):
        self.range_count += 1

    def dec_range_count(self):
        self.range_count -= 1

    def inc_page_number(self):
        self.root_data['page_number'] += 1

    def get_page_number(self):
        return self.root_data['page_number']

    def set_page_count(self, page_count):
        self.root_data['page_count'] = page_count
